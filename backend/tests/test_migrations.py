"""Schema, migrations and database-level state-machine enforcement."""
import os
import uuid

import psycopg
import pytest

from notesagent.common import states
from notesagent.common.migrate import apply_migrations

EXPECTED_TABLES = {"jobs", "job_events", "job_checkpoints", "outbox", "idempotency_keys",
                   "job_status_transitions", "schema_migrations"}


def test_migration_recorded_and_tables_exist(db):
    assert {r["version"] for r in db.execute("SELECT version FROM schema_migrations")} == {
        "001_init", "002_result_integrity"}
    tables = {r["table_name"] for r in db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    assert EXPECTED_TABLES <= tables


def test_migrations_are_idempotent():
    assert apply_migrations(os.environ["TEST_DATABASE_URL"]) == []


def test_db_transition_table_matches_python_state_machine(db):
    db_pairs = {(r["from_status"], r["to_status"]) for r in db.execute("SELECT * FROM job_status_transitions")}
    assert db_pairs == states.all_transition_pairs()


def test_no_column_can_hold_a_token(db):
    cols = db.execute("SELECT table_name, column_name FROM information_schema.columns"
                      " WHERE table_schema = 'public' AND (column_name ILIKE '%token%'"
                      " OR column_name ILIKE '%secret%' OR column_name ILIKE '%credential%')").fetchall()
    assert cols == []


def _insert_job(db) -> str:
    job_id = str(uuid.uuid4())
    db.execute("INSERT INTO jobs (id, video_id, youtube_url) VALUES (%s, 'dQw4w9WgXcQ', 'https://youtu.be/dQw4w9WgXcQ')",
               (job_id,))
    return job_id


def test_trigger_rejects_illegal_transition(db):
    job_id = _insert_job(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE jobs SET status = 'SUMMARIZING' WHERE id = %s", (job_id,))


def test_trigger_makes_terminal_jobs_immutable(db):
    job_id = _insert_job(db)
    db.execute("UPDATE jobs SET status = 'FAILED', finished_at = now(), error = '{\"code\": \"X\"}'"
               " WHERE id = %s", (job_id,))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE jobs SET progress = 50 WHERE id = %s", (job_id,))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE jobs SET status = 'QUEUED', finished_at = NULL WHERE id = %s", (job_id,))


@pytest.mark.parametrize("video_id,status,progress", [
    ("dQw4w9WgXcQ", "DONE", 0),        # unknown status
    ("bad", "QUEUED", 0),              # malformed video id
    ("dQw4w9WgXcQ", "QUEUED", 101),    # progress out of range
])
def test_check_constraints(db, video_id, status, progress):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO jobs (id, video_id, youtube_url, status, progress) VALUES (%s, %s, 'u', %s, %s)",
                   (str(uuid.uuid4()), video_id, status, progress))


def test_finished_at_required_for_terminal_status(db):
    job_id = _insert_job(db)
    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_finished_iff_terminal"):
        db.execute("UPDATE jobs SET status = 'FAILED', error = '{\"code\": \"X\"}' WHERE id = %s", (job_id,))


URL_RESULT = '{"notion_page_url": "https://www.notion.so/x", "frames_requested": 1, "frames_published": 1}'


def _walk_to_publishing(db, job_id):
    for status in ("TRANSCRIBING", "SUMMARIZING", "EXTRACTING_FRAMES", "PUBLISHING"):
        db.execute("UPDATE jobs SET status = %s WHERE id = %s", (status, job_id))


@pytest.mark.parametrize("status", ["FAILED", "DEAD_LETTERED"])
def test_failed_jobs_can_never_carry_a_result_url(db, status):
    job_id = _insert_job(db)
    _walk_to_publishing(db, job_id)
    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_result_only_on_success"):
        db.execute("UPDATE jobs SET status = %s, finished_at = now(), error = '{\"code\": \"X\"}', result = %s"
                   " WHERE id = %s", (status, URL_RESULT, job_id))


def test_failed_jobs_must_carry_an_error(db):
    job_id = _insert_job(db)
    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_error_only_on_failure"):
        db.execute("UPDATE jobs SET status = 'FAILED', finished_at = now() WHERE id = %s", (job_id,))


@pytest.mark.parametrize("result,constraint", [
    (None, "jobs_result_only_on_success"),
    ('{}', "jobs_result_shape"),
    ('[]', "jobs_result_shape"),
    ('{"frames_requested": 1, "frames_published": 1}', "jobs_result_shape"),
    ('{"notion_page_url": "http://insecure.example", "frames_requested": 0, "frames_published": 0}',
     "jobs_result_shape"),
    ('{"notion_page_url": "https://www.notion.so/x", "frames_requested": "1", "frames_published": 1}',
     "jobs_result_shape"),
])
def test_completed_jobs_require_a_valid_result(db, result, constraint):
    job_id = _insert_job(db)
    _walk_to_publishing(db, job_id)
    with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
        db.execute("UPDATE jobs SET status = 'COMPLETED', finished_at = now(), result = %s WHERE id = %s",
                   (result, job_id))


ERROR = '{"code": "X", "message": "x"}'


@pytest.mark.parametrize("status", ["COMPLETED", "COMPLETED_WITH_WARNINGS"])
def test_success_states_accept_valid_result_without_error(db, status):
    job_id = _insert_job(db)
    _walk_to_publishing(db, job_id)
    db.execute("UPDATE jobs SET status = %s, finished_at = now(), result = %s WHERE id = %s",
               (status, URL_RESULT, job_id))
    row = db.execute("SELECT status, result, error FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row["status"] == status and row["result"]["notion_page_url"].startswith("https://")
    assert row["error"] is None


@pytest.mark.parametrize("status", ["COMPLETED", "COMPLETED_WITH_WARNINGS"])
def test_success_states_require_a_result(db, status):
    job_id = _insert_job(db)
    _walk_to_publishing(db, job_id)
    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_result_only_on_success"):
        db.execute("UPDATE jobs SET status = %s, finished_at = now() WHERE id = %s", (status, job_id))


@pytest.mark.parametrize("status", ["COMPLETED", "COMPLETED_WITH_WARNINGS"])
def test_success_states_reject_an_error(db, status):
    job_id = _insert_job(db)
    _walk_to_publishing(db, job_id)
    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_error_only_on_failure"):
        db.execute("UPDATE jobs SET status = %s, finished_at = now(), result = %s, error = %s WHERE id = %s",
                   (status, URL_RESULT, ERROR, job_id))


@pytest.mark.parametrize("status", ["FAILED", "DEAD_LETTERED"])
def test_failure_states_accept_error_without_result(db, status):
    job_id = _insert_job(db)
    db.execute("UPDATE jobs SET status = %s, finished_at = now(), error = %s WHERE id = %s",
               (status, ERROR, job_id))
    row = db.execute("SELECT status, result, error FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row["status"] == status and row["result"] is None and row["error"]["code"] == "X"


@pytest.mark.parametrize("status", ["FAILED", "DEAD_LETTERED"])
def test_failure_states_reject_result_even_with_error(db, status):
    job_id = _insert_job(db)
    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_result_only_on_success"):
        db.execute("UPDATE jobs SET status = %s, finished_at = now(), error = %s, result = %s WHERE id = %s",
                   (status, ERROR, URL_RESULT, job_id))


@pytest.mark.parametrize("status", ["QUEUED", "TRANSCRIBING", "SUMMARIZING", "EXTRACTING_FRAMES", "PUBLISHING"])
def test_in_progress_states_carry_neither_result_nor_error(db, status):
    job_id = _insert_job(db)
    for step in ("TRANSCRIBING", "SUMMARIZING", "EXTRACTING_FRAMES", "PUBLISHING")[:
                 ["QUEUED", "TRANSCRIBING", "SUMMARIZING", "EXTRACTING_FRAMES", "PUBLISHING"].index(status)]:
        db.execute("UPDATE jobs SET status = %s WHERE id = %s", (step, job_id))
    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_result_only_on_success"):
        db.execute("UPDATE jobs SET result = %s WHERE id = %s", (URL_RESULT, job_id))
    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_error_only_on_failure"):
        db.execute("UPDATE jobs SET error = %s WHERE id = %s", (ERROR, job_id))
