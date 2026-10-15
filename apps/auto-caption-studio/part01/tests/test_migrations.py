from __future__ import annotations

import sqlite3


def test_fresh_database_applies_ordered_migrations(db_env):
    from web_backend.database import connect, migration_status

    assert [item["version"] for item in migration_status()] == [
        1, 2, 3, 4, 5, 6, 7, 2026081501
    ]
    with connect() as db:
        tables = {
            row["name"]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "projects",
            "jobs",
            "job_attempts",
            "job_events",
            "upload_batches",
            "batch_items",
            "drive_ingress",
            "schema_migrations",
            "scheduler_state",
            "acs_pipeline_controls",
            "acs_worker_runtime",
        }.issubset(tables)
        job_columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
        assert {
            "active_attempt_id",
            "current_stage",
            "last_heartbeat_at",
            "lease_expires_at",
            "drive_verified",
            "drive_input_verified",
            "pipeline_stage",
            "transcription_path",
            "render_verified",
            "drive_web_url",
        }.issubset(job_columns)
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_existing_minimal_database_upgrades_without_losing_job(tmp_path, monkeypatch, deterministic_ids):
    database_path = tmp_path / "legacy.db"
    project_path = tmp_path / "legacy-project"
    monkeypatch.setenv("AUTO_CAPTION_APP_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("AUTO_CAPTION_DB_PATH", str(database_path))
    db = sqlite3.connect(database_path)
    db.executescript(
        f"""
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, project_path TEXT NOT NULL
        );
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO projects VALUES (
            'legacy-project', 'Legacy', 'alice', 'created',
            '2025-01-01T00:00:00+00:00', '{project_path}'
        );
        INSERT INTO jobs VALUES (
            'legacy-job', 'legacy-project', 'rendering', 41, 'Old worker',
            '2025-01-01T00:00:00+00:00', '2025-01-01T00:01:00+00:00'
        );
        """
    )
    db.commit()
    db.close()

    from web_backend.database import connect, migration_status, run_migrations

    expected_versions = [1, 2, 3, 4, 5, 6, 7, 2026081501]
    assert run_migrations() == expected_versions
    assert [item["version"] for item in migration_status()] == expected_versions
    with connect() as upgraded:
        job = upgraded.execute("SELECT * FROM jobs WHERE id='legacy-job'").fetchone()
        assert job is not None
        assert job["status"] == "interrupted"
        assert job["progress"] == 41
        assert job["active_attempt_id"]
        assert "migration" in job["recovery_note"].lower()
        assert upgraded.execute(
            "SELECT COUNT(*) FROM job_attempts WHERE job_id='legacy-job'"
        ).fetchone()[0] == 1
        assert upgraded.execute(
            "SELECT COUNT(*) FROM job_events WHERE job_id='legacy-job'"
        ).fetchone()[0] >= 1


def test_migrations_are_idempotent(db_env):
    from web_backend.database import run_migrations

    assert run_migrations() == []
    assert run_migrations() == []


def test_separated_worker_controls_exist(db_env):
    from web_backend.database import connect

    with connect() as db:
        controls = {
            row["name"] for row in db.execute("SELECT name FROM acs_pipeline_controls")
        }
        assert {"voice", "transcribe", "render", "compress", "upload"}.issubset(controls)


def test_upgrade_from_phase3_maps_upload_queue_without_losing_job(db_env, project_factory):
    from web_backend.database import connect, run_migrations
    from web_backend.queue_store import create_job

    project_factory("legacy-upload")
    job = create_job("legacy-upload", {})
    with connect() as db:
        db.execute("UPDATE jobs SET pipeline_stage='ready_publish' WHERE id=?", (job["id"],))
        db.execute("DELETE FROM schema_migrations WHERE version=2026081501")
        # Production already uses version 8 for a different migration. Its
        # presence must not collide with this release's timestamped migration.
        db.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,name,checksum,applied_at) "
            "VALUES(8,'existing_production_migration','production-checksum',datetime('now'))"
        )
        db.execute("DELETE FROM acs_pipeline_controls WHERE name='compress'")
        db.execute("DROP TABLE acs_worker_runtime")
        db.commit()
    assert run_migrations() == [2026081501]
    with connect() as db:
        upgraded = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
        assert upgraded["pipeline_stage"] == "ready_upload"
        assert upgraded["status"] == "queued"
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] == 1
