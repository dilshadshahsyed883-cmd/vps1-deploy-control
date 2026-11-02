"""Guarded recovery of missing voice_tenant.py in the currently failed Voice web.

No feature migration or Song cutover. One immutable source-only overlay and
one Voice web service start. The old queue/media/Drive data never moves.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from production import runtime
from production import song_audio_service as service

BASE = runtime.BASE
REPO = runtime.REPO
VOICE_CURRENT = BASE / "current/voice-studio"
VOICE_RELEASES = BASE / "releases/voice-studio"
SONG_CURRENT = BASE / "current/song-studio"
VOICE_UNIT = service.VOICE_UNIT
SONG_UNIT = service.SONG_UNIT
VOICE_UNIT_PATH = Path("/etc/systemd/system") / VOICE_UNIT
JOURNALS = BASE / "transactions/voice-tenant-module-recovery"
TENANT = Path("part01/voice_tenant.py")
ENTRY = Path("part01/vps1_omnivoice_app.py")


def require(value: bool, detail: str) -> None:
    if not value:
        raise RuntimeError("VOICE_TENANT_RECOVERY_BLOCKED: " + detail)


def _active(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", unit],
                          capture_output=True, check=False).returncode == 0


def _source() -> bytes:
    source = REPO / "apps/voice-studio" / TENANT
    require(source.is_file() and not source.is_symlink(),
            "reviewed Voice session verifier unavailable")
    payload = source.read_bytes()
    require(len(payload) < 20000 and b"def scene_identity(" in payload
            and b"def set_identity(" in payload
            and b"def reset_identity(" in payload,
            "reviewed Scene SSO module changed")
    compile(payload, str(source), "exec")
    return payload


def _release(previous: Path, revision: str, source: bytes) -> Path:
    require(re.fullmatch(r"[a-f0-9]{40}", revision) is not None,
            "exact protected main revision required")
    require(previous.is_relative_to(VOICE_RELEASES),
            "Voice release pointer outside approved ownership")
    entry = previous / ENTRY
    manifest_file = previous / "_production_manifest.json"
    require(entry.is_file() and not entry.is_symlink(),
            "existing Voice entrypoint is missing")
    require(b"from voice_tenant import" in entry.read_bytes(),
            "Voice entrypoint does not require the diagnosed module")
    require(not (previous / TENANT).exists() and not (previous / TENANT).is_symlink(),
            "old release already contains Voice tenant module")
    require(manifest_file.is_file() and not manifest_file.is_symlink(),
            "owned Voice release manifest missing")
    prior = json.loads(manifest_file.read_text())
    require(prior.get("app") == "voice-studio" and str(ENTRY) in prior.get("files", {}),
            "existing manifest ownership mismatch")
    require(str(TENANT) not in prior.get("files", {}),
            "Voice tenant is already manifest-owned")
    target = VOICE_RELEASES / (revision + "-tenant-recovery")
    require(not target.is_symlink(), "linked recovery release forbidden")
    updated = dict(prior)
    updated["files"] = dict(prior["files"])
    updated["files"][str(TENANT)] = hashlib.sha256(source).hexdigest()
    updated["revision"] = revision
    updated["voice_tenant_recovery_overlay"] = str(previous)
    if target.exists():
        manifest = target / "_production_manifest.json"
        require(manifest.is_file() and not manifest.is_symlink(),
                "unowned previous recovery attempt")
        require(json.loads(manifest.read_text()) == updated,
                "recovery manifest differs on retry")
        require((target / TENANT).read_bytes() == source,
                "recovery module modified since prior stage")
        return target
    shutil.copytree(previous, target, symlinks=True)
    (target / TENANT).write_bytes(source)
    (target / TENANT).chmod(0o644)
    runtime.atomic_json(target / "_production_manifest.json", updated, 0o644)
    return target


def _anonymous_denied() -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:8143/app", timeout=5):
            raise RuntimeError("Anonymous Voice app returned a public document")
    except urllib.error.HTTPError as exc:
        require(exc.code in (401, 403), "Voice SSO anonymous gate did not deny")


def install(revision: str) -> None:
    runtime.check_host(revision)
    intent = runtime.run(["git", "log", "-1", "--format=%B"], cwd=REPO, quiet=True)
    require("[vps1-voice-tenant-recovery-only]" in intent,
            "protected Voice recovery merge intent missing")
    require(_active(service.SCENE_UNIT), "Scene identity authority inactive")
    require(not _active(SONG_UNIT) and not SONG_CURRENT.exists() and
            not SONG_CURRENT.is_symlink(),
            "independent Song worker or source pointer exists")
    require(VOICE_UNIT_PATH.is_file() and not VOICE_UNIT_PATH.is_symlink(),
            "Voice web unit is not a managed regular file")
    unit_text = VOICE_UNIT_PATH.read_text()
    require("WorkingDirectory=/srv/fresh-website/current/voice-studio/part01" in unit_text,
            "Voice web unit does not use reviewed active pointer")
    require(VOICE_CURRENT.is_symlink(), "existing Voice pointer missing")
    previous = VOICE_CURRENT.resolve(strict=True)
    require(previous.is_relative_to(VOICE_RELEASES), "Voice source ownership drift")
    candidate = VOICE_RELEASES / (revision + "-tenant-recovery")
    if previous == candidate and _active(VOICE_UNIT):
        runtime.http_check("voice-studio")
        _anonymous_denied()
        runtime.event(voice_tenant_recovery="PASS_ALREADY_ACTIVE", data="PRESERVED",
                      voice_app="HEALTHY")
        return
    require(not _active(VOICE_UNIT),
            "Voice already active: do not restart a running production service")
    # User jobs, including the sole dormant orphan, stay unchanged and all
    # genuinely in-flight transfers must be empty before re-enabling Voice.
    runtime.require_voice_release_idle(runtime.DATA / "voice-studio")
    source = _source()
    target = _release(previous, revision, source)
    journal = JOURNALS / (revision + "-" + str(time.time_ns()))
    journal.mkdir(parents=True, mode=0o700)
    journal.chmod(0o700)
    runtime.atomic_json(journal / "previous.json", {
        "revision": revision, "previous": str(previous), "candidate": str(target),
        "service": VOICE_UNIT, "user_data": "PRESERVED"})
    switched = False
    try:
        runtime.atomic_link(VOICE_CURRENT, target)
        switched = True
        runtime.run(["systemctl", "reset-failed", VOICE_UNIT], timeout=30, quiet=True)
        runtime.run(["systemctl", "start", VOICE_UNIT], timeout=70, quiet=True)
        runtime.http_check("voice-studio")
        _anonymous_denied()
        runtime.atomic_json(journal / "result.json", {
            "status": "ACTIVE", "voice_health": "PASS",
            "session_gate": "ANONYMOUS_DENIED", "queue": "PRESERVED"})
        runtime.event(voice_tenant_recovery="PASS",
                      voice_health="HTTP_200", session_gate="ANONYMOUS_DENIED",
                      independent_song_worker="OFFLINE", data="PRESERVED",
                      rollback=str(journal), auto_script="OFFLINE")
    except BaseException:
        if switched:
            runtime.atomic_link(VOICE_CURRENT, previous)
        runtime.atomic_json(journal / "result.json", {
            "status": "FAILED_LEGACY_STILL_UNHEALTHY",
            "queue": "PRESERVED", "previous_release": str(previous)})
        # Do not restart the known-broken legacy module or claim rollback passed.
        # Independent Voice synthesis workers and Song data are untouched.
        raise
