from __future__ import annotations

from datetime import datetime, timezone

from .database import connect, now_iso


def update_worker_runtime(
    name: str,
    worker_id: str,
    state: str,
    *,
    current_job_id: str = "",
    message: str = "",
) -> None:
    timestamp = now_iso()
    with connect() as db:
        db.execute(
            """INSERT INTO acs_worker_runtime(
                   name,worker_id,state,current_job_id,last_heartbeat,started_at,message
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 worker_id=excluded.worker_id,
                 state=excluded.state,
                 current_job_id=excluded.current_job_id,
                 last_heartbeat=excluded.last_heartbeat,
                 started_at=CASE
                   WHEN acs_worker_runtime.worker_id<>excluded.worker_id
                   THEN excluded.started_at ELSE acs_worker_runtime.started_at END,
                 message=excluded.message""",
            (name, worker_id, state, current_job_id, timestamp, timestamp, message),
        )
        db.commit()


def runtime_snapshot() -> dict[str, dict]:
    with connect() as db:
        rows = db.execute(
            """SELECT name,worker_id,state,current_job_id,last_heartbeat,started_at,message
               FROM acs_worker_runtime ORDER BY name"""
        ).fetchall()
    now = datetime.now(timezone.utc)
    result: dict[str, dict] = {}
    for row in rows:
        item = dict(row)
        try:
            heartbeat = datetime.fromisoformat(str(item["last_heartbeat"]))
            age = max(0.0, (now - heartbeat).total_seconds())
        except (TypeError, ValueError):
            age = 10**9
        item["online"] = age <= 15
        item["heartbeat_age_seconds"] = round(age, 1)
        if not item["online"]:
            item["state"] = "offline"
        result[str(item["name"])] = item
    return result
