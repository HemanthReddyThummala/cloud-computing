"""Job worker: consume, lock, run stages with checkpoints, retry/dead-letter via the outbox.

Per delivered message:

1. Take a PostgreSQL *session* advisory lock for the job (``pg_try_advisory_lock``). Only one
   live worker can hold it; if this worker dies, PostgreSQL drops its session and the lock.
   - Lock busy -> another live worker owns the job: send this (duplicate) message through the
     lock delay queue (confirmed) and ack it — nothing is counted.
2. Job terminal -> duplicate delivery: clean up the credential mailbox, ack.
3. Retry gating (ADR-11): if a retry is scheduled (``jobs.retry`` set), only the message whose
   ``message_id`` equals ``retry.event_id`` may run; any other message is stale -> ack and drop.
   The authorised message is re-delayed (hop, not counted) while ``next_attempt_at`` is in the future.
4. QUEUED -> TRANSCRIBING ("claimed"); otherwise the job is *resumed* at its current stage.
5. Each stage: run it; on success checkpoint + CAS transition in one transaction. On failure the
   error is classified: fatal -> FAILED; retryable -> ONE transaction that counts the attempt and
   either schedules a retry (outbox row -> delay queue, new event id) or dead-letters the job
   (outbox row -> DLQ copy). Then the message is acked.
6. Terminal -> delete the credential mailbox, ack.

Each worker also runs the dead-letter auditor (``dlq_audit``, ADR-12) on its own channel: it
reconciles PostgreSQL when RabbitMQ itself dead-letters a job message.

If the job state cannot be recorded (PostgreSQL / broker problem), nothing is counted: a
confirmed copy goes through the lock delay queue and only then is the original acked (P3-D4).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import redis.asyncio as aioredis
from aio_pika.abc import AbstractIncomingMessage
from aio_pika.exceptions import ChannelNotFoundEntity

from notesagent.common import jobs_repo, states
from notesagent.common.broker import (
    EXCHANGE_RETRY,
    LEGACY_QUEUE_WORK,
    QUEUE_WORK,
    BrokerClient,
    delay_routing_key,
)
from notesagent.common.config import Settings
from notesagent.common.crypto import CredentialCipher, CredentialError
from notesagent.common.db import open_pool
from notesagent.common.errors import FatalStageError, Provider
from notesagent.common.http_errors import classify_exception
from notesagent.common.ratelimit import RateLimiter
from notesagent.common.states import JobStatus
from notesagent.worker import retry_policy
from notesagent.worker.dlq_audit import DeadLetterAuditor
from notesagent.worker.stages import StageContext, StageRegistry

log = logging.getLogger(__name__)


class _LegacyQueueGone(Exception):
    pass


class _RetryScheduled(Exception):
    """Control flow: a retry was committed; the current message must now be acked."""


def advisory_lock_key(job_id: uuid.UUID) -> int:
    return int.from_bytes(job_id.bytes[:8], "big", signed=True)


_NOTION_HOSTS = ("notion.so", "notion.site", "notion.com")   # notion.com: Notion's current domain (ADR-18)


def validate_publish_result(result: Any) -> dict[str, Any]:
    """A job may only complete with a real, absolute https Notion URL and sane frame counts (spec §5)."""
    def invalid(reason: str) -> FatalStageError:
        return FatalStageError("PUBLISH_RESULT_INVALID", f"Publishing did not produce a valid result: {reason}.")

    if not isinstance(result, dict):
        raise invalid("no result")
    url = result.get("notion_page_url")
    parts = urlsplit(url) if isinstance(url, str) else None
    host = (parts.hostname or "").lower() if parts else ""
    if not parts or parts.scheme != "https" or not any(host == h or host.endswith("." + h) for h in _NOTION_HOSTS):
        raise invalid("notion_page_url is not an absolute https Notion URL")
    requested, published = result.get("frames_requested"), result.get("frames_published")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (requested, published)):
        raise invalid("frame counts must be non-negative integers")
    if published > requested:
        raise invalid("frames_published exceeds frames_requested")
    return {"notion_page_url": url, "frames_requested": requested, "frames_published": published}


class JobWorker:
    def __init__(self, settings: Settings, stages: StageRegistry) -> None:
        self.settings = settings
        self.stages = stages
        self.worker_id = settings.effective_worker_id
        self.cipher = CredentialCipher(settings.job_payload_encryption_key.get_secret_value()
                                       if settings.job_payload_encryption_key else "")
        self.pool = None
        self.broker: BrokerClient | None = None
        self.redis: aioredis.Redis | None = None
        self.limiter: RateLimiter | None = None
        self._slot = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle
    async def run(self, stop: asyncio.Event) -> None:
        self.pool = await open_pool(self.settings, name=f"worker-{self.worker_id}", min_size=1, max_size=3, wait=True)
        self.broker = BrokerClient(self.settings, name=f"worker-{self.worker_id}")
        await self.broker.start(wait=True)
        self.redis = aioredis.from_url(self.settings.redis_url.get_secret_value(),
                                       socket_timeout=2, socket_connect_timeout=2)
        self.limiter = RateLimiter(self.redis, self.settings)     # ADR-13, shared by all workers via Redis
        assert self.broker.connection is not None
        consume_channel = await self.broker.connection.channel(publisher_confirms=False)
        await consume_channel.set_qos(prefetch_count=1)          # one job at a time per worker
        queue = await consume_channel.get_queue(QUEUE_WORK)
        consumer_tag = await queue.consume(self._on_message, no_ack=False)  # manual acknowledgements
        # ADR-12: reconcile broker-initiated dead letters (own channel; independent of job processing).
        auditor = DeadLetterAuditor(self.settings, self.pool, self.broker, self.worker_id, advisory_lock_key)
        await auditor.start()
        heartbeat = asyncio.create_task(self._heartbeat(stop))
        drain = None
        if await self._legacy_queue_exists():
            drain = asyncio.create_task(self._drain_legacy_queue(stop))
        log.info("worker started", extra={"worker_id": self.worker_id, "stages": sorted(self.stages),
                                          "work_queue": QUEUE_WORK, "draining_legacy": drain is not None})
        try:
            await stop.wait()
        finally:
            log.info("worker stopping", extra={"worker_id": self.worker_id})
            with contextlib.suppress(Exception):
                await queue.cancel(consumer_tag)
            await auditor.stop()
            for task in (heartbeat, drain):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            await self.broker.close()
            await self.pool.close()
            with contextlib.suppress(Exception):
                await self.redis.aclose()

    async def _heartbeat(self, stop: asyncio.Event) -> None:
        key = f"workers:heartbeat:{self.worker_id}"
        while not stop.is_set():
            with contextlib.suppress(OSError):
                Path(self.settings.worker_heartbeat_file).write_text(str(time.time()))
            try:
                await self.redis.set(key, json.dumps({"worker_id": self.worker_id, "ts": time.time()}),
                                     ex=self.settings.worker_heartbeat_ttl_sec)
            except Exception as exc:  # noqa: BLE001 - Redis is not needed to process jobs
                log.debug("heartbeat write failed", extra={"error_type": type(exc).__name__})
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), 5)

    # ------------------------------------------------------------ legacy queue drain (topology migration)
    async def _legacy_queue_exists(self) -> bool:
        ch = await self.broker.connection.channel(publisher_confirms=False)
        try:
            await ch.declare_queue(LEGACY_QUEUE_WORK, passive=True)
            return True
        except ChannelNotFoundEntity:
            return False
        finally:
            with contextlib.suppress(Exception):
                await ch.close()

    async def _drain_legacy_queue(self, stop: asyncio.Event) -> None:
        """Drain-only processing of the Phase 1 queue ``jobs.work`` until it is deleted (basic.get polling)."""
        log.info("draining legacy work queue", extra={"queue": LEGACY_QUEUE_WORK})
        while not stop.is_set():
            try:
                handled = await self._drain_one_legacy_message()
            except _LegacyQueueGone:
                log.info("legacy work queue no longer exists; drain finished", extra={"queue": LEGACY_QUEUE_WORK})
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep draining; the message stays in the queue
                log.warning("legacy drain attempt failed", extra={"error_type": type(exc).__name__})
                handled = False
            if not handled:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), self.settings.legacy_drain_interval_sec)

    async def _drain_one_legacy_message(self) -> bool:
        ch = await self.broker.connection.channel(publisher_confirms=False)
        try:
            try:
                queue = await ch.declare_queue(LEGACY_QUEUE_WORK, passive=True)
            except ChannelNotFoundEntity:
                raise _LegacyQueueGone() from None
            message = await queue.get(no_ack=False, fail=False, timeout=5)
            if message is None:
                return False
            log.info("processing message from legacy queue", extra={"message_id": message.message_id})
            async with self._slot:
                await self._process_message(message)
            return True
        finally:
            with contextlib.suppress(Exception):
                await ch.close()  # an unacked message is returned to the legacy queue

    # ------------------------------------------------------------ message handling
    async def _on_message(self, message: AbstractIncomingMessage) -> None:
        async with self._slot:                    # one job at a time, whichever queue it came from
            await self._process_message(message)

    async def _process_message(self, message: AbstractIncomingMessage) -> None:
        try:
            job_id = uuid.UUID(json.loads(message.body)["job_id"])
        except Exception:  # noqa: BLE001
            log.error("malformed job message rejected to DLQ", extra={"message_id": message.message_id})
            await message.reject(requeue=False)  # quorum queue dead-letters it to jobs.dlq
            return
        try:
            await self._handle(message, job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - job state could not be recorded (DB/broker/infra)
            await self._delay_without_counting(message, job_id, exc)

    async def _delay_without_counting(self, message: AbstractIncomingMessage, job_id: uuid.UUID,
                                      exc: BaseException) -> None:
        """P3-D4: the job state could not be read/recorded (e.g. PostgreSQL unavailable, lock not
        obtainable). Count nothing. Send a confirmed copy (same message_id) through the lock delay
        queue, and only then ack the original. If even that fails, do not ack: after a throttle the
        message is returned to the broker (no hot loop); a lost connection makes the broker redeliver
        it, which counts toward x-delivery-limit."""
        delay = self.settings.lock_retry_delay_sec
        log.warning("job state could not be recorded; message delayed without counting an attempt",
                    extra={"job_id": str(job_id), "error_type": type(exc).__name__, "delay_sec": delay})
        try:
            infra_delays = int((message.headers or {}).get("x-infra-delays", 0)) + 1
            await self.broker.republish(message, exchange=EXCHANGE_RETRY, routing_key=delay_routing_key(delay),
                                        extra_headers={"x-infra-delays": infra_delays})
            await message.ack()
        except Exception as inner:  # noqa: BLE001
            log.error("could not delay the message; leaving it unacknowledged for broker redelivery",
                      extra={"job_id": str(job_id), "error_type": type(inner).__name__})
            await asyncio.sleep(delay)
            with contextlib.suppress(Exception):
                await message.nack(requeue=True)

    async def _handle(self, message: AbstractIncomingMessage, job_id: uuid.UUID) -> None:
        key = advisory_lock_key(job_id)
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (key,))
            if not (await cur.fetchone())["locked"]:
                await self._defer_duplicate(conn, message, job_id)
                return
            try:
                await self._process(conn, message, job_id)
            finally:
                with contextlib.suppress(Exception):
                    await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))

    async def _defer_duplicate(self, conn, message: AbstractIncomingMessage, job_id: uuid.UUID) -> None:
        """Lock busy: another live worker owns the job. Delay-queue deferral, nothing counted."""
        job = await jobs_repo.get_job(conn, job_id)
        if job is None or states.is_terminal(job["status"]):
            log.info("duplicate message for finished job dropped", extra={"job_id": str(job_id)})
            await message.ack()
            return
        deferrals = int((message.headers or {}).get("x-deferrals", 0))
        if deferrals == 0:
            await jobs_repo.insert_event(conn, job_id=job_id, event_type="duplicate_deferred",
                                         from_status=job["status"], to_status=job["status"],
                                         worker_id=self.worker_id, detail={"reason": "job locked by another worker"})
        log.info("job locked by another worker; deferring duplicate message",
                 extra={"job_id": str(job_id), "deferrals": deferrals})
        await self.broker.republish(message, exchange=EXCHANGE_RETRY,
                                    routing_key=delay_routing_key(self.settings.lock_retry_delay_sec),
                                    extra_headers={"x-deferrals": deferrals + 1})   # confirmed first
        await message.ack()                                                          # then ack

    async def _process(self, conn, message: AbstractIncomingMessage, job_id: uuid.UUID) -> None:
        headers = message.headers or {}
        delivery = {"delivery_count": int(headers.get("x-delivery-count", 0) or 0),
                    "redelivered": bool(message.redelivered)}
        job = await jobs_repo.get_job(conn, job_id)
        if job is None:
            log.warning("message for unknown job acknowledged", extra={"job_id": str(job_id)})
            await message.ack()
            return
        if states.is_terminal(job["status"]):
            await self._cleanup_credential(job_id)
            log.info("duplicate message for finished job dropped", extra={"job_id": str(job_id)})
            await message.ack()
            return

        retry = job["retry"]
        if retry:
            # Only the scheduled retry message (its event id) may run the next attempt.
            if message.message_id != retry.get("event_id"):
                log.info("stale message dropped: a different retry is scheduled",
                         extra={"job_id": str(job_id), "message_id": message.message_id})
                await message.ack()
                return
            remaining = retry_policy.seconds_until(retry, datetime.now(timezone.utc))
            if remaining > 0:
                bucket = retry_policy.choose_bucket(remaining, self.settings.delay_queue_seconds)
                log.info("early retry delivery re-delayed",
                         extra={"job_id": str(job_id), "message_id": message.message_id,
                                "remaining_sec": round(remaining, 3), "bucket_sec": bucket})
                await self.broker.republish(message, exchange=EXCHANGE_RETRY, routing_key=delay_routing_key(bucket))
                await message.ack()
                return
            await jobs_repo.insert_event(conn, job_id=job_id, event_type="retry_started", from_status=job["status"],
                                         to_status=job["status"], worker_id=self.worker_id,
                                         detail={"attempt": retry.get("attempt"), **delivery})
            log.info("retry started", extra={"job_id": str(job_id), "attempt": retry.get("attempt")})
        elif job["status"] == JobStatus.QUEUED:
            job = await jobs_repo.transition(conn, job_id, expected=JobStatus.QUEUED, new=JobStatus.TRANSCRIBING,
                                             worker_id=self.worker_id, event_type="claimed", detail=delivery)
            log.info("job claimed", extra={"job_id": str(job_id), "worker_id": self.worker_id})
        else:
            await jobs_repo.insert_event(conn, job_id=job_id, event_type="resumed", from_status=job["status"],
                                         to_status=job["status"], worker_id=self.worker_id, detail=delivery)
            log.info("job resumed from checkpoint", extra={"job_id": str(job_id), "status": job["status"],
                                                            "worker_id": self.worker_id, **delivery})

        checkpoints = await jobs_repo.get_checkpoints(conn, job_id)
        try:
            while not states.is_terminal(job["status"]):
                job = await self._run_stage(conn, job_id, job, checkpoints)
        except _RetryScheduled:
            await message.ack()  # the retry (outbox row) is durably committed
            return

        await self._cleanup_credential(job_id)
        await message.ack()  # only now: the terminal state is durably committed
        log.info("job finished", extra={"job_id": str(job_id), "status": job["status"]})

    async def _run_stage(self, conn, job_id: uuid.UUID, job: dict[str, Any],
                         checkpoints: dict[str, dict[str, Any]]) -> dict[str, Any]:
        status = JobStatus(job["status"])
        stage = self.stages.get(status)
        if stage is None:
            log.error("pipeline stage not available in this build", extra={"job_id": str(job_id), "stage": status})
            return await jobs_repo.transition(
                conn, job_id, expected=status, new=JobStatus.FAILED, worker_id=self.worker_id,
                error={"code": "PIPELINE_STAGE_UNAVAILABLE",
                       "message": f"The {status.value} stage is not available in this build of the worker."})

        ctx = StageContext(job_id=job_id, job=job, checkpoints=dict(checkpoints), worker_id=self.worker_id,
                           _credential_reader=lambda: self._read_credential(job_id), limiter=self.limiter)
        log.info("stage started", extra={"job_id": str(job_id), "stage": status})
        try:
            output = await stage(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified below; upstream text is never logged
            return await self._handle_stage_failure(conn, job_id, job, status, exc)

        warnings = list(job["warnings"]) + list(output.warnings)
        if status is JobStatus.PUBLISHING:
            try:
                result = validate_publish_result(output.result)
            except FatalStageError as exc:
                log.error("publishing stage returned an invalid result", extra={"job_id": str(job_id)})
                return await jobs_repo.transition(conn, job_id, expected=status, new=JobStatus.FAILED,
                                                  worker_id=self.worker_id,
                                                  error={"code": exc.code, "message": exc.message})
            frames_omitted = result["frames_published"] < result["frames_requested"]
            new = JobStatus.COMPLETED_WITH_WARNINGS if (warnings or frames_omitted) else JobStatus.COMPLETED
            updated = await jobs_repo.transition(
                conn, job_id, expected=status, new=new, worker_id=self.worker_id, checkpoint=output.checkpoint,
                result=result, warnings=warnings, fields=output.job_fields)
        else:
            updated = await jobs_repo.transition(
                conn, job_id, expected=status, new=states.next_stage(status), worker_id=self.worker_id,
                checkpoint=output.checkpoint, warnings=warnings if output.warnings else None,
                fields=output.job_fields)
        checkpoints[status.value] = output.checkpoint
        log.info("stage checkpointed", extra={"job_id": str(job_id), "stage": status, "next": updated["status"]})
        return updated

    async def _handle_stage_failure(self, conn, job_id: uuid.UUID, job: dict[str, Any], status: JobStatus,
                                    exc: BaseException) -> dict[str, Any]:
        """Fatal -> FAILED. Retryable -> one transaction that counts the attempt and schedules a retry
        (raises _RetryScheduled) or dead-letters the job (returns the terminal row)."""
        error = classify_exception(Provider.GENERIC, exc)
        provider_status = retry_policy.provider_status(error)       # ADR-21: e.g. 503 / UNAVAILABLE
        if not error.retryable:
            log.warning("stage failed permanently",
                        extra={"job_id": str(job_id), "stage": status, "code": error.code, **provider_status})
            return await jobs_repo.transition(conn, job_id, expected=status, new=JobStatus.FAILED,
                                              worker_id=self.worker_id,
                                              error={"code": error.code, "message": error.message})

        if self.limiter is not None:
            # ADR-13: a provider 429 / exhausted quota becomes a cooldown shared by every worker
            # (honours Retry-After). Best effort; the retry below is scheduled either way.
            await self.limiter.report(error)
        plan = retry_policy.plan_failure(job, error, self.settings, datetime.now(timezone.utc))
        if isinstance(plan, retry_policy.DeadLetterPlan):
            log.error("retry budget exhausted; job dead-lettered",
                      extra={"job_id": str(job_id), "stage": status, "code": error.code, "attempt": plan.attempt,
                             "budget": error.budget.value, **provider_status})
            return await jobs_repo.dead_letter(conn, job_id, expected=status, column=plan.attempt_column,
                                               attempt=plan.attempt, error=plan.error, event_id=plan.event_id,
                                               worker_id=self.worker_id)

        await jobs_repo.schedule_retry(conn, job_id, expected=status, column=plan.attempt_column,
                                       attempt=plan.attempt, retry_doc=plan.retry_doc, event_id=plan.event_id,
                                       routing_key=delay_routing_key(plan.routing_key_seconds),
                                       worker_id=self.worker_id)
        log.warning("stage failed; retry scheduled",
                    extra={"job_id": str(job_id), "stage": status, "code": error.code, "attempt": plan.attempt,
                           "max_attempts": plan.max_attempts, "delay_sec": plan.delay_sec,
                           "budget": error.budget.value, "event_id": str(plan.event_id),
                           "backoff": plan.retry_doc.get("backoff"), **provider_status})
        raise _RetryScheduled()

    # ------------------------------------------------------------ credential mailbox
    async def _read_credential(self, job_id: uuid.UUID) -> str:
        deadline = time.monotonic() + self.settings.credential_read_wait_sec
        delay, warned = 0.5, False
        while True:
            peek = await self.broker.peek_credential(job_id)
            if not peek.exists:
                raise FatalStageError("NOTION_CREDENTIALS_UNAVAILABLE",
                                      "The Notion credential for this job expired or is missing. Please resubmit the job.")
            if peek.body is not None:
                break
            # Queue exists but the message is not ready: most likely still unacked by a worker
            # that crashed moments ago. The broker returns it once it notices the dead connection.
            if time.monotonic() >= deadline:
                raise FatalStageError("NOTION_CREDENTIALS_UNAVAILABLE",
                                      "The Notion credential for this job could not be read. Please resubmit the job.")
            if not warned:
                log.warning("credential mailbox temporarily empty; waiting for the broker to return it",
                            extra={"job_id": str(job_id), "max_wait_sec": self.settings.credential_read_wait_sec})
                warned = True
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)
        try:
            return self.cipher.open(job_id, peek.body)
        except CredentialError:
            raise FatalStageError("NOTION_CREDENTIALS_UNAVAILABLE",
                                  "The Notion credential for this job could not be read. Please resubmit the job.") from None

    async def _cleanup_credential(self, job_id: uuid.UUID) -> None:
        try:
            await self.broker.delete_credential(job_id)
        except Exception as exc:  # noqa: BLE001 - it still expires via x-expires
            log.warning("credential mailbox cleanup failed; it will expire",
                        extra={"job_id": str(job_id), "error_type": type(exc).__name__})
