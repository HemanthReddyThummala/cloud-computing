"""Minimal, ordered SQL migration runner.

Each ``migrations/NNN_name.sql`` file is applied once, inside its own transaction,
and recorded in ``schema_migrations``. A PostgreSQL advisory lock makes concurrent
runs safe. Run as ``python -m notesagent.common.migrate``.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import psycopg

from notesagent.common.config import MigrationSettings, load_settings
from notesagent.common.logging import setup_logging

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(os.environ.get("MIGRATIONS_DIR", Path(__file__).resolve().parents[2] / "migrations"))
_LOCK_KEY = 7_420_001


def apply_migrations(conninfo: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    applied_now: list[str] = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for path in sorted(directory.glob("*.sql")):
                if path.stem in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
                applied_now.append(path.stem)
                log.info("migration applied", extra={"version": path.stem})
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied_now


def main() -> None:
    settings = load_settings(MigrationSettings)
    setup_logging("migrate", settings.log_level)
    conninfo = settings.database_url.get_secret_value()
    for attempt in range(1, 16):
        try:
            applied = apply_migrations(conninfo)
            log.info("migrations up to date", extra={"applied_now": applied})
            return
        except psycopg.OperationalError as exc:
            log.warning("database not reachable, retrying", extra={"attempt": attempt, "error_type": type(exc).__name__})
            time.sleep(2)
    raise SystemExit("database unreachable; migrations not applied")


if __name__ == "__main__":
    main()
