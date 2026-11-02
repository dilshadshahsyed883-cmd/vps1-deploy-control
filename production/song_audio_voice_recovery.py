"""Restore the existing Voice 8143 runtime before a separate Song Audio cutover.

Run ONLY through the protected-main, Tailscale, serialized Voice installer lane
and the exact [vps1-song-audio-voice-recovery] intent. This operation never
starts the Song worker, changes Scene routing, rewrites SQLite, changes Voice
features, or mutates existing immutable releases.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import time

from production import runtime
from production import song_audio_cutover as cutover
from production import song_audio_service as service
from production import song_audio_voice_diagnostic as diagnostic
from production import scene_candidate_release as scene

RECOVERY_MARKER = "[vps1-song-audio-voice-recovery]"
RECOVERY_JOURNALS = runtime.BASE / "transactions/song-audio-voice-recovery"
LEGACY_SCENE_STATES = {"LEGACY", "UNSET"}
COMPANIONS = ("part01/voice_tenant.py", "part01/queue_actions.py")


def _safe_source_and_unit() -> tuple[Path, Path]:
    """Require a pointer-owned existing Voice release and its unmodified unit."""
    previous = cutover._verify_pointer(cutover.VOICE_CURRENT, cutover.VOICE_RELEASES)
    unit = Path("/etc/systemd/system") / service.VOICE_UNIT
    if unit.is_symlink() or not unit.is_file():
        raise RuntimeError("Voice recovery unit missing or linked")
    lines = unit.read_text(encoding="utf-8").splitlines()
    if lines.count("WorkingDirectory=/srv/fresh-website/current/voice-studio/part01") != 1:
        raise RuntimeError("Voice recovery unit is not pointer-owned")
    executable = [x.removeprefix("ExecStart=") for x in lines
                  if x.startswith("ExecStart=")]
    pattern = (r"(/srv/fresh-website/venvs/voice-studio/[a-f0-9]{40}/bin/python)"
               r" -m uvicorn vps1_omnivoice_app:app --host 127\.0\.0\.1 --port 8143")
    if len(executable) != 1:
        raise RuntimeError("Voice recovery unit has ambiguous executable")
    match = re.fullmatch(pattern, executable[0])
    if not match or not Path(match.group(1)).is_file():
        raise RuntimeError("Voice recovery executable is unapproved or missing")
    return previous, Path(match.group(1))


def _require_safe_live_state() -> dict:
    """Read-only host proof: recovery must not compete with any Song worker."""
    state = diagnostic.report()
    if state["voice_pointer"] != "LEGACY_OWNED":
        raise RuntimeError("Voice recovery pointer not in legacy-owned state")
    if state["scene_pointer"] != "LEGACY_OWNED" or state["scene_unit"]["active"] != "active":
        raise RuntimeError("Voice recovery Scene ownership or health invalid")
    if (state["song_unit"]["active"] != "inactive" or
            state["song_unit"]["pid_present"] or
            state["song_pointer"] != "NOT_A_SYMLINK"):
        raise RuntimeError("Voice recovery blocked by independent Song worker state")
    if state["voice_split"] not in ("UNSET", "LEGACY"):
        raise RuntimeError("Voice recovery requires original unsplit backend")
    if state["scene_song_route"] not in LEGACY_SCENE_STATES:
        raise RuntimeError("Voice recovery requires original Scene audio route")
    runtime.free_port(service.SONG_PORT)
    if state["voice_health"] == "HTTP_200_OK" and state["voice_unit"]["active"] == "active":
        return state
    if (state["voice_unit"]["active"] not in ("failed", "inactive") or
            state["voice_health"] != "UNAVAILABLE" or
            not state["voice_recent_error_classes"]["ModuleNotFoundError"] or
            state["voice_missing_python_module"] != "voice_tenant"):
        raise RuntimeError("Voice recovery error does not match proven missing module")
    return state


def _reviewed_missing_files(previous: Path) -> dict[str, bytes]:
    """Never replace existing Voice V1/V1.1 modules or arbitrary source files."""
    missing = {}
    for name in COMPANIONS:
        old = previous / name
        reviewed = runtime.REPO / "apps/voice-studio" / name
        if old.is_symlink() or (old.exists() and not old.is_file()):
            raise RuntimeError("Voice recovery previous companion is unsafe")
        if reviewed.is_symlink() or not reviewed.is_file():
            raise RuntimeError("Voice recovery reviewed companion missing")
        if not old.exists():
            missing[name] = reviewed.read_bytes()
    if "part01/voice_tenant.py" not in missing:
        raise RuntimeError("Voice recovery exact missing identity file not confirmed")
    return missing


def _test_candidate_import(candidate: Path, python: Path) -> None:
    """Use the existing Voice venv for a real source import; start no worker."""
    cutover._verify_voice_candidate_modules(candidate)
    # Explicit disposable runtime roots: no live queue or Voice user data.
    scratch = "/tmp/vps1-voice-recovery-import-only"
    command = [
        "runuser", "-u", "ubuntu", "--", "/usr/bin/env", "-i",
        "HOME=/home/ubuntu", "PATH=/usr/local/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE=1", "FRESH_WEBSITE_SMOKE_TEST=1",
        "SONG_AUDIO_BACKEND_SPLIT=0",
        "VOICE_CLONE_ROOT=" + scratch + "/clone",
        "KOKORO_STUDIO_ROOT=" + scratch + "/kokoro",
        "EDGE_STUDIO_ROOT=" + scratch + "/edge",
        "AUDIO_DOWNLOADER_ROOT=" + scratch + "/audio",
        str(python), "-c",
        "import vps1_omnivoice_app; assert vps1_omnivoice_app.app is not None",
    ]
    runtime.run(command, cwd=candidate / "part01", timeout=45, quiet=True)


def _public_acceptance() -> dict:
    """Use an actual Scene session; anonymous access must remain denied."""
    scene._health()
    from production.route_uploader_scene_https import https_get, scene_cookie
    cookie = scene_cookie()
    status, content_type, body = https_get("/voice-studio/app", cookie=cookie)
    if status != 200 or "text/html" not in content_type.lower() or b"Voice" not in body:
        raise RuntimeError("Voice recovery authenticated Scene route not healthy")
    anonymous, _type, _body = https_get("/voice-studio/app")
    if anonymous not in (401, 403, 303):
        raise RuntimeError("Voice recovery anonymous SSO boundary not enforced")
    return {"voice_8143": "HEALTHY", "voice_scene_sso": "PASS",
            "anonymous": "DENIED", "song_8152": "NOT_STARTED"}


def recover(revision: str) -> None:
    runtime.check_host(revision)
    message = runtime.run(["git", "log", "-1", "--format=%B"],
                          cwd=runtime.REPO, quiet=True)
    if RECOVERY_MARKER not in message or "[vps1-song-audio-separate]" in message:
        raise RuntimeError("Voice recovery requires exclusive protected release intent")

    state = _require_safe_live_state()
    if state["voice_health"] == "HTTP_200_OK":
        runtime.event(voice_recovery="PASS_ALREADY_HEALTHY",
                      song_audio_cutover="NOT_RUN", data="PRESERVED")
        return

    previous, executable = _safe_source_and_unit()
    # The original safety gate checks ALL owners, both synthesis stores,
    # historical Song Maker handoffs, and every downloader job state.
    db = service.canonical_root() / "data/jobs.sqlite3"
    runtime.require_voice_release_idle(runtime.DATA / "voice-studio", audio_db=db)
    changes = _reviewed_missing_files(previous)

    candidate = cutover.VOICE_RELEASES / (revision + "-voice-recovery")
    cutover._clone_overlay(previous, candidate, changes,
                           "_production_manifest.json", "voice-studio", revision)
    _test_candidate_import(candidate, executable)

    # Durable pointer-only undo receipt. Never copy/delete the original SQLite
    # or media, credentials, current Voice unit or frozen feature implementation.
    journal = RECOVERY_JOURNALS / (revision + "-" + str(time.time_ns()))
    journal.mkdir(parents=True, mode=0o700)
    journal.chmod(0o700)
    runtime.atomic_json(journal / "previous.json", {
        "revision": revision, "voice": str(previous),
        "candidate": str(candidate), "data": "UNCHANGED",
        "song_audio_worker": "NOT_STARTED"}, 0o600)

    # Re-check idle under the existing serialized deploy lock immediately
    # before changing the pointer. No second audio worker exists.
    runtime.require_voice_release_idle(runtime.DATA / "voice-studio", audio_db=db)
    cutover._verify_pointer(cutover.VOICE_CURRENT, cutover.VOICE_RELEASES)
    if cutover.VOICE_CURRENT.resolve(strict=True) != previous:
        raise RuntimeError("Voice recovery pointer moved during preflight")
    runtime.free_port(service.SONG_PORT)
    cutover._verify_voice_candidate_modules(candidate)

    runtime.atomic_link(cutover.VOICE_CURRENT, candidate)
    try:
        # Failed legacy units can hit systemd's start rate limit; reset ONLY
        # its failure counter. Environment and systemd unit remain untouched.
        runtime.run(["systemctl", "reset-failed", service.VOICE_UNIT], quiet=True)
        runtime.run(["systemctl", "start", service.VOICE_UNIT], timeout=70, quiet=True)
        runtime.http_check("voice-studio")
    except BaseException:
        # Before restoring a pointer, ensure restarting the original cannot
        # create a competing worker or interrupt newly accepted jobs.
        try:
            runtime.require_voice_release_idle(runtime.DATA / "voice-studio", audio_db=db)
            current = cutover.VOICE_CURRENT.resolve(strict=True)
            if current != candidate:
                raise RuntimeError("Voice recovery candidate pointer moved")
            runtime.atomic_link(cutover.VOICE_CURRENT, previous)
            runtime.run(["systemctl", "reset-failed", service.VOICE_UNIT], quiet=True)
            runtime.run(["systemctl", "start", service.VOICE_UNIT], timeout=70, quiet=True)
        except BaseException:
            runtime.atomic_json(journal / "result.json", {
                "status": "RECOVERY_ROLLBACK_UNVERIFIED",
                "data": "PRESERVED", "song_audio": "NOT_STARTED"}, 0o600)
            raise RuntimeError("Voice recovery failed; safe rollback unverified")
        runtime.atomic_json(journal / "result.json", {
            "status": "RECOVERY_FAILED_RESTORED_PREVIOUS",
            "data": "PRESERVED", "song_audio": "NOT_STARTED"}, 0o600)
        raise RuntimeError("Voice recovery failed; original pointer restored")

    # Once the original Voice worker is running again, do NOT stop it just
    # because the public gateway is unhealthy; keep jobs and media intact.
    try:
        acceptance = _public_acceptance()
        state = _require_safe_live_state()
        if state["voice_health"] != "HTTP_200_OK" or state["voice_unit"]["active"] != "active":
            raise RuntimeError("Voice recovery local health did not remain active")
    except BaseException:
        runtime.atomic_json(journal / "result.json", {
            "status": "VOICE_LOCAL_HEALTH_ONLY_PUBLIC_ACCEPTANCE_PENDING",
            "data": "PRESERVED", "song_audio": "NOT_STARTED"}, 0o600)
        raise RuntimeError("Voice localhost recovered; public acceptance still blocked")

    runtime.atomic_json(journal / "result.json", {
        "status": "VOICE_RECOVERED",
        "acceptance": acceptance, "data": "PRESERVED",
        "song_audio": "NOT_STARTED"}, 0o600)
    runtime.event(voice_recovery="PASS", acceptance=acceptance,
                  original_database="PRESERVED", original_media="PRESERVED",
                  song_audio_cutover="NOT_RUN", rollback=str(journal))
