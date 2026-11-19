"""POST /jobs business logic: credential mailbox + transactional outbox + idempotency.

Order of operations (ADR-02/03/04):

1. Idempotency-Key already used?  -> replay the original 202 (or 422 if the body differs).
2. Encrypt the Notion token and publish it into ``cred.<job_id>`` — wait for the broker
   confirm. Failure -> 503, nothing created.
3. ONE PostgreSQL transaction: claim the idempotency key, insert the job (QUEUED), the
   outbox event and an audit event; NOTIFY the outbox publisher; COMMIT.
   Failure -> best-effort delete of the mailbox from step 2 (it would expire anyway) -> 503.

The PostgreSQL commit and the RabbitMQ publish are NOT atomic; the outbox publisher
turns the committed outbox row into a RabbitMQ message later (at least once).
"""
from __future__ import annotations

import contextlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from notesagent.common import jobs_repo
from notesagent.common.broker import BrokerClient
from notesagent.common.config import Settings
from notesagent.common.crypto import CredentialCipher
from notesagent.common.errors import ApiError
from notesagent.gateway.validation import SubmitJobRequest

log = logging.getLogger(__name__)

JOB_SUBMITTED = "job.submitted"


@dataclass(frozen=True)
class SubmitOutcome:
    job_id: str
    body: dict[str, Any]
    replayed: bool


class _KeyTakenConcurrently(Exception):
    pass


class JobSubmissionService:
    def __init__(self, pool: AsyncConnectionPool, broker: BrokerClient, cipher: CredentialCipher,
                 settings: Settings) -> None:
        self._pool = pool
        self._broker = broker
        self._cipher = cipher
        self._settings = settings

    async def submit(self, req: SubmitJobRequest, idempotency_key: str | None) -> SubmitOutcome:
        fingerprint: str | None = None
        if idempotency_key:
            fingerprint = self._cipher.fingerprint({
                "youtube_url": req.youtube_url,
                "notion_parent_id": str(req.notion_parent_id) if req.notion_parent_id else None,
                "notion_token": req.notion_token,
            })
            existing = await self._lookup_key(idempotency_key)
            if existing is not None:
                return self._replay(existing, fingerprint)

        job_id, event_id = uuid.uuid4(), uuid.uuid4()
        body = {"job_id": str(job_id), "status": "QUEUED", "video_id": req.video_id}

        # Step 2: credential mailbox (broker), confirmed before anything is committed.
        try:
            await self._broker.put_credential(job_id, self._cipher.seal(job_id, req.notion_token))
        except Exception as exc:  # noqa: BLE001
            log.warning("credential mailbox publish failed", extra={"job_id": str(job_id),
                                                                    "error_type": type(exc).__name__})
            raise ApiError(503, "BROKER_UNAVAILABLE",
                           "The message broker is unavailable. Please retry shortly.") from None

        # Step 3: one database transaction for job + outbox event (+ idempotency key).
        try:
            async with self._pool.connection(timeout=5) as conn:
                async with conn.transaction():
                    if idempotency_key:
                        await jobs_repo.delete_expired_idempotency_key(
                            conn, idempotency_key, self._settings.idempotency_key_ttl_hours)
                        claimed = await jobs_repo.claim_idempotency_key(
                            conn, key=idempotency_key, fingerprint=fingerprint or "", job_id=job_id, response=body)
                        if not claimed:
                            raise _KeyTakenConcurrently()
                    await jobs_repo.insert_job(conn, job_id=job_id, video_id=req.video_id,
                                               youtube_url=req.youtube_url, notion_parent_id=req.notion_parent_id)
                    await jobs_repo.insert_outbox(conn, event_id=event_id, job_id=job_id, event_type=JOB_SUBMITTED,
                                                  payload={"event_id": str(event_id), "event_type": JOB_SUBMITTED,
                                                           "job_id": str(job_id), "schema": 1})
                    await jobs_repo.insert_event(conn, job_id=job_id, event_type="submitted", to_status="QUEUED",
                                                 detail={"video_id": req.video_id,
                                                         "idempotency_key": bool(idempotency_key)})
                    await conn.execute("SELECT pg_notify(%s, %s)", (jobs_repo.OUTBOX_NOTIFY_CHANNEL, str(job_id)))
        except _KeyTakenConcurrently:
            await self._discard_mailbox(job_id)
            existing = await self._lookup_key(idempotency_key or "")
            if existing is None:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "A request with this Idempotency-Key is in progress; retry.") from None
            return self._replay(existing, fingerprint or "")
        except (psycopg.Error, PoolTimeout) as exc:
            await self._discard_mailbox(job_id)
            log.warning("job insert failed", extra={"job_id": str(job_id), "error_type": type(exc).__name__})
            raise ApiError(503, "DATABASE_UNAVAILABLE", "The job store is unavailable. Please retry shortly.") from None

        log.info("job accepted", extra={"job_id": str(job_id), "video_id": req.video_id})
        return SubmitOutcome(job_id=str(job_id), body=body, replayed=False)

    async def _lookup_key(self, key: str) -> dict[str, Any] | None:
        try:
            async with self._pool.connection(timeout=5) as conn:
                return await jobs_repo.find_idempotency_key(conn, key, self._settings.idempotency_key_ttl_hours)
        except (psycopg.Error, PoolTimeout):
            raise ApiError(503, "DATABASE_UNAVAILABLE", "The job store is unavailable. Please retry shortly.") from None

    @staticmethod
    def _replay(existing: dict[str, Any], fingerprint: str) -> SubmitOutcome:
        if existing["fingerprint"] != fingerprint:
            raise ApiError(422, "IDEMPOTENCY_KEY_REUSED",
                           "This Idempotency-Key was already used with a different request body.")
        return SubmitOutcome(job_id=str(existing["job_id"]), body=existing["response"], replayed=True)

    async def _discard_mailbox(self, job_id: uuid.UUID) -> None:
        with contextlib.suppress(Exception):
            await self._broker.delete_credential(job_id)
