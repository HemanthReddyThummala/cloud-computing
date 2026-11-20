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


async def get_job_for_update(conn: AsyncConnection, job_id: uuid.UUID) -> dict[str, Any] | None:
    """Read and row-lock a job (inside a transaction) so the decision and the write see one state."""
    cur = await conn.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
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
                        payload: dict[str, Any], destination_exchange: str = "jobs",
                        destination_routing_key: str = "job.work") -> None:
    """Queue an event for the outbox publisher. The destination defaults to the work queue;
    the database restricts exchanges to jobs / jobs.retry / jobs.dlx (migration 003)."""
    await conn.execute(
        "INSERT INTO outbox (event_id, job_id, event_type, payload, destination_exchange, destination_routing_key)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (event_id, job_id, event_type, Jsonb(payload), destination_exchange, destination_routing_key),
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


# ---------------------------------------------------------------- retries and dead-lettering (Part 3, ADR-11)
_ATTEMPT_COLUMNS = {"attempts", "infra_attempts"}
EXCHANGE_RETRY = "jobs.retry"
EXCHANGE_DLX = "jobs.dlx"


async def _bump_attempts(conn: AsyncConnection, job_id: uuid.UUID, *, expected: JobStatus, column: str,
                         attempt: int, retry: dict[str, Any] | None) -> dict[str, Any]:
    """CAS: only if the job is still in ``expected`` and the counter still holds ``attempt - 1``."""
    if column not in _ATTEMPT_COLUMNS:
        raise ValueError(f"unknown attempt column {column!r}")
    query = sql.SQL("UPDATE jobs SET {col} = %(attempt)s, retry = %(retry)s"
                    " WHERE id = %(id)s AND status = %(expected)s AND {col} = %(previous)s RETURNING *").format(
        col=sql.Identifier(column))
    cur = await conn.execute(query, {"attempt": attempt, "retry": Jsonb(retry) if retry is not None else None,
                                     "id": job_id, "expected": JobStatus(expected).value, "previous": attempt - 1})
    row = await cur.fetchone()
    if row is None:
        raise StaleJobState(f"job {job_id} changed while scheduling a retry")
    return row


async def schedule_retry(conn: AsyncConnection, job_id: uuid.UUID, *, expected: JobStatus, column: str, attempt: int,
                         retry_doc: dict[str, Any], event_id: uuid.UUID, routing_key: str,
                         worker_id: str | None) -> dict[str, Any]:
    """ONE transaction: counter + retry state + audit event + outbox row (delay queue) + NOTIFY.
    Any failure rolls everything back — nothing is counted and nothing is published."""
    async with conn.transaction():
        row = await _bump_attempts(conn, job_id, expected=expected, column=column, attempt=attempt, retry=retry_doc)
        await insert_event(conn, job_id=job_id, event_type="retry_scheduled", from_status=row["status"],
                           to_status=row["status"], worker_id=worker_id,
                           detail={"event_id": str(event_id), "attempt": attempt, "budget": retry_doc.get("budget"),
                                   "reason_code": retry_doc.get("reason_code"), "delay_sec": retry_doc.get("delay_sec"),
                                   # ADR-21: sanitized provider status (e.g. 503 / UNAVAILABLE), never text
                                   "http_status": retry_doc.get("http_status"),
                                   "provider_code": retry_doc.get("provider_code"),
                                   "backoff": retry_doc.get("backoff")})
        await insert_outbox(conn, event_id=event_id, job_id=job_id, event_type="job.retry",
                            payload={"event_id": str(event_id), "event_type": "job.retry", "job_id": str(job_id),
                                     "schema": 1},
                            destination_exchange=EXCHANGE_RETRY, destination_routing_key=routing_key)
        await conn.execute("SELECT pg_notify(%s, %s)", (OUTBOX_NOTIFY_CHANNEL, str(job_id)))
    return row


async def dead_letter(conn: AsyncConnection, job_id: uuid.UUID, *, expected: JobStatus, column: str, attempt: int,
                      error: dict[str, Any], event_id: uuid.UUID, worker_id: str | None) -> dict[str, Any]:
    """ONE transaction: counter + DEAD_LETTERED (CAS) + audit event + outbox row (DLQ copy) + NOTIFY."""
    async with conn.transaction():
        await _bump_attempts(conn, job_id, expected=expected, column=column, attempt=attempt, retry=None)
        row = await transition(conn, job_id, expected=expected, new=JobStatus.DEAD_LETTERED, worker_id=worker_id,
                               event_type="dead_lettered", error=error,
                               detail={"attempt": attempt, "last_error_code": error.get("last_error_code"),
                                       "last_http_status": error.get("last_http_status"),
                                       "last_provider_code": error.get("last_provider_code")})
        await insert_outbox(conn, event_id=event_id, job_id=job_id, event_type="job.dead_lettered",
                            payload={"event_id": str(event_id), "event_type": "job.dead_lettered",
                                     "job_id": str(job_id), "last_error_code": error.get("last_error_code"),
                                     "schema": 1},
                            destination_exchange=EXCHANGE_DLX, destination_routing_key="dead")
        await conn.execute("SELECT pg_notify(%s, %s)", (OUTBOX_NOTIFY_CHANNEL, str(job_id)))
    return row


# ---------------------------------------------------------------- dead-letter audit (ADR-12)
async def upsert_dead_letter_audit(conn: AsyncConnection, *, dedup_key: str, message_id: str | None,
                                   job_id: uuid.UUID | None, death_reason: str, source_queue: str | None,
                                   outcome: str, detail: dict[str, Any]) -> bool:
    """Record one observation of a dead letter. The first observation inserts the row (its outcome is
    kept); a repeated one only increments ``seen_count`` and notes the latest outcome. Returns True
    for the first observation. Never stores message bodies."""
    cur = await conn.execute(
        "INSERT INTO dead_letter_audit (dedup_key, message_id, job_id, death_reason, source_queue, outcome, detail)"
        " VALUES (%(key)s, %(mid)s, %(job)s, %(reason)s, %(queue)s, %(outcome)s, %(detail)s)"
        " ON CONFLICT (dedup_key) DO UPDATE SET seen_count = dead_letter_audit.seen_count + 1,"
        "   last_seen_at = now(),"
        "   detail = dead_letter_audit.detail || jsonb_build_object('last_outcome', EXCLUDED.outcome)"
        " RETURNING (xmax = 0) AS inserted",
        {"key": dedup_key, "mid": message_id, "job": job_id, "reason": death_reason, "queue": source_queue,
         "outcome": outcome, "detail": Jsonb(detail)},
    )
    return bool((await cur.fetchone())["inserted"])


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
