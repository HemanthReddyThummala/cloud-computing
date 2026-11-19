"""All SQL touching job state lives here (gateway and workers share it)."""
from __future__ import annotations

import uuid
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb

from notesagent.common import states
from notesagent.common.errors import InvalidTransition, StaleJobState
from notesagent.common.states import JobStatus

_UPDATABLE_FIELDS = {"video_title", "duration_sec"}

# PostgreSQL NOTIFY channel the gateway signals after committing an outbox row.
OUTBOX_NOTIFY_CHANNEL = "outbox_new"


# ---------------------------------------------------------------- reads
async def get_job(conn: AsyncConnection, job_id: uuid.UUID) -> dict[str, Any] | None:
    cur = await conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
    return await cur.fetchone()


async def get_checkpoints(conn: AsyncConnection, job_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    cur = await conn.execute("SELECT stage, data FROM job_checkpoints WHERE job_id = %s", (job_id,))
    return {row["stage"]: row["data"] for row in await cur.fetchall()}


# ---------------------------------------------------------------- submission
async def insert_job(conn: AsyncConnection, *, job_id: uuid.UUID, video_id: str, youtube_url: str,
                     notion_parent_id: uuid.UUID | None) -> None:
    await conn.execute(
        "INSERT INTO jobs (id, video_id, youtube_url, notion_parent_id, status, progress)"
        " VALUES (%s, %s, %s, %s, 'QUEUED', 0)",
        (job_id, video_id, youtube_url, notion_parent_id),
    )


async def insert_outbox(conn: AsyncConnection, *, event_id: uuid.UUID, job_id: uuid.UUID, event_type: str,
                        payload: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO outbox (event_id, job_id, event_type, payload) VALUES (%s, %s, %s, %s)",
        (event_id, job_id, event_type, Jsonb(payload)),
    )


async def insert_event(conn: AsyncConnection, *, job_id: uuid.UUID, event_type: str,
                       from_status: str | None = None, to_status: str | None = None,
                       worker_id: str | None = None, detail: dict[str, Any] | None = None) -> None:
    await conn.execute(
        "INSERT INTO job_events (job_id, event_type, from_status, to_status, worker_id, detail)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (job_id, event_type, from_status, to_status, worker_id, Jsonb(detail or {})),
    )


# ---------------------------------------------------------------- idempotency keys (ADR-04)
async def find_idempotency_key(conn: AsyncConnection, key: str, ttl_hours: float) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT key, fingerprint, job_id, response FROM idempotency_keys"
        " WHERE key = %s AND created_at > now() - make_interval(secs => %s)",
        (key, ttl_hours * 3600),
    )
    return await cur.fetchone()


async def delete_expired_idempotency_key(conn: AsyncConnection, key: str, ttl_hours: float) -> None:
    await conn.execute(
        "DELETE FROM idempotency_keys WHERE key = %s AND created_at <= now() - make_interval(secs => %s)",
        (key, ttl_hours * 3600),
    )


async def claim_idempotency_key(conn: AsyncConnection, *, key: str, fingerprint: str, job_id: uuid.UUID,
                                response: dict[str, Any]) -> bool:
    """True if this request now owns the key; False if another request already does."""
    cur = await conn.execute(
        "INSERT INTO idempotency_keys (key, fingerprint, job_id, response) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (key) DO NOTHING RETURNING key",
        (key, fingerprint, job_id, Jsonb(response)),
    )
    return await cur.fetchone() is not None


# ---------------------------------------------------------------- compare-and-set transitions
async def transition(
    conn: AsyncConnection,
    job_id: uuid.UUID,
    *,
    expected: JobStatus,
    new: JobStatus,
    worker_id: str | None = None,
    event_type: str = "transition",
    detail: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically move a job from ``expected`` to ``new``.

    In ONE transaction: optionally save the checkpoint of the stage that just finished,
    update the job only if it is still in ``expected`` (compare-and-set), and append an
    audit event. Raises InvalidTransition (illegal move) or StaleJobState (CAS lost).
    """
    expected, new = JobStatus(expected), JobStatus(new)
    if not states.can_transition(expected, new):
        raise InvalidTransition(expected.value, new.value)

    sets = [sql.SQL("status = %(new)s"), sql.SQL("retry = NULL")]
    params: dict[str, Any] = {"id": job_id, "expected": expected.value, "new": new.value}
    progress = states.progress_for(new)
    if progress is not None:
        sets.append(sql.SQL("progress = %(progress)s"))
        params["progress"] = progress
    if states.is_terminal(new):
        sets.append(sql.SQL("finished_at = now()"))
    if error is not None:
        sets.append(sql.SQL("error = %(error)s"))
        params["error"] = Jsonb(error)
    if result is not None:
        sets.append(sql.SQL("result = %(result)s"))
        params["result"] = Jsonb(result)
    if warnings is not None:
        sets.append(sql.SQL("warnings = %(warnings)s"))
        params["warnings"] = Jsonb(warnings)
    for name, value in (fields or {}).items():
        if name not in _UPDATABLE_FIELDS:
            raise ValueError(f"field {name!r} cannot be set by a stage")
        sets.append(sql.SQL("{} = {}").format(sql.Identifier(name), sql.Placeholder(name)))
        params[name] = value

    query = sql.SQL("UPDATE jobs SET {} WHERE id = %(id)s AND status = %(expected)s RETURNING *").format(
        sql.SQL(", ").join(sets)
    )
    async with conn.transaction():
        if checkpoint is not None:
            await conn.execute(
                "INSERT INTO job_checkpoints (job_id, stage, data, worker_id) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (job_id, stage) DO UPDATE"
                " SET data = EXCLUDED.data, worker_id = EXCLUDED.worker_id, updated_at = now()",
                (job_id, expected.value, Jsonb(checkpoint), worker_id),
            )
        cur = await conn.execute(query, params)
        row = await cur.fetchone()
        if row is None:
            raise StaleJobState(f"job {job_id} is no longer in status {expected.value}")
        await insert_event(conn, job_id=job_id, event_type=event_type, from_status=expected.value,
                           to_status=new.value, worker_id=worker_id, detail=detail)
    return row
