"""Migration 003: outbox destination, infra_attempts, dead_letter_audit — and compatibility with
an existing Phase 1 database (applied on top of real Phase 1 data)."""
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg.rows import dict_row

from notesagent.common.migrate import MIGRATIONS_DIR, apply_migrations
from tests.support.helpers import drain_outbox, submit_ok


def _insert_job(db, status_path=()):
    job_id = str(uuid.uuid4())
    db.execute("INSERT INTO jobs (id, video_id, youtube_url) VALUES (%s, 'dQw4w9WgXcQ', 'u')", (job_id,))
    for status in status_path:
        db.execute("UPDATE jobs SET status = %s WHERE id = %s", (status, job_id))
    return job_id


def test_003_recorded(db):
    versions = {r["version"] for r in db.execute("SELECT version FROM schema_migrations")}
    assert "003_retry_dlq_foundation" in versions


# ---------------------------------------------------------------- outbox destination
def test_gateway_outbox_rows_default_to_work_queue_destination(api, db):
    job_id = submit_ok(api)
    row = db.execute("SELECT destination_exchange, destination_routing_key FROM outbox WHERE job_id = %s",
                     (job_id,)).fetchone()
    assert row == {"destination_exchange": "jobs", "destination_routing_key": "job.work"}
    assert drain_outbox() == 1


@pytest.mark.parametrize("exchange,key,constraint", [
    ("amq.direct", "job.work", "outbox_destination_exchange_allowed"),
    ("", "job.work", "outbox_destination_exchange_allowed"),
    ("jobs", "", "outbox_destination_routing_key_valid"),
    ("jobs", "has space", "outbox_destination_routing_key_valid"),
    ("jobs.retry", "x" * 256, "outbox_destination_routing_key_valid"),
])
def test_outbox_destination_constraints(db, exchange, key, constraint):
    job_id = _insert_job(db)
    with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
        db.execute("INSERT INTO outbox (event_id, job_id, event_type, payload, destination_exchange,"
                   " destination_routing_key) VALUES (%s, %s, 'x', '{}', %s, %s)",
                   (str(uuid.uuid4()), job_id, exchange, key))


@pytest.mark.parametrize("exchange,key", [("jobs", "job.work"), ("jobs.retry", "30s"), ("jobs.dlx", "dead")])
def test_outbox_destination_allowed_values(db, exchange, key):
    job_id = _insert_job(db)
    db.execute("INSERT INTO outbox (event_id, job_id, event_type, payload, destination_exchange,"
               " destination_routing_key) VALUES (%s, %s, 'x', '{}', %s, %s)",
               (str(uuid.uuid4()), job_id, exchange, key))


# ---------------------------------------------------------------- infra_attempts
def test_infra_attempts_defaults_to_zero_and_rejects_negative(db):
    job_id = _insert_job(db)
    assert db.execute("SELECT infra_attempts FROM jobs WHERE id = %s", (job_id,)).fetchone()["infra_attempts"] == 0
    db.execute("UPDATE jobs SET infra_attempts = 3 WHERE id = %s", (job_id,))
    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_infra_attempts_non_negative"):
        db.execute("UPDATE jobs SET infra_attempts = -1 WHERE id = %s", (job_id,))


# ---------------------------------------------------------------- dead_letter_audit
def _audit(db, **overrides):
    values = {"dedup_key": f"msg-{uuid.uuid4()}:delivery_limit", "message_id": "m", "job_id": None,
              "death_reason": "delivery_limit", "source_queue": "jobs.work.v2", "outcome": "applied"} | overrides
    db.execute("INSERT INTO dead_letter_audit (dedup_key, message_id, job_id, death_reason, source_queue, outcome)"
               " VALUES (%(dedup_key)s, %(message_id)s, %(job_id)s, %(death_reason)s, %(source_queue)s, %(outcome)s)",
               values)
    return values


def test_audit_accepts_unknown_job_ids_without_foreign_key(db):
    row = _audit(db, job_id=str(uuid.uuid4()), outcome="unknown_job")
    stored = db.execute("SELECT * FROM dead_letter_audit WHERE dedup_key = %s", (row["dedup_key"],)).fetchone()
    assert stored["outcome"] == "unknown_job" and stored["seen_count"] == 1 and stored["detail"] == {}


def test_audit_dedup_key_is_unique(db):
    row = _audit(db)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _audit(db, dedup_key=row["dedup_key"])


@pytest.mark.parametrize("field,value", [("outcome", "deleted"), ("death_reason", "boom"), ("dedup_key", "")])
def test_audit_constraints(db, field, value):
    with pytest.raises(psycopg.errors.CheckViolation):
        _audit(db, **{field: value})


def test_audit_table_stores_no_message_bodies(db):
    columns = {r["column_name"] for r in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'dead_letter_audit'")}
    assert columns == {"id", "dedup_key", "message_id", "job_id", "death_reason", "source_queue", "outcome",
                       "detail", "seen_count", "first_seen_at", "last_seen_at"}


# ---------------------------------------------------------------- compatibility with Phase 1 data
def test_003_applies_cleanly_on_a_populated_phase1_database(tmp_path):
    """Apply 001+002, create real Phase 1 rows (incl. a terminal job — the trigger forbids
    updating it), then apply 003: nothing is rewritten, defaults are filled in."""
    admin = os.environ["TEST_ADMIN_DATABASE_URL"]
    scratch = urlsplit(os.environ["TEST_DATABASE_URL"])._replace(path="/notes_mig003").geturl()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS notes_mig003 WITH (FORCE)")
        conn.execute("CREATE DATABASE notes_mig003")
    try:
        phase1_dir = tmp_path / "phase1"
        phase1_dir.mkdir()
        for name in ("001_init.sql", "002_result_integrity.sql"):
            (phase1_dir / name).write_text((Path(MIGRATIONS_DIR) / name).read_text(encoding="utf-8"), encoding="utf-8")
        assert apply_migrations(scratch, phase1_dir) == ["001_init", "002_result_integrity"]

        with psycopg.connect(scratch, autocommit=True, row_factory=dict_row) as db:
            done = _insert_job(db)
            db.execute("UPDATE jobs SET status = 'FAILED', finished_at = now(),"
                       " error = '{\"code\": \"X\", \"message\": \"x\"}' WHERE id = %s", (done,))
            running = _insert_job(db, ("TRANSCRIBING",))
            db.execute("INSERT INTO outbox (event_id, job_id, event_type, payload, published_at)"
                       " VALUES (%s, %s, 'job.submitted', '{}', now())", (str(uuid.uuid4()), done))
            before = db.execute("SELECT id, status, updated_at FROM jobs ORDER BY id").fetchall()

        # 003 (and the later 004, which only touches dead_letter_audit) apply on Phase 1 data
        assert apply_migrations(scratch) == ["003_retry_dlq_foundation", "004_dead_letter_audit_outcomes"]
        assert apply_migrations(scratch) == []                                    # idempotent

        with psycopg.connect(scratch, autocommit=True, row_factory=dict_row) as db:
            after = db.execute("SELECT id, status, updated_at, infra_attempts FROM jobs ORDER BY id").fetchall()
            assert [(r["id"], r["status"], r["updated_at"]) for r in after] == \
                   [(r["id"], r["status"], r["updated_at"]) for r in before]       # rows untouched
            assert all(r["infra_attempts"] == 0 for r in after)
            ob = db.execute("SELECT destination_exchange, destination_routing_key FROM outbox").fetchone()
            assert ob == {"destination_exchange": "jobs", "destination_routing_key": "job.work"}
            assert running and db.execute("SELECT count(*) AS n FROM dead_letter_audit").fetchone()["n"] == 0
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute("DROP DATABASE IF EXISTS notes_mig003 WITH (FORCE)")
