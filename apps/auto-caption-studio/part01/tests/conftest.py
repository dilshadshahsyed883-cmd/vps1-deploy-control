from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import itertools
import hashlib
import os

import pytest


@pytest.fixture(autouse=True)
def test_entropy_source(monkeypatch):
    """Work around this runner's blocked /dev/urandom device in tests only."""
    counter = itertools.count(1)

    def deterministic_urandom(length: int) -> bytes:
        output = bytearray()
        while len(output) < length:
            value = next(counter)
            output.extend(hashlib.sha256(f"phase1-test-{value}".encode()).digest())
        return bytes(output[:length])

    monkeypatch.setattr(os, "urandom", deterministic_urandom)


@pytest.fixture
def deterministic_ids(monkeypatch, test_entropy_source):
    counter = itertools.count(1)

    def fake_uuid4():
        return SimpleNamespace(hex=f"test{next(counter):028d}")

    import web_backend.database as database
    import web_backend.queue_store as queue_store

    monkeypatch.setattr(database.uuid, "uuid4", fake_uuid4)
    monkeypatch.setattr(queue_store.uuid, "uuid4", fake_uuid4)
    try:
        import web_backend.app as app_module
        import web_backend.batch_api as batch_api

        monkeypatch.setattr(app_module.uuid, "uuid4", fake_uuid4)
        monkeypatch.setattr(batch_api.uuid, "uuid4", fake_uuid4)
    except ImportError:
        pass
    return fake_uuid4


@pytest.fixture
def db_env(tmp_path: Path, monkeypatch, deterministic_ids):
    app_root = tmp_path / "app"
    database = tmp_path / "auto_caption.db"
    monkeypatch.setenv("AUTO_CAPTION_APP_ROOT", str(app_root))
    monkeypatch.setenv("AUTO_CAPTION_DB_PATH", str(database))
    monkeypatch.setenv("AUTO_CAPTION_CODE_ROOT", str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("AUTO_CAPTION_LEASE_SECONDS", "15")
    monkeypatch.setenv("AUTO_CAPTION_MAX_ATTEMPTS", "3")

    from web_backend.database import initialize_storage

    initialize_storage()
    return {"app_root": app_root, "database": database}


@pytest.fixture
def project_factory(db_env):
    from web_backend.database import connect, now_iso

    def create(project_id: str = "project-1", owner: str = "alice") -> dict:
        path = db_env["app_root"] / "data" / "projects" / project_id
        for folder in ("input", "temp", "output", "preview", "logs"):
            (path / folder).mkdir(parents=True, exist_ok=True)
        with connect() as db:
            db.execute(
                """
                INSERT INTO projects(id, name, owner_id, status, created_at, project_path)
                VALUES (?, ?, ?, 'created', ?, ?)
                """,
                (project_id, f"Project {project_id}", owner, now_iso(), str(path)),
            )
            db.commit()
        return {
            "id": project_id,
            "owner_id": owner,
            "project_path": path,
        }

    return create
