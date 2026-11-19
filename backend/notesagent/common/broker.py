"""RabbitMQ topology, connection management and the per-job credential mailbox.

Topology v2 (declared idempotently by every service at start-up):

    exchange "jobs" (direct)   --job.work--> queue "jobs.work.v2"   (quorum, durable, x-delivery-limit,
                                                                      DLX jobs.dlx, at-least-once DL)
    exchange "jobs.retry" (direct) --<N>s--> queue "jobs.retry.<N>s" (quorum, x-message-ttl=N s; on expiry
                                                                      dead-letters to jobs/job.work)
    exchange "jobs.dlx" (fanout) ----------> queue "jobs.dlq"       (quorum, durable; inspection)
                                 ----------> queue "jobs.dlq.audit" (quorum; consumed by every worker's
                                             dead-letter auditor, ADR-12; at its delivery limit it
                                             dead-letters at-least-once via the default exchange to)
    queue "jobs.dlq.audit.parked"            (quorum, unlimited deliveries, not bound; inspection)
    queue "cred.<job_id>" (classic, durable, x-expires + x-message-ttl) - one per job

The Phase 1 queue "jobs.work" (v1) is never declared any more. Where it still exists it is
unbound and drained by ``notesagent.ops.work_queue_migration`` (scripts/migrate_work_queue.py)
while workers poll it in drain-only mode; it is deleted only once verified empty.

All publishes use publisher confirms: ``publish()`` only returns once the broker
has taken responsibility for the message (persisted for durable queues).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractIncomingMessage, AbstractRobustConnection
from aio_pika.exceptions import ChannelNotFoundEntity

from notesagent.common.config import Settings
from notesagent.common.errors import BrokerUnavailable

log = logging.getLogger(__name__)

TOPOLOGY_VERSION = 2

EXCHANGE_JOBS = "jobs"
EXCHANGE_RETRY = "jobs.retry"
EXCHANGE_DLX = "jobs.dlx"
QUEUE_WORK = "jobs.work.v2"          # current work queue
LEGACY_QUEUE_WORK = "jobs.work"      # Phase 1 queue: drain-only until migrated away
QUEUE_DLQ = "jobs.dlq"
QUEUE_DLQ_AUDIT = "jobs.dlq.audit"
QUEUE_DLQ_PARKED = "jobs.dlq.audit.parked"
ROUTING_KEY_WORK = "job.work"
# Header naming the outbox event type of every published job message (ADR-12): lets the
# dead-letter auditor tell worker-initiated DLQ copies from broker dead letters.
HEADER_EVENT_TYPE = "x-event-type"
CREDENTIAL_QUEUE_PREFIX = "cred."
DELAY_QUEUE_PREFIX = "jobs.retry."


@dataclass(frozen=True)
class CredentialPeek:
    exists: bool
    body: bytes | None = None


def credential_queue_name(job_id: uuid.UUID | str) -> str:
    return f"{CREDENTIAL_QUEUE_PREFIX}{job_id}"


def delay_queue_name(seconds: int) -> str:
    return f"{DELAY_QUEUE_PREFIX}{seconds}s"


def delay_routing_key(seconds: int) -> str:
    return f"{seconds}s"


def work_queue_arguments(settings: Settings) -> dict[str, Any]:
    """Arguments of jobs.work.v2. at-least-once dead-lettering requires overflow=reject-publish."""
    return {
        "x-queue-type": "quorum",
        "x-delivery-limit": settings.queue_delivery_limit,
        "x-dead-letter-exchange": EXCHANGE_DLX,
        "x-dead-letter-strategy": "at-least-once",
        "x-overflow": "reject-publish",
    }


def legacy_work_queue_arguments(settings: Settings) -> dict[str, Any]:
    """Arguments the Phase 1 queue was declared with (used only by tests and diagnostics)."""
    return {
        "x-queue-type": "quorum",
        "x-delivery-limit": settings.queue_delivery_limit,
        "x-dead-letter-exchange": EXCHANGE_DLX,
    }


def delay_queue_arguments(seconds: int) -> dict[str, Any]:
    """A message waits N seconds, then is dead-lettered (at-least-once) back to jobs/job.work."""
    return {
        "x-queue-type": "quorum",
        "x-message-ttl": seconds * 1000,
        "x-dead-letter-exchange": EXCHANGE_JOBS,
        "x-dead-letter-routing-key": ROUTING_KEY_WORK,
        "x-dead-letter-strategy": "at-least-once",
        "x-overflow": "reject-publish",
    }


def audit_queue_arguments(settings: Settings) -> dict[str, Any]:
    """jobs.dlq.audit (ADR-12). At its delivery limit (only consumer crashes count; nack(requeue)
    does not) a message is dead-lettered to the parking queue. at-least-once needs
    overflow=reject-publish, otherwise RabbitMQ silently falls back to at-most-once (verified)."""
    return {
        "x-queue-type": "quorum",
        "x-delivery-limit": settings.effective_audit_queue_delivery_limit,
        "x-dead-letter-exchange": "",                      # default exchange -> routing key = queue name
        "x-dead-letter-routing-key": QUEUE_DLQ_PARKED,
        "x-dead-letter-strategy": "at-least-once",
        "x-overflow": "reject-publish",
    }


def parked_queue_arguments() -> dict[str, Any]:
    """jobs.dlq.audit.parked: never discards. No TTL, no length limit, no DLX; -1 disables the
    delivery limit, so even crashing inspection tools cannot drop a parked message."""
    return {"x-queue-type": "quorum", "x-delivery-limit": -1}


async def declare_topology(channel: AbstractChannel, settings: Settings) -> dict[str, AbstractExchange]:
    """Declare topology v2. Never declares (or alters) the legacy Phase 1 queue."""
    jobs = await channel.declare_exchange(EXCHANGE_JOBS, ExchangeType.DIRECT, durable=True)
    retry = await channel.declare_exchange(EXCHANGE_RETRY, ExchangeType.DIRECT, durable=True)
    dlx = await channel.declare_exchange(EXCHANGE_DLX, ExchangeType.FANOUT, durable=True)
    work = await channel.declare_queue(QUEUE_WORK, durable=True, arguments=work_queue_arguments(settings))
    await work.bind(jobs, ROUTING_KEY_WORK)
    dlq = await channel.declare_queue(QUEUE_DLQ, durable=True, arguments={"x-queue-type": "quorum"})
    await dlq.bind(dlx, "")
    # Parking queue first: it is the dead-letter target of the audit queue.
    await channel.declare_queue(QUEUE_DLQ_PARKED, durable=True, arguments=parked_queue_arguments())
    audit = await channel.declare_queue(QUEUE_DLQ_AUDIT, durable=True, arguments=audit_queue_arguments(settings))
    await audit.bind(dlx, "")
    for seconds in settings.delay_queue_seconds:
        delay = await channel.declare_queue(delay_queue_name(seconds), durable=True,
                                            arguments=delay_queue_arguments(seconds))
        await delay.bind(retry, delay_routing_key(seconds))
    return {EXCHANGE_JOBS: jobs, EXCHANGE_RETRY: retry, EXCHANGE_DLX: dlx}


class BrokerClient:
    """One robust (auto-reconnecting) connection plus a confirm-mode channel."""

    def __init__(self, settings: Settings, name: str) -> None:
        self._settings = settings
        self._name = name
        self.connection: AbstractRobustConnection | None = None
        self.channel: AbstractChannel | None = None
        self._exchanges: dict[str, AbstractExchange] = {}
        self._reconnect_task: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self, *, wait: bool = True) -> None:
        """Connect now. With ``wait=False`` a failure is retried in the background."""
        try:
            await self._connect_once()
        except Exception as exc:  # noqa: BLE001 - any connection failure
            if wait:
                raise
            log.warning("broker not reachable yet; retrying in background",
                        extra={"error_type": type(exc).__name__})
            self._reconnect_task = asyncio.create_task(self._connect_forever())

    async def _connect_once(self) -> None:
        connection = await aio_pika.connect_robust(
            self._settings.rabbitmq_url.get_secret_value(),
            timeout=10,
            client_properties={"connection_name": self._name},
        )
        try:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            exchanges = await declare_topology(channel, self._settings)
        except Exception:
            await connection.close()
            raise
        self.connection, self.channel, self._exchanges = connection, channel, exchanges
        log.info("broker connected", extra={"connection_name": self._name})

    async def _connect_forever(self) -> None:
        delay = 1.0
        while True:
            await asyncio.sleep(delay)
            try:
                await self._connect_once()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("broker reconnect failed", extra={"error_type": type(exc).__name__})
                delay = min(delay * 2, 30.0)

    async def close(self) -> None:
        if self._reconnect_task:
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reconnect_task
        if self.connection and not self.connection.is_closed:
            with contextlib.suppress(Exception):
                await self.connection.close()

    @property
    def ready(self) -> bool:
        return (
            self.connection is not None and not self.connection.is_closed
            and self.channel is not None and not self.channel.is_closed
        )

    def _require_ready(self) -> AbstractRobustConnection:
        if not self.ready:
            raise BrokerUnavailable("RabbitMQ is not connected")
        assert self.connection is not None
        return self.connection

    # ------------------------------------------------------------ job events
    async def publish_to(self, exchange: str, routing_key: str, *, event_id: uuid.UUID | str,
                         job_id: uuid.UUID | str, body: bytes, headers: dict[str, Any] | None = None) -> None:
        """Publish a persistent job message to a declared exchange and wait for the broker's confirm.

        Unroutable messages raise (mandatory + on_return_raises), so a bad destination is never
        silently dropped.
        """
        self._require_ready()
        target = self._exchanges.get(exchange)
        if target is None:
            raise ValueError(f"unknown exchange {exchange!r}")
        message = Message(
            body,
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
            message_id=str(event_id),
            headers={"x-job-id": str(job_id), **(headers or {})},
        )
        await target.publish(message, routing_key=routing_key, timeout=10)

    async def publish_job_event(self, *, event_id: uuid.UUID | str, job_id: uuid.UUID | str, body: bytes,
                                headers: dict[str, Any] | None = None) -> None:
        """Publish a job message to the work queue (jobs / job.work)."""
        await self.publish_to(EXCHANGE_JOBS, ROUTING_KEY_WORK, event_id=event_id, job_id=job_id,
                              body=body, headers=headers)

    async def republish(self, message: AbstractIncomingMessage, *, exchange: str = EXCHANGE_JOBS,
                        routing_key: str = ROUTING_KEY_WORK, extra_headers: dict[str, Any] | None = None) -> None:
        """Publish a confirmed copy of a delivered job message, KEEPING its message_id (the retry
        authorisation travels with the copy). Used before acking the original. Broker bookkeeping
        headers (delivery count, dead-letter history) are not copied."""
        headers = {k: v for k, v in (message.headers or {}).items()
                   if not k.startswith(("x-delivery", "x-death", "x-first-death", "x-last-death"))}
        headers.update(extra_headers or {})
        job_id = headers.get("x-job-id", "")
        await self.publish_to(exchange, routing_key, event_id=message.message_id or uuid.uuid4(), job_id=job_id,
                              body=message.body, headers=headers)

    # ------------------------------------------------------------ credential mailbox (ADR-03)
    async def put_credential(self, job_id: uuid.UUID, blob: bytes) -> None:
        """Create ``cred.<job_id>`` and store the encrypted token in it (publisher-confirmed)."""
        connection = self._require_ready()
        ttl_ms = self._settings.credential_ttl_ms
        name = credential_queue_name(job_id)
        async with connection.channel(publisher_confirms=True, on_return_raises=True) as ch:
            await ch.declare_queue(
                name,
                durable=True,
                arguments={"x-queue-type": "classic", "x-expires": ttl_ms, "x-message-ttl": ttl_ms},
            )
            await ch.default_exchange.publish(
                Message(blob, content_type="application/octet-stream", delivery_mode=DeliveryMode.PERSISTENT,
                        message_id=str(job_id)),
                routing_key=name,
                timeout=10,
            )

    async def peek_credential(self, job_id: uuid.UUID) -> CredentialPeek:
        """Read the credential WITHOUT consuming it: basic.get (manual ack), copy, nack(requeue=True).

        The SAME message goes back to the queue — nothing is republished, so its original
        per-message TTL (x-message-ttl, set at submission) is kept. If this process dies between
        get and nack, the broker returns the unacknowledged message when the channel closes.

        Returns ``exists=False`` if the mailbox queue does not exist (expired or deleted), and
        ``exists=True, body=None`` if the queue exists but its message is not currently ready —
        e.g. it is still held (unacked) by a worker whose crash the broker has not detected yet.
        """
        connection = self._require_ready()
        ch = await connection.channel(publisher_confirms=False)
        try:
            try:
                queue = await ch.declare_queue(credential_queue_name(job_id), passive=True)
            except ChannelNotFoundEntity:
                return CredentialPeek(exists=False)
            message = await queue.get(no_ack=False, fail=False, timeout=5)
            if message is None:
                return CredentialPeek(exists=True)
            body = bytes(message.body)
            await message.nack(requeue=True)
            return CredentialPeek(exists=True, body=body)
        finally:
            with contextlib.suppress(Exception):
                await ch.close()  # also returns the message if nack was never sent

    async def delete_credential(self, job_id: uuid.UUID) -> None:
        connection = self._require_ready()
        async with connection.channel(publisher_confirms=False) as ch:
            await ch.queue_delete(credential_queue_name(job_id))
