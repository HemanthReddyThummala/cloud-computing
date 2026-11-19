"""Migration 004: dead_letter_audit outcome constraint gains 'stale' and 'inconsistent' — applied on
top of a populated migration-003 database."""
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg.rows import dict_row

from notesagent.common.migrate import MIGRATIONS_DIR, apply_migrations

OLD = ["applied", "already_terminal", "unknown_job", "unreadable"]
NEW = ["stale", "inconsistent"]


def _insert(db, outcome):
    db.execute("INSERT INTO dead_letter_audit (dedup_key, death_reason, outcome) VALUES (%s, 'delivery_limit', %s)",
               (f"{uuid.uuid4()}:delivery_limit", outcome))


def test_004_recorded_and_constraint_named(db):
    versions = {r["version"] for r in db.execute("SELECT version FROM schema_migrations")}
    assert "004_dead_letter_audit_outcomes" in versions
    checks = db.execute("SELECT conname FROM pg_constraint WHERE conrelid = 'dead_letter_audit'::regclass"
                        " AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%outcome%'").fetchall()
    assert [c["conname"] for c in checks] == ["dead_letter_audit_outcome_allowed"]


@pytest.mark.parametrize("outcome", OLD + NEW)
def test_allowed_outcomes(db, outcome):
    _insert(db, outcome)


@pytest.mark.parametrize("outcome", ["parked", "", "APPLIED", "deferred"])
def test_other_outcomes_rejected(db, outcome):
    with pytest.raises(psycopg.errors.CheckViolation, match="dead_letter_audit_outcome_allowed"):
        _insert(db, outcome)


def test_004_applies_cleanly_on_populated_003_database(tmp_path):
    admin = os.environ["TEST_ADMIN_DATABASE_URL"]
    scratch = urlsplit(os.environ["TEST_DATABASE_URL"])._replace(path="/notes_mig004").geturl()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS notes_mig004 WITH (FORCE)")
        conn.execute("CREATE DATABASE notes_mig004")
    try:
        older = tmp_path / "upto003"
        older.mkdir()
        for name in ("001_init.sql", "002_result_integrity.sql", "003_retry_dlq_foundation.sql"):
            (older / name).write_text((Path(MIGRATIONS_DIR) / name).read_text(encoding="utf-8"), encoding="utf-8")
        assert apply_migrations(scratch, older) == ["001_init", "002_result_integrity", "003_retry_dlq_foundation"]

        with psycopg.connect(scratch, autocommit=True, row_factory=dict_row) as db:
            for outcome in OLD:
                _insert(db, outcome)
            with pytest.raises(psycopg.errors.CheckViolation):          # 003 does not know 'stale'
                _insert(db, "stale")
            before = db.execute("SELECT * FROM dead_letter_audit ORDER BY id").fetchall()

        assert apply_migrations(scratch) == ["004_dead_letter_audit_outcomes"]
        assert apply_migrations(scratch) == []                                    # idempotent

        with psycopg.connect(scratch, autocommit=True, row_factory=dict_row) as db:
            assert db.execute("SELECT * FROM dead_letter_audit ORDER BY id").fetchall() == before   # untouched
            for outcome in NEW:
                _insert(db, outcome)
            with pytest.raises(psycopg.errors.CheckViolation):
                _insert(db, "parked")
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute("DROP DATABASE IF EXISTS notes_mig004 WITH (FORCE)")
