"""Small synchronous helpers shared by the tests."""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from notesagent.common import jobs_repo
from notesagent.common.config import get_settings
from notesagent.common.states import JobStatus
from notesagent.outbox.publisher import OutboxPublisher

VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
VIDEO_ID = "dQw4w9WgXcQ"
TABLES = ("jobs", "job_events", "job_checkpoints", "outbox", "idempotency_keys", "dead_letter_audit")


def new_token() -> str:
    return f"ntn_{uuid.uuid4().hex}{uuid.uuid4().hex}"


def token_secret_part(token: str) -> str:
    return token.removeprefix("ntn_")


def wait_until(predicate: Callable[[], Any], timeout: float = 30, interval: float = 0.1, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what or predicate}")


def submit(api, *, token: str | None = None, url: str = VIDEO_URL, key: str | None = None,
           parent: str | None = None):
    body: dict[str, Any] = {"youtube_url": url, "notion_token": token or new_token()}
    if parent:
        body["notion_parent_id"] = parent
    headers = {"Idempotency-Key": key} if key else {}
    return api.post("/jobs", json=body, headers=headers)


def submit_ok(api, **kwargs) -> str:
    resp = submit(api, **kwargs)
    assert resp.status_code == 202, resp.text
    return resp.json()["job_id"]


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True, row_factory=dict_row)


def job(db, job_id: str) -> dict[str, Any]:
    return db.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()


def events(db, job_id: str) -> list[dict[str, Any]]:
    return db.execute("SELECT * FROM job_events WHERE job_id = %s ORDER BY id", (job_id,)).fetchall()


def count(db, table: str) -> int:
    return db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def dump_all_tables(db) -> str:
    """Every row of every table as JSON text — used to prove no token is stored."""
    return "\n".join(r["t"] for table in TABLES
                     for r in db.execute(f"SELECT row_to_json(x)::text AS t FROM {table} x").fetchall())


def wait_for_status(db, job_id: str, statuses: set[str], timeout: float = 30) -> dict[str, Any]:
    return wait_until(lambda: (j := job(db, job_id)) and j["status"] in statuses and j,
                      timeout=timeout, what=f"job {job_id} in {statuses}")


def drain_outbox() -> int:
    async def _run() -> int:
        publisher = OutboxPublisher(get_settings())
        await publisher.open()
        try:
            return await publisher.drain()
        finally:
            await publisher.close()
    return asyncio.run(_run())


def force_transitions(job_id: str, path: list[JobStatus], **final_kwargs) -> None:
    """Drive a job through ``path`` with the real CAS repository (for API contract tests)."""
    async def _run() -> None:
        conn = await psycopg.AsyncConnection.connect(os.environ["TEST_DATABASE_URL"], autocommit=True,
                                                     row_factory=dict_row)
        async with conn:
            current = (await jobs_repo.get_job(conn, uuid.UUID(job_id)))["status"]
            for i, nxt in enumerate(path):
                kwargs = final_kwargs if i == len(path) - 1 else {}
                await jobs_repo.transition(conn, uuid.UUID(job_id), expected=JobStatus(current), new=nxt,
                                           worker_id="test", **kwargs)
                current = nxt
    asyncio.run(_run())


# ---------------------------------------------------------------- fixture-stage files
def files(fx: Path, prefix: str) -> list[Path]:
    return sorted(fx.glob(f"{prefix}*"))


def run_count(fx: Path, job_id: str, stage: str) -> int:
    return len(files(fx, f"run.{job_id}.{stage}."))


def started_pids(fx: Path, job_id: str, stage: str) -> list[int]:
    return [int(p.name.rsplit(".", 1)[1]) for p in files(fx, f"started.{job_id}.{stage}.")]


def release(fx: Path, job_id: str) -> None:
    (fx / f"release.{job_id}").write_text("")
