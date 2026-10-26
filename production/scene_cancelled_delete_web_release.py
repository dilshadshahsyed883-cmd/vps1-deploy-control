"""Scene cancelled-project delete UI + guarded web route, without worker restart.

The independently owned render worker remains running while the website can
receive exact owner-authorized requests. The existing worker DELETE endpoint is
the only mechanism allowed to delete worker-owned job files and queue rows.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

from production.runtime import BASE, REPO, atomic_json, atomic_link

CURRENT = BASE / "current/scene-builder"
RELEASES = BASE / "releases/scene-builder"
TRANSACTIONS = BASE / "transactions/scene-cancelled-delete-web"
UNIT = "vps1-scene-candidate.service"
WEB_SOURCE = REPO / "apps/scene-builder/part01"
PROJECT_ID = "a" * 32

DELETE_ROUTE = '''
@app.post("/queue/{job_id}/delete")
def delete_cancelled_project_web(request: Request, job_id: str):
    require_job_access(request, job_id)
    current = worker_request("GET", f"/jobs/{job_id}").json()
    if str(current.get("status") or "").lower() != "cancelled":
        raise HTTPException(409, "Only cancelled projects can be deleted")
    if int(current.get("cancel_requested") or 0) != 1:
        raise HTTPException(409, "Cancellation has not finished")
    # A Drive copy requires the separately guarded worker's verified remote
    # deletion implementation. Never leave an unknown Drive copy orphaned.
    if str(current.get("drive_destination") or "").strip():
        raise HTTPException(409, "Drive-owned projects require verified worker cleanup")
    worker_request("DELETE", f"/jobs/{job_id}", timeout=480)
    with db() as connection:
        connection.execute("DELETE FROM job_sml_settings WHERE job_id=?", (job_id,))
        connection.execute("DELETE FROM job_ownership WHERE job_id=?", (job_id,))
    audit_event("scene_cancelled_project_deleted", "Cancelled project deleted", {"job_id": job_id})
    return RedirectResponse("/queue", status_code=303)

'''

DELETE_BUTTON = '''  if(s==='cancelled')
    out+=\`<form action="\${base}/delete" method="post" onsubmit="return confirm('Permanently delete this cancelled project and its local data? This cannot be undone.')"><button type="submit" class="danger" style="background:#fff1f2;color:#b42336;border:1px solid #f4b4bd">Delete Permanently</button></form>\`;
'''


def require(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError("SCENE_CANCELLED_DELETE_WEB_BLOCKED: " + message)


def patch_policy(text: str) -> str:
    key = "_PROJECT_RECOVERY = re.compile("
    require(text.count(key) == 1, "unrecognized candidate recovery policy")
    start = text.index(key)
    end = text.find(')\\Z")', start)
    require(end > start, "invalid recovery policy pattern")
    section = text[start:end]
    require("(?:", in section, "recovery verb group missing")
    verbs = section.split("(?:", 1)[1].split("|")
    require(all(re.fullmatch(r"[a-z-]+", word) for word in verbs),
            "unrecognized recovery verbs")
    if "delete" in verbs:
        return text
    return text[:start] + section.replace("(?:", "(?:delete|", 1) + text[end:]


def patch_web(text: str) -> str:
    require('def require_job_access(' in text and
            'def worker_request(' in text and
            'def audit_event(' in text and
            'def db(' in text and
            'RedirectResponse' in text, "missing authenticated Scene web helpers")
    if '@app.post("/queue/{job_id}/delete")' in text:
        # The currently installed route may already be safe. Avoid duplicate
        # routes or replacing unrelated application source.
        require('require_job_access(request, job_id)' in text, "owner check absent")
        return text
    anchor = '@app.post("/queue/{job_id}/cancel")'
    require(text.count(anchor) == 1, "cannot insert deletion route uniquely")
    return text.replace(anchor, DELETE_ROUTE + "\n" + anchor, 1)


def patch_queue(text: str) -> str:
    begin = 'function actionsFor(j){'
    end = '\nfunction projectRow(j)'
    require(text.count(begin) == 1, "missing queue action builder")
    pos = text.index(begin)
    finish = text.find(end, pos)
    require(finish > pos, "queue action builder boundary missing")
    section = text[pos:finish]
    require('const s=String(j.status||' in section and
            'const base=' in section, "unknown queue job action shape")
    if "Delete Permanently" in section:
        return text
    anchor = "  return out;\n}"
    require(section.count(anchor) == 1, "cannot insert project action safely")
    section = section.replace(anchor, DELETE_BUTTON + anchor)
    return text[:pos] + section + text[finish:]


def verify_policy(path: Path) -> None:
    import runpy
    policy = runpy.run_path(str(path))
    allowed = policy["mutation_allowed"]
    jid = "/queue/" + PROJECT_ID
    require(allowed("POST", jid + "/delete", True), "cancelled delete is not authorized")
    require(allowed("POST", jid + "/start", True), "manual Start permission regressed")
    require(not allowed("POST", jid + "/delete", False) and
            not allowed("GET", jid + "/delete", True) and
            not allowed("POST", "/queue/not-a-job/delete", True),
            "candidate policy permits unapproved delete")


def health() -> bool:
    for _ in range(35):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18080/health", timeout=3) as reply:
                if reply.status == 200 and json.load(reply).get("ok") is True:
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    return False


def restart_web() -> None:
    subprocess.run(["systemctl", "restart", UNIT], check=True,
                   capture_output=True, timeout=60)
    require(health(), "Scene web candidate failed health check")


def install(revision: str) -> None:
    require(os.geteuid() == 0 and re.fullmatch("[0-9a-f]{40}", revision) is not None,
            "root protected release and SHA required")
    require(shutil.disk_usage("/").free >= 5_000_000_000, "disk reserve")
    require(CURRENT.is_symlink(), "Scene web release pointer missing")
    old = CURRENT.resolve(strict=True)
    require(old.is_dir() and old.is_relative_to(RELEASES), "unowned web release")
    require(subprocess.run(["systemctl", "is-active", "--quiet", UNIT],
                           timeout=10, capture_output=True).returncode == 0 and health(),
            "existing web service not healthy")
    require((WEB_SOURCE / "templates/queue.html").is_file() and
            "Delete Permanently" in (WEB_SOURCE / "templates/queue.html").read_text(),
            "trusted queue source missing Delete action")
    target = RELEASES / (revision + "-cancelled-delete-web-only")
    journal = TRANSACTIONS / revision
    if target == old:
        require("Delete Permanently" in (target / "part01/templates/queue.html").read_text(),
                "already installed release lacks Delete control")
        verify_policy(target / "part01/precutover_policy.py")
        print("SCENE_CANCELLED_DELETE_WEB=PASS_ALREADY_INSTALLED", flush=True)
        return
    require(not target.exists() and not target.is_symlink() and not journal.exists(),
            "release transaction or target already exists")
    changed = False
    staged = False
    try:
        shutil.copytree(old, target, symlinks=True)
        staged = True
        for name, patcher in (
            ("part01/precutover_policy.py", patch_policy),
            ("part01/templates/queue.html", patch_queue),
            ("part01/app.py", patch_web),
        ):
            destination = target / name
            require(destination.is_file() and not destination.is_symlink(),
                    "existing Scene web file cannot be patched: " + name)
            original = destination.read_text(encoding="utf-8")
            updated = patcher(original)
            # Reject unrelated changes to existing application sources.
            require(len(updated) >= len(original), "hotpatch unexpectedly removed application source")
            intermediate = destination.with_name(destination.name + ".web-patch")
            intermediate.write_text(updated, encoding="utf-8")
            os.chmod(intermediate, 0o644)
            os.replace(intermediate, destination)
        verify_policy(target / "part01/precutover_policy.py")
        code = (target / "part01/app.py").read_text()
        require('@app.post("/queue/{job_id}/delete")' in code and
                'require_job_access(request, job_id)' in code and
                'worker_request("DELETE", f"/jobs/{job_id}"' in code,
                "safe cancelled Delete web handler absent")
        require("Delete Permanently" in (target / "part01/templates/queue.html").read_text(),
                "cancelled project button absent from staged UI")
        journal.mkdir(mode=0o700, parents=True)
        atomic_json(journal / "transaction.json", {
            "revision": revision, "old": str(old), "new": str(target),
            "modified_files": ["part01/precutover_policy.py",
                               "part01/templates/queue.html", "part01/app.py"],
            "status": "STAGED",
        })
        atomic_link(CURRENT, target)
        changed = True
        restart_web()
        require(CURRENT.resolve(strict=True) == target, "live Scene pointer changed unexpectedly")
        verify_policy(CURRENT / "part01/precutover_policy.py")
        atomic_json(journal / "transaction.json", {
            "revision": revision, "old": str(old), "new": str(target),
            "status": "ACTIVE", "worker": "UNTOUCHED", "jobs": "PRESERVED",
        })
        print("SCENE_CANCELLED_DELETE_WEB=PASS BUTTON=VISIBLE POLICY=LIVE WEB=HEALTHY WORKER=UNTOUCHED", flush=True)
    except BaseException:
        if changed:
            atomic_link(CURRENT, old)
            restart_web()
            print("SCENE_CANCELLED_DELETE_WEB_ROLLBACK=PASS", flush=True)
        elif staged:
            shutil.rmtree(target, ignore_errors=True)
        raise
