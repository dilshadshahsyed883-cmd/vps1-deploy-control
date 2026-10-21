"""Idempotent Scene-owned builtin LUT provisioning; never touch existing projects.

This runs as the existing non-root production-worker user, using the live
worker's effective HOME/XDG paths, before the guarded Scene source promotion.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

from production.scene_production_worker import (
    BASE, ENV, RECORD, STATE, UNIT, VENV, entries, require,
)

# Executed by the worker's exact Python against the new, immutable Scene source.
# Source lookup still uses the existing smart_scroll.lut_engine resolver.
LUT_PROBE = r'''
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

from smart_scroll.lut_engine import (
    LUT_SIZE, PACK_DEFINITIONS, _infer_cube_size, _parse_cube, _write_cube,
    lut_filter_for_scene, persistent_lut_root, resolve_lut_source,
)

state = Path(os.environ["SCENE_WORKER_DATA"]).resolve(strict=True)
home = Path(os.environ["HOME"]).resolve(strict=True)
assert home == state / "worker-home", "Unexpected worker HOME"
root = persistent_lut_root()
assert root.resolve().is_relative_to(state), "LUT directory outside Scene state"
created = 0
existing = 0
for pack, config in PACK_DEFINITIONS.items():
    for preset in config["presets"]:
        folder = root / pack
        assert not folder.is_symlink(), "Unowned symlink in LUT pack"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (preset + ".cube")
        assert not path.is_symlink(), "Unowned symlink in LUT preset"
        if not path.exists():
            # Generate and validate in the same directory, then publish via a
            # no-clobber hard link. Never overwrite a user's existing LUT.
            with tempfile.NamedTemporaryFile(dir=folder, prefix=".scene-lut-",
                                             suffix=".cube", delete=False) as temp:
                candidate = Path(temp.name)
            try:
                _write_cube(candidate, pack, preset)
                headers, data = _parse_cube(candidate)
                assert _infer_cube_size(headers, len(data)) == LUT_SIZE
                assert len(data) == LUT_SIZE**3
                try:
                    os.link(candidate, path)
                    created += 1
                except FileExistsError:
                    existing += 1
            finally:
                candidate.unlink(missing_ok=True)
        else:
            existing += 1
        headers, data = _parse_cube(path)
        assert _infer_cube_size(headers, len(data)) == LUT_SIZE
        assert len(data) == LUT_SIZE**3
        resolved = resolve_lut_source({"pack": pack, "preset": preset}, None)
        assert resolved is not None and resolved.samefile(path), "Production LUT resolver mismatch"

choice = {"pack": "neutral_correction", "preset": "subtle_contrast",
          "label": "Neutral / Correction LUTs • Subtle Contrast"}
with tempfile.TemporaryDirectory(prefix="lut-check-", dir=state / "tmp") as temp:
    filter_chain, label = lut_filter_for_scene(
        {"scene_code": "SCENE_001"}, choice,
        SimpleNamespace(lut_enabled=True, lut_normalize_enabled=True,
                        lut_strength_percent=40), Path(temp), {})
    assert filter_chain.startswith("eq=") and "lut3d=" in filter_chain
    assert "Missing LUT:" not in label
    subprocess.run(
        ["/usr/bin/ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=5:d=0.2",
         "-vf", filter_chain, "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, text=True, check=True, timeout=30,
    )

print(json.dumps({"builtins": created + existing, "new_files": created,
                  "existing_preserved": existing, "cube_entries": LUT_SIZE**3,
                  "scene_001_subtle_contrast": "PASS",
                  "real_ffmpeg_lut_filter": "PASS",
                  "worker_lut_location": "OWNED_SCENE_STATE"}))
'''


def _worker_source(record: dict[str, str]) -> Path:
    require(record.get("owner") == "scene-production-worker-v1" and
            record.get("unit") == UNIT and record.get("state") == str(STATE),
            "unknown Scene production worker ownership")
    path = Path(str(record.get("source") or ""))
    require(path.is_absolute() and path.is_relative_to(BASE / "releases/scene-builder")
            and re.fullmatch(r"[0-9a-f]{40}", path.name) and
            (path / "part02/smart_scroll/lut_engine.py").is_file(),
            "unrecognized running Scene source")
    runner = BASE / "releases/scene-production-worker" / path.name
    require((runner / "vps_engine").is_symlink() and
            (runner / "vps_engine").resolve() == path / "part02",
            "Scene worker Python source no longer matches owned unit")
    return path


def _process_env() -> dict[bytes, bytes]:
    pid = subprocess.run(
        ["systemctl", "show", "--property=MainPID", "--value", UNIT],
        check=True, text=True, capture_output=True, timeout=20,
    ).stdout.strip()
    require(pid.isdecimal() and int(pid) > 1, "Scene worker process is inactive")
    values = dict(field.split(b"=", 1) for field in
                  (Path("/proc") / pid / "environ").read_bytes().split(b"\0")
                  if b"=" in field)
    require(values.get(b"HOME") == str(STATE / "worker-home").encode(),
            "Scene worker live HOME is not the owned writable directory")
    return values


def provision_builtins(candidate_source: Path) -> dict[str, object]:
    """Add missing builtin LUTs only. No unit restart, queue edits, or key reads."""
    require(os.geteuid() == 0, "protected Scene release required")
    require(STATE.is_dir() and not STATE.is_symlink(), "Scene worker state missing")
    record = json.loads(RECORD.read_text())
    running = _worker_source(record)
    require(running != candidate_source or
            (candidate_source / "part02/smart_scroll/lut_engine.py").is_file(),
            "candidate Scene asset source invalid")
    require(candidate_source.is_relative_to(BASE / "releases/scene-builder") and
            (candidate_source / "part02/smart_scroll/lut_engine.py").is_file(),
            "candidate Scene assets not owned by immutable source")
    config = entries(ENV.read_text())
    require(config.get("SCENE_WORKER_DATA") == str(STATE), "worker state drift")
    actual = _process_env()
    for key in (b"SCENE_WORKER_DATA", b"HOME"):
        require(actual.get(key) == config.get(key.decode(), "").encode()
                if key == b"SCENE_WORKER_DATA" else
                actual.get(key) == str(STATE / "worker-home").encode(),
                "live worker environment mismatch")
    selected = {
        "HOME": str(STATE / "worker-home"),
        "SCENE_WORKER_DATA": str(STATE),
        "TMPDIR": str(STATE / "tmp"),
        "PYTHONPATH": str(candidate_source / "part02"),
    }
    if actual.get(b"XDG_DATA_HOME"):
        selected["XDG_DATA_HOME"] = actual[b"XDG_DATA_HOME"].decode()
    elif config.get("XDG_DATA_HOME"):
        raise RuntimeError("worker XDG data path did not reach actual process")
    command = ["/usr/sbin/runuser", "-u", "vps1scene", "--", "/usr/bin/env",
               *[k + "=" + v for k, v in selected.items()],
               str(VENV / "bin/python"), "-c", LUT_PROBE]
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    require(result.returncode == 0,
            "Scene builtin LUT preparation/FFmpeg proof failed: " +
            result.stderr[-1300:].replace("\n", " "))
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError("Missing machine-readable LUT evidence") from exc
    require(payload.get("builtins") == 23 and
            payload.get("scene_001_subtle_contrast") == "PASS" and
            payload.get("real_ffmpeg_lut_filter") == "PASS",
            "Incomplete Scene built-in LUT or real-filter verification")
    print("SCENE_BUILTIN_LUTS=PASS COUNT=23 REAL_FFMPEG=PASS EXISTING_PROJECTS=UNTOUCHED",
          flush=True)
    return payload
