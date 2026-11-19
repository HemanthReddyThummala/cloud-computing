"""Dead-letter auditor (ADR-12): consumes ``jobs.dlq.audit`` inside every worker process.

RabbitMQ dead-letters a job message by itself after repeated consumer crashes (``delivery_limit``) or
when a worker rejects a malformed message (``rejected``). The copy in ``jobs.dlq`` stays for manual
inspection; this auditor brings PostgreSQL in line so the job does not stay "running" forever.

Per message (prefetch 1, manual ack):
1. Parse it (``dead_letters.parse``). A worker copy (job already DEAD_LETTERED by the worker) -> ack.
   Unreadable or inconsistent metadata -> record the observation, ack; the job is never changed.
2. ``pg_try_advisory_lock(job)``. Busy (a live worker owns the job) -> pause, ``nack(requeue=True)``.
3. ONE transaction: lock the job row, decide (unknown / terminal / stale / apply), CAS transition to
   DEAD_LETTERED with its event when applying, upsert the audit row. Commit.
4. Delete the credential mailbox (idempotent; it also expires), then ack.

Temporary failures (busy lock, PostgreSQL unavailable) -> pause, then ``nack(requeue=True)``, which
RabbitMQ 4.4.1 does NOT count toward the delivery limit. ``reject(requeue=True)`` IS counted and is
never used. Consumer crashes are counted; at the audit queue's limit the message is dead-lettered
at-least-once to ``jobs.dlq.audit.parked`` instead of being lost.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from typing import Any

from aio_pika.abc import AbstractIncomingMessage, AbstractQueue

from notesagent.common import jobs_repo
from notesagent.common.broker import QUEUE_DLQ_AUDIT, BrokerClient
from notesagent.common.config import Settings
from notesagent.common.states import JobStatus
from notesagent.worker import dead_letters as dl_rules

log = logging.getLogger(__name__)


class DeadLetterAuditor:
    def __init__(self, settings: Settings, pool, broker: BrokerClient, worker_id: str,
                 lock_key: Callable[[uuid.UUID], int]) -> None:
        self.settings = settings
        self.pool = pool
        self.broker = broker
        self.worker_id = worker_id
        self.lock_key = lock_key
        self._queue: AbstractQueue | None = None
        self._tag: str | None = None

    async def start(self) -> None:
        assert self.broker.connection is not None
        channel = await self.broker.connection.channel(publisher_confirms=False)
        await channel.set_qos(prefetch_count=1)
        self._queue = await channel.get_queue(QUEUE_DLQ_AUDIT)
        self._tag = await self._queue.consume(self.on_message, no_ack=False)

    async def stop(self) -> None:
        if self._queue is not None and self._tag is not None:
            with contextlib.suppress(Exception):
                await self._queue.cancel(self._tag)

    # ------------------------------------------------------------ message handling
    async def on_message(self, message: AbstractIncomingMessage) -> None:
        try:
            await self._handle(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - PostgreSQL/broker trouble: nothing recorded, try later
            log.warning("dead letter could not be audited now; requeued",
                        extra={"message_id": message.message_id, "error_type": type(exc).__name__})
            await self._requeue_later(message)

    async def _requeue_later(self, message: AbstractIncomingMessage) -> None:
        """P4-D4/D7: pause, then nack(requeue=True) — not counted by the delivery limit."""
        await asyncio.sleep(self.settings.lock_retry_delay_sec)
        with contextlib.suppress(Exception):   # if the channel is gone the broker returns it anyway
            await message.nack(requeue=True)

    async def _handle(self, message: AbstractIncomingMessage) -> None:
        dl = dl_rules.parse(bytes(message.body), message.headers, message.message_id)
        log_extra = {"message_id": dl.message_id, "job_id": str(dl.job_id) if dl.job_id else None,
                     "death_reason": dl.death_reason, "source_queue": dl.source_queue}

        if dl.kind == "worker_copy":
            log.info("worker-initiated dead-letter copy; job already dead-lettered", extra=log_extra)
            await message.ack()
            return

        early = dl_rules.outcome_without_job(dl)
        if early is not None:
            async with self.pool.connection() as conn, conn.transaction():
                await self._record(conn, dl, early)
            log.warning("dead letter recorded without changing any job",
                        extra={**log_extra, "outcome": early, "problem": dl.problem})
            await message.ack()
            return

        assert dl.job_id is not None
        key = self.lock_key(dl.job_id)
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (key,))
            locked = (await cur.fetchone())["locked"]
            if locked:
                try:
                    outcome = await self._apply(conn, dl)
                finally:
                    with contextlib.suppress(Exception):
                        await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        if not locked:
            log.info("job locked by a live worker; dead letter deferred", extra=log_extra)
            await self._requeue_later(message)
            return

        log.log(logging.WARNING if outcome == dl_rules.APPLIED else logging.INFO,
                "dead letter audited", extra={**log_extra, "outcome": outcome})
        if outcome in (dl_rules.APPLIED, dl_rules.ALREADY_TERMINAL):
            await self._cleanup_credential(dl.job_id)
        await message.ack()                    # only after the transaction committed

    async def _apply(self, conn, dl: dl_rules.DeadLetter) -> str:
        """ONE transaction: decide on the locked row; transition + event (if applying) + audit row."""
        async with conn.transaction():
            job = await jobs_repo.get_job_for_update(conn, dl.job_id)
            outcome = dl_rules.decide(job, dl)
            if outcome == dl_rules.APPLIED:
                await jobs_repo.transition(
                    conn, dl.job_id, expected=JobStatus(job["status"]), new=JobStatus.DEAD_LETTERED,
                    worker_id=self.worker_id, event_type="dead_lettered_by_broker",
                    error=dl_rules.job_error(dl.death_reason),
                    detail={"reason": dl.death_reason, "source_queue": dl.source_queue,
                            "message_id": dl.message_id, "dedup_key": dl.dedup_key})
            await self._record(conn, dl, outcome, job)
        return outcome

    @staticmethod
    async def _record(conn, dl: dl_rules.DeadLetter, outcome: str, job: dict[str, Any] | None = None) -> None:
        detail = {**dl.detail, "problem": dl.problem}
        if job is not None:
            detail["job_status_seen"] = job["status"]
        await jobs_repo.upsert_dead_letter_audit(
            conn, dedup_key=dl.dedup_key, message_id=dl.message_id, job_id=dl.job_id,
            death_reason=dl.death_reason, source_queue=dl.source_queue, outcome=outcome, detail=detail)

    async def _cleanup_credential(self, job_id: uuid.UUID) -> None:
        try:
            await self.broker.delete_credential(job_id)
        except Exception as exc:  # noqa: BLE001 - it still expires via x-expires
            log.warning("credential mailbox cleanup failed; it will expire",
                        extra={"job_id": str(job_id), "error_type": type(exc).__name__})
