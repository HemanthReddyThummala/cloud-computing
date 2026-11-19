"""Job worker: consume, lock, run stages with checkpoints, ack only when durable.

Per delivered message:

1. Take a PostgreSQL *session* advisory lock for the job (``pg_try_advisory_lock``).
   Only one live worker can hold it; if this worker dies, PostgreSQL drops its session
   and releases the lock automatically.
   - Lock busy  -> another live worker owns the job: re-publish this (duplicate) message
     with a short delay and ack it, so it is never lost if the owner dies later.
2. Job already terminal -> duplicate delivery: clean up the credential mailbox, ack.
3. QUEUED -> TRANSCRIBING ("claimed"); otherwise the job is *resumed* at its current stage.
4. For each remaining stage: run it, then in ONE transaction save its checkpoint, move the
   job to the next status (compare-and-set) and write an audit event.
5. On a terminal status: delete the credential mailbox, then ack the message.

The message stays unacknowledged until step 5. If the worker is SIGKILLed at any point,
RabbitMQ redelivers it to another worker, which resumes from the last committed checkpoint.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import redis.asyncio as aioredis
from aio_pika.abc import AbstractIncomingMessage

from notesagent.common import jobs_repo, states
from notesagent.common.broker import QUEUE_WORK, BrokerClient
from notesagent.common.config import Settings
from notesagent.common.crypto import CredentialCipher, CredentialError
from notesagent.common.db import open_pool
from notesagent.common.errors import FatalStageError
from notesagent.common.states import JobStatus
from notesagent.worker.stages import StageContext, StageRegistry

log = logging.getLogger(__name__)


def advisory_lock_key(job_id: uuid.UUID) -> int:
    return int.from_bytes(job_id.bytes[:8], "big", signed=True)


_NOTION_HOSTS = ("notion.so", "notion.site")


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

    # ------------------------------------------------------------ lifecycle
    async def run(self, stop: asyncio.Event) -> None:
        self.pool = await open_pool(self.settings, name=f"worker-{self.worker_id}", min_size=1, max_size=3, wait=True)
        self.broker = BrokerClient(self.settings, name=f"worker-{self.worker_id}")
        await self.broker.start(wait=True)
        self.redis = aioredis.from_url(self.settings.redis_url.get_secret_value(),
                                       socket_timeout=2, socket_connect_timeout=2)
        assert self.broker.connection is not None
        consume_channel = await self.broker.connection.channel(publisher_confirms=False)
        await consume_channel.set_qos(prefetch_count=1)          # one job at a time per worker
        queue = await consume_channel.get_queue(QUEUE_WORK)
        consumer_tag = await queue.consume(self._on_message, no_ack=False)  # manual acknowledgements
        heartbeat = asyncio.create_task(self._heartbeat(stop))
        log.info("worker started", extra={"worker_id": self.worker_id, "stages": sorted(self.stages)})
        try:
            await stop.wait()
        finally:
            log.info("worker stopping", extra={"worker_id": self.worker_id})
            with contextlib.suppress(Exception):
                await queue.cancel(consumer_tag)
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
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

    # ------------------------------------------------------------ message handling
    async def _on_message(self, message: AbstractIncomingMessage) -> None:
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
        except Exception as exc:  # noqa: BLE001
            await self._retry_or_dead_letter(message, job_id, exc)

    async def _retry_or_dead_letter(self, message: AbstractIncomingMessage, job_id: uuid.UUID,
                                    exc: Exception) -> None:
        """Bounded handling of unexpected errors (INTERIM — Phase 2 adds classified exponential backoff).

        RabbitMQ 4 does NOT increase a quorum queue's delivery count when a consumer calls
        nack(requeue=True), so x-delivery-limit cannot bound explicit requeues. The worker
        therefore counts attempts itself in the ``x-processing-attempts`` header: it republishes
        a confirmed copy with the count + 1 and acks the original, and after
        QUEUE_DELIVERY_LIMIT attempts it rejects the message, which dead-letters it to jobs.dlq.
        """
        attempts = int((message.headers or {}).get("x-processing-attempts", 0)) + 1
        limit = self.settings.queue_delivery_limit
        log.exception("job processing error", extra={"job_id": str(job_id), "error_type": type(exc).__name__,
                                                       "attempt": attempts, "max_attempts": limit})
        try:
            if attempts >= limit:
                log.error("attempt limit reached; message dead-lettered to jobs.dlq",
                          extra={"job_id": str(job_id), "attempts": attempts})
                await message.reject(requeue=False)
                return
            await asyncio.sleep(self.settings.unexpected_error_retry_delay_sec)
            await self.broker.republish(message, extra_headers={"x-processing-attempts": attempts})
            await message.ack()
        except Exception as inner:  # noqa: BLE001 - broker trouble: leave the message unacked
            log.warning("could not reschedule failed job; broker will redeliver it",
                        extra={"job_id": str(job_id), "error_type": type(inner).__name__})

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
        await asyncio.sleep(self.settings.lock_busy_retry_delay_sec)
        await self.broker.republish(message, extra_headers={"x-deferrals": deferrals + 1})  # confirmed first
        await message.ack()                                                                 # then ack

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

        if job["status"] == JobStatus.QUEUED:
            job = await jobs_repo.transition(conn, job_id, expected=JobStatus.QUEUED, new=JobStatus.TRANSCRIBING,
                                             worker_id=self.worker_id, event_type="claimed", detail=delivery)
            log.info("job claimed", extra={"job_id": str(job_id), "worker_id": self.worker_id})
        else:
            await jobs_repo.insert_event(conn, job_id=job_id, event_type="resumed", from_status=job["status"],
                                         to_status=job["status"], worker_id=self.worker_id, detail=delivery)
            log.info("job resumed from checkpoint", extra={"job_id": str(job_id), "status": job["status"],
                                                            "worker_id": self.worker_id, **delivery})

        checkpoints = await jobs_repo.get_checkpoints(conn, job_id)
        while not states.is_terminal(job["status"]):
            job = await self._run_stage(conn, job_id, job, checkpoints)

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
                           _credential_reader=lambda: self._read_credential(job_id))
        log.info("stage started", extra={"job_id": str(job_id), "stage": status})
        try:
            output = await stage(ctx)
        except FatalStageError as exc:
            log.warning("stage failed permanently", extra={"job_id": str(job_id), "stage": status, "code": exc.code})
            return await jobs_repo.transition(conn, job_id, expected=status, new=JobStatus.FAILED,
                                              worker_id=self.worker_id,
                                              error={"code": exc.code, "message": exc.message})

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
