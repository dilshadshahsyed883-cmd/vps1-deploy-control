"""Read-only failed-cutover Voice health and source-ownership audit.

Never prints environment contents, credentials, user details, jobs, filenames,
OAuth, database paths or systemd journal lines. Never starts or stops services.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import stat
import subprocess
import urllib.error
import urllib.request

from production import runtime
from production import song_audio_service as service
from production import song_audio_cutover as cutover


def _unit(unit: str) -> dict:
    if unit not in (service.VOICE_UNIT, service.SCENE_UNIT, service.SONG_UNIT):
        raise ValueError("unrecognized Voice/Song unit")
    result = subprocess.run(
        ["systemctl", "show", unit, "--no-pager",
         "--property=ActiveState,SubState,MainPID,ExecMainStatus,Result,NRestarts"],
        capture_output=True, text=True, timeout=8, check=False)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines()
                  if "=" in line)
    return {
        "active": values.get("ActiveState", "unknown")[:24],
        "substate": values.get("SubState", "unknown")[:24],
        "result": values.get("Result", "unknown")[:32],
        "pid_present": values.get("MainPID", "0") not in ("0", ""),
        "exec_status": values.get("ExecMainStatus", "unknown")[:16],
        "restarts": values.get("NRestarts", "unknown")[:16],
    }


def _pointer(path: Path, owned: Path) -> str:
    if not path.is_symlink():
        return "NOT_A_SYMLINK"
    target = path.resolve(strict=False)
    if target.is_relative_to(owned):
        return ("SONG_AUDIO_OVERLAY" if target.name.endswith("-song-audio")
                else "LEGACY_OWNED")
    return "OUTSIDE_OWNER"


def _env_flag(path: Path, name: str, allowed: dict[str, str]) -> str:
    if path.is_symlink() or not path.is_file():
        return "MISSING_OR_LINKED"
    if not stat.S_ISREG(path.stat().st_mode):
        return "NOT_REGULAR"
    values = [line.split("=", 1)[1].strip() for line in
              path.read_text(encoding="utf-8").splitlines()
              if line.startswith(name + "=")]
    if not values:
        return "UNSET"
    if len(values) != 1:
        return "DUPLICATE"
    return allowed.get(values[0], "OTHER")


def _health() -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:8143/healthz", timeout=4) as response:
            if response.status != 200:
                return "NON_200"
            payload = json.loads(response.read(4096))
        return "HTTP_200_OK" if payload.get("ok") is True else "HTTP_200_UNHEALTHY"
    except urllib.error.HTTPError as exc:
        return "HTTP_" + str(exc.code)
    except (OSError, ValueError, TimeoutError):
        return "UNAVAILABLE"


def _error_classes() -> dict[str, bool]:
    # Search only classes, never echo full journal/error messages.
    result = subprocess.run(
        ["journalctl", "-u", service.VOICE_UNIT, "-n", "100",
         "--no-pager", "--output=cat"],
        capture_output=True, text=True, timeout=8, check=False)
    data = result.stdout[-150000:]
    patterns = {
        "ModuleNotFoundError": r"\bModuleNotFoundError\b",
        "ImportError": r"\bImportError\b",
        "PermissionError": r"\bPermissionError\b",
        "RuntimeError": r"\bRuntimeError\b",
        "SQLiteError": r"\bsqlite3\.\w+Error\b",
        "AddressAlreadyUsed": r"Address already in use",
        "WorkerLockUnavailable": r"worker.{0,20}lock|resource temporarily unavailable",
        "Traceback": r"Traceback \(most recent call last\)",
    }
    return {name: bool(re.search(pattern, data, re.IGNORECASE))
            for name, pattern in patterns.items()}



def _missing_module() -> str:
    """Emit only a syntactically valid Python module identifier, not journal text."""
    result = subprocess.run(
        ["journalctl", "-u", service.VOICE_UNIT, "-n", "160",
         "--no-pager", "--output=cat"],
        capture_output=True, text=True, timeout=8, check=False)
    text = result.stdout[-180000:]
    matches = re.findall(
        r"(?:ModuleNotFoundError|ImportError):\s*No module named\s+['\"]"
        r"([A-Za-z_][A-Za-z0-9_.]{0,79})['\"]",
        text)
    return matches[-1] if matches else "UNKNOWN"


def _song_startup_evidence() -> dict:
    """Read only the dedicated worker journal, exporting safe error classes.

    The unit may already have been removed by a successful fail-closed rollback.
    Never return journal text, private paths, tokens, owners or media filenames.
    """
    outcome = subprocess.run(
        ["journalctl", "-u", service.SONG_UNIT, "-n", "240",
         "--no-pager", "--output=cat"],
        capture_output=True, text=True, timeout=10, check=False)
    data = outcome.stdout[-180000:] if outcome.returncode == 0 else ""
    patterns = {
        "module_missing": r"\bModuleNotFoundError\b",
        "import_error": r"\bImportError\b",
        "permission_denied": r"\bPermissionError\b|\[Errno 13\]",
        "file_missing": r"\bFileNotFoundError\b|\[Errno 2\]",
        "sqlite_error": r"\bsqlite3\.\w+Error\b",
        "runtime_error": r"\bRuntimeError\b",
        "address_in_use": r"Address already in use|\[Errno 98\]",
        "worker_lock": r"worker.{0,32}lock|resource temporarily unavailable",
        "start_limit_hit": r"start request repeated too quickly|start-limit-hit",
        "no_space": r"No space left on device|\[Errno 28\]",
        "traceback": r"Traceback \(most recent call last\)",
        "uvicorn_started": r"Uvicorn running on|Application startup complete",
    }
    classes = {key: bool(re.search(pattern, data, re.IGNORECASE))
               for key, pattern in patterns.items()}
    names = re.findall(
        r"(?:ModuleNotFoundError|ImportError):\s*No module named\s+['\"]"
        r"([A-Za-z_][A-Za-z0-9_.]{0,79})['\"]",
        data)
    return {
        "journal_available": outcome.returncode == 0 and bool(data.strip()),
        "recent_error_classes": classes,
        "missing_module": names[-1] if names else "UNKNOWN",
        "service_unit_now": _unit(service.SONG_UNIT),
    }


def report() -> dict:
    recent = "NONE"
    root = runtime.BASE / "transactions/song-audio-separation"
    if root.is_dir() and not root.is_symlink():
        journals = sorted((p for p in root.iterdir()
                           if p.is_dir() and not p.is_symlink()),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        for journal in journals[:1]:
            outcome = journal / "result.json"
            if outcome.is_file() and not outcome.is_symlink():
                try:
                    value = json.loads(outcome.read_text()).get("status")
                    if value in {"ROLLBACK_FAILED", "ROLLED_BACK", "ACTIVE",
                                 "RECOVERY_BLOCKED_ACTIVE_SONG_WORK"}:
                        recent = value
                    else:
                        recent = "UNKNOWN_STATUS"
                except (OSError, ValueError):
                    recent = "UNREADABLE"
    return {
        "audit": "SONG_AUDIO_FAILED_ROLLBACK_READ_ONLY",
        "voice_unit": _unit(service.VOICE_UNIT),
        "scene_unit": _unit(service.SCENE_UNIT),
        "song_unit": _unit(service.SONG_UNIT),
        "song_startup": _song_startup_evidence(),
        "voice_pointer": _pointer(cutover.VOICE_CURRENT, cutover.VOICE_RELEASES),
        "scene_pointer": _pointer(cutover.SCENE_CURRENT, cutover.SCENE_RELEASES),
        "song_pointer": _pointer(cutover.SONG_CURRENT, cutover.SONG_RELEASES),
        "voice_split": _env_flag(
            service.VOICE_ENV, "SONG_AUDIO_BACKEND_SPLIT",
            {"0": "LEGACY", "1": "SEPARATED"}),
        "scene_song_route": _env_flag(
            service.SCENE_ENV, "SONG_AUDIO_SPLIT_UPSTREAM",
            {"http://127.0.0.1:8143": "LEGACY",
             "http://127.0.0.1:8152": "DEDICATED"}),
        "voice_health": _health(),
        "voice_recent_error_classes": _error_classes(),
        "voice_missing_python_module": _missing_module(),
        "last_cutover_record": recent,
        "queue_mutations": "NONE",
        "service_mutations": "NONE",
        "secrets_disclosed": False,
    }
