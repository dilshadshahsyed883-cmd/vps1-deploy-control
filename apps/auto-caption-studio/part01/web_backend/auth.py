from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from .database import connect


@dataclass(frozen=True)
class Principal:
    username: str
    is_admin: bool = False


def get_current_principal(
    x_auth_user: str | None = Header(default=None),
    x_app_access: str | None = Header(default=None),
) -> Principal:
    username = (x_auth_user or "").strip()
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required")
    access = (x_app_access or "").strip().lower()
    return Principal(username=username, is_admin=access == "both")


def project_for_principal(project_id: str, principal: Principal) -> dict:
    with connect() as db:
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    item = dict(row)
    if not principal.is_admin and item["owner_id"] != principal.username:
        raise HTTPException(status_code=403, detail="Project access denied")
    return item


def require_project_access(
    project_id: str,
    principal: Principal = Depends(get_current_principal),
) -> dict:
    return project_for_principal(project_id, principal)


def job_for_principal(job_id: str, principal: Principal) -> dict:
    with connect() as db:
        row = db.execute(
            """
            SELECT j.*, p.owner_id AS project_owner_id
            FROM jobs j
            JOIN projects p ON p.id=j.project_id
            WHERE j.id=?
            """,
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    item = dict(row)
    if not principal.is_admin and item["project_owner_id"] != principal.username:
        raise HTTPException(status_code=403, detail="Job access denied")
    return item


def require_job_access(
    job_id: str,
    principal: Principal = Depends(get_current_principal),
) -> dict:
    return job_for_principal(job_id, principal)
