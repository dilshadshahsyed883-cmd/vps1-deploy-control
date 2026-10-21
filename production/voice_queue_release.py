"""Guarded Voice-only recovery for missing Kokoro/Edge queue GET endpoints.

Copy only the two reviewed API Python modules into a new immutable release;
preserve the current HTML, worker processes, data, OAuth and model assets.
The production web unit alone is restarted and rolled back if the local or
signed-in HTTPS queue checks fail.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import urllib.error
import urllib.request

from production.runtime import BASE, REPO, atomic_json, atomic_link, event, http_check, run

APP = "voice-studio"
CURRENT = BASE / "current/voice-studio"
RELEASES = BASE / "releases/voice-studio"
UNIT = "vps1-production-voice-studio.service"
JOURNALS = BASE / "transactions/voice-queue-api-only"
MODULES = (
    Path("part01/kokoro_api.py"),
    Path("part01/edge_api.py"),
)
ENGINES = ("kokoro", "edge")


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise RuntimeError("VOICE_QUEUE_RELEASE_BLOCKED: " + reason)


def check_source(path: Path, payload: bytes) -> None:
    """Reject stale or unexpected GitHub API source before touching the web unit."""
    tree = ast.parse(payload.decode("utf-8"), filename=str(path))
    routes = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and isinstance(dec.func.value, ast.Name)
                        and dec.func.value.id == "router" and dec.func.attr in {"get", "post"}
                        and dec.args and isinstance(dec.args[0], ast.Constant)):
                    routes[(dec.func.attr, dec.args[0].value)] = node.name
    require(routes.get(("get", "/jobs")) == "jobs",
            str(path) + " lacks canonical GET /jobs handler")
    require(routes.get(("get", "/status")) == "status",
            str(path) + " lacks worker status handler")
    require(routes.get(("post", "/generate")) == "generate",
            str(path) + " lacks generation handler")
    require(b"APIRouter(prefix='/tts/api/" + (
        b"kokoro" if "kokoro" in path.name else b"edge") + b"'" in payload,
            str(path) + " prefix mismatch")


def local_json(url: str) -> tuple[int, dict]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=12) as response:
            status, body = response.status, response.read(1_000_000)
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read(2048)
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        value = {}
    return status, value if isinstance(value, dict) else {}


def local_endpoint(engine: str, action: str) -> tuple[int, dict]:
    return local_json(f"http://127.0.0.1:8143/tts/api/{engine}/{action}")


def verify_queues() -> None:
    for engine in ENGINES:
        status, health = local_endpoint(engine, "status")
        require(status == 200 and health.get("ready") is True,
                engine + " upstream worker not ready: " + str(health.get("reason","status_missing")))
        status, jobs = local_endpoint(engine, "jobs?limit=1")
        require(status == 200 and isinstance(jobs.get("jobs"), list),
                engine + " upstream queue API not registered/healthy")

    # Verifies the same Nginx /voice-studio/ proxy path that the browser uses.
    from production.route_uploader_scene_https import https_get, scene_cookie
    cookie = scene_cookie()
    for engine in ENGINES:
        path = f"/voice-studio/tts/api/{engine}/jobs?limit=1"
        status, kind, body = https_get(path, cookie)
        try:
            value = json.loads(body)
        except (ValueError, TypeError):
            value = {}
        require(status == 200 and "json" in kind.lower()
                and isinstance(value, dict) and isinstance(value.get("jobs"), list),
                engine + " authenticated HTTPS queue route is not healthy")
        anonymous, _, _ = https_get(path)
        require(anonymous in (401, 403, 303),
                engine + " unauthenticated queue access unexpectedly allowed")


def install(revision: str) -> None:
    require(len(revision) == 40 and all(c in "0123456789abcdef" for c in revision),
            "immutable revision invalid")
    require(CURRENT.is_symlink(), "current release is not a managed symlink")
    previous = CURRENT.resolve(strict=True)
    require(previous.is_dir() and previous.is_relative_to(RELEASES),
            "current release pointer escaped Voice releases")
    require(subprocess.run(["systemctl", "is-active", "--quiet", UNIT],
                           check=False, capture_output=True).returncode == 0,
            "Voice web unit not active")
    http_check(APP)
    patches: dict[Path, bytes] = {}
    for relative in MODULES:
        source = REPO / "apps/voice-studio" / relative
        old = previous / relative
        require(source.is_file() and not source.is_symlink()
                and old.is_file() and not old.is_symlink(),
                str(relative) + " untrusted/missing module")
        data = source.read_bytes()
        check_source(relative, data)
        patches[relative] = data

    # A healthy queue already requires no mutation. Otherwise at least one
    # reviewed source module must differ; unchanged source means a routing
    # issue or unknown runtime state, never justification for blind restart.
    if all(local_endpoint(e, "jobs?limit=1")[0] == 200 for e in ENGINES):
        verify_queues()
        event(app=APP, result="PASS_VOICE_QUEUE_API", mode="ALREADY_HEALTHY",
              workers="PRESERVED", data="UNCHANGED")
        return
    for engine in ENGINES:
        status, value = local_endpoint(engine, "status")
        require(status == 200 and "ready" in value,
                engine + " preflight not healthy; investigate before release")
    require(any((previous / p).read_bytes() != payload for p, payload in patches.items()),
            "source equals runtime; queue 404 indicates an upstream/routing mismatch")

    target = RELEASES / (revision + "-queue-api-only")
    require(not target.exists() and not target.is_symlink(),
            "immutable candidate already exists")
    journal = JOURNALS / revision
    require(not journal.exists(), "release journal already exists")
    journal.mkdir(mode=0o700, parents=True)
    old_hashes = {str(p): hashlib.sha256((previous / p).read_bytes()).hexdigest()
                  for p in MODULES}
    atomic_json(journal / "before.json", {
        "revision": revision, "old_release": str(previous), "old_files": old_hashes,
        "workers": "untouched", "data": "untouched"
    })
    shutil.copytree(previous, target, symlinks=True)
    for relative, payload in patches.items():
        (target / relative).write_bytes(payload)
    manifest_path = target / "_production_manifest.json"
    require(manifest_path.is_file() and not manifest_path.is_symlink(),
            "release manifest not found")
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get("app") == APP and isinstance(manifest.get("files"), dict),
            "release identity mismatch")
    for path, payload in patches.items():
        require(str(path) in manifest["files"], "module absent from release manifest")
        manifest["files"][str(path)] = hashlib.sha256(payload).hexdigest()
    manifest["revision"] = revision
    manifest["voice_queue_api_only_overlay"] = str(previous)
    atomic_json(manifest_path, manifest, 0o644)
    switched = False
    try:
        atomic_link(CURRENT, target)
        switched = True
        run(["systemctl", "restart", UNIT], timeout=60, quiet=True)
        http_check(APP)
        verify_queues()
        atomic_json(journal / "result.json", {
            "state": "ACTIVE", "revision": revision,
            "workers": "PRESERVED", "data": "UNCHANGED", "queue_api": "PASS"
        })
        event(app=APP, result="PASS_VOICE_QUEUE_API", workers="PRESERVED",
              data="UNCHANGED", public_https="PASS", rollback=str(journal))
    except BaseException:
        if switched:
            atomic_link(CURRENT, previous)
            run(["systemctl", "restart", UNIT], timeout=60, quiet=True)
            http_check(APP)
            event(app=APP, rollback="PASS", workers="PRESERVED", data="UNCHANGED")
        raise
