from __future__ import annotations


def _seed_batch(db, *, batch_id: str, category: str, created_at: str, projects: list[dict]) -> list[str]:
    from web_backend.queue_store import create_job_record

    db.execute(
        """INSERT INTO upload_batches(
               id,owner_id,status,expected_count,staged_count,verified_count,
               category,settings_json,created_at,updated_at,finalized_at
           ) VALUES(?,?,'accepted',?,?,?,?, '{}',?,?,?)""",
        (batch_id, "alice", len(projects), len(projects), len(projects), category,
         created_at, created_at, created_at),
    )
    job_ids = []
    for order, project in enumerate(projects, 1):
        row = db.execute("SELECT * FROM projects WHERE id=?", (project["id"],)).fetchone()
        job_id = create_job_record(
            db, project=row, settings={"category": category}, batch_id=batch_id,
            client_item_id=f"{batch_id}-{order}", category=category, upload_verified=True,
        )
        item_id = f"item-{batch_id}-{order}"
        db.execute(
            """INSERT INTO batch_items(
                   id,batch_id,client_item_id,project_id,job_id,project_name,category,
                   upload_verified,item_order,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,1,?,?,?)""",
            (item_id,batch_id,f"{batch_id}-{order}",project["id"],job_id,
             f"Project {order}",category,order,created_at,created_at),
        )
        job_ids.append(job_id)
    return job_ids


def test_category_rotation_prefers_a_different_category(db_env, project_factory):
    from web_backend.database import connect, now_iso
    from web_backend.pipeline_v3 import LAST_CATEGORY_KEY, _set_state, claim_stage

    news = project_factory("news")
    arabic = project_factory("arabic")
    with connect() as db:
        news_jobs = _seed_batch(
            db, batch_id="batch-news", category="News",
            created_at="2026-01-01T00:00:00+00:00", projects=[news],
        )
        arabic_jobs = _seed_batch(
            db, batch_id="batch-arabic", category="Arabic",
            created_at="2026-01-02T00:00:00+00:00", projects=[arabic],
        )
        _set_state(db, LAST_CATEGORY_KEY, "News")
        db.commit()

    claimed = claim_stage("ready_transcribe", "transcriber-test")
    assert claimed["id"] == arabic_jobs[0]
    assert claimed["batch_id"] == "batch-arabic"
    assert claimed["lease_expires_at"] is not None
    assert claimed["id"] != news_jobs[0]


def test_active_batch_is_not_mixed_and_stage_handoff_is_durable(db_env, project_factory):
    from web_backend.database import connect
    from web_backend.pipeline_v3 import claim_stage
    from web_backend.queue_store import finish_job, release_job_to_queue
    from web_backend.job_states import FAILED

    first = project_factory("first")
    second = project_factory("second")
    other = project_factory("other")
    with connect() as db:
        selected_jobs = _seed_batch(
            db, batch_id="batch-selected", category="News",
            created_at="2026-01-01T00:00:00+00:00", projects=[first, second],
        )
        other_jobs = _seed_batch(
            db, batch_id="batch-other", category="Arabic",
            created_at="2026-01-02T00:00:00+00:00", projects=[other],
        )
        db.commit()

    one = claim_stage("ready_transcribe", "transcriber-1")
    assert one["id"] == selected_jobs[0]
    queued = release_job_to_queue(
        one["id"], "transcriber-1", pipeline_stage="ready_render", progress=35,
        message="Transcription persisted",
    )
    assert queued["status"] == "queued"
    assert queued["pipeline_stage"] == "ready_render"

    two = claim_stage("ready_transcribe", "transcriber-2")
    assert two["id"] == selected_jobs[1]
    assert two["id"] != other_jobs[0]
    finish_job(two["id"], "transcriber-2", FAILED, message="isolated failure")

    # The first project's next stage remains claimable from the same batch;
    # the failed sibling does not block it and the other batch is not mixed in.
    render = claim_stage("ready_render", "renderer-1")
    assert render["id"] == selected_jobs[0]


def test_pause_after_current_blocks_only_new_stage_claims(db_env, project_factory):
    from web_backend.database import connect, now_iso
    from web_backend.pipeline_v3 import claim_stage

    project = project_factory("compress-waiting")
    with connect() as db:
        jobs = _seed_batch(
            db, batch_id="batch-compress", category="News",
            created_at="2026-01-01T00:00:00+00:00", projects=[project],
        )
        db.execute(
            "UPDATE jobs SET pipeline_stage='ready_compress' WHERE id=?", (jobs[0],)
        )
        db.execute(
            "UPDATE acs_pipeline_controls SET paused=1,updated_at=? WHERE name='compress'",
            (now_iso(),),
        )
        db.commit()
    assert claim_stage("ready_compress", "compressor-1") is None
    with connect() as db:
        db.execute(
            "UPDATE acs_pipeline_controls SET paused=0,updated_at=? WHERE name='compress'",
            (now_iso(),),
        )
        db.commit()
    claimed = claim_stage("ready_compress", "compressor-1")
    assert claimed["id"] == jobs[0]
