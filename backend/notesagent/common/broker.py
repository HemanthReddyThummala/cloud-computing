"""RabbitMQ topology, connection management and the per-job credential mailbox.

Topology (declared idempotently by every service at start-up):

    exchange "jobs" (direct) --job.work--> queue "jobs.work"  (quorum, durable,
                                                               x-delivery-limit, DLX)
    exchange "jobs.dlx" (fanout) --------> queue "jobs.dlq"   (quorum, durable)
    queue "cred.<job_id>" (classic, durable, x-expires + x-message-ttl) - one per job

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

EXCHANGE_JOBS = "jobs"
EXCHANGE_DLX = "jobs.dlx"
QUEUE_WORK = "jobs.work"
QUEUE_DLQ = "jobs.dlq"
ROUTING_KEY_WORK = "job.work"
CREDENTIAL_QUEUE_PREFIX = "cred."


@dataclass(frozen=True)
class CredentialPeek:
    exists: bool
    body: bytes | None = None


def credential_queue_name(job_id: uuid.UUID | str) -> str:
    return f"{CREDENTIAL_QUEUE_PREFIX}{job_id}"


def work_queue_arguments(settings: Settings) -> dict[str, Any]:
    return {
        "x-queue-type": "quorum",
        "x-delivery-limit": settings.queue_delivery_limit,
        "x-dead-letter-exchange": EXCHANGE_DLX,
    }


async def declare_topology(channel: AbstractChannel, settings: Settings) -> AbstractExchange:
    jobs = await channel.declare_exchange(EXCHANGE_JOBS, ExchangeType.DIRECT, durable=True)
    dlx = await channel.declare_exchange(EXCHANGE_DLX, ExchangeType.FANOUT, durable=True)
    work = await channel.declare_queue(QUEUE_WORK, durable=True, arguments=work_queue_arguments(settings))
    await work.bind(jobs, ROUTING_KEY_WORK)
    dlq = await channel.declare_queue(QUEUE_DLQ, durable=True, arguments={"x-queue-type": "quorum"})
    await dlq.bind(dlx, "")
    return jobs


class BrokerClient:
    """One robust (auto-reconnecting) connection plus a confirm-mode channel."""

    def __init__(self, settings: Settings, name: str) -> None:
        self._settings = settings
        self._name = name
        self.connection: AbstractRobustConnection | None = None
        self.channel: AbstractChannel | None = None
        self._jobs_exchange: AbstractExchange | None = None
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
            exchange = await declare_topology(channel, self._settings)
        except Exception:
            await connection.close()
            raise
        self.connection, self.channel, self._jobs_exchange = connection, channel, exchange
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
    async def publish_job_event(self, *, event_id: uuid.UUID | str, job_id: uuid.UUID | str, body: bytes,
                                headers: dict[str, Any] | None = None) -> None:
        """Publish a persistent job message and wait for the broker's confirm."""
        self._require_ready()
        assert self._jobs_exchange is not None
        message = Message(
            body,
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
            message_id=str(event_id),
            headers={"x-job-id": str(job_id), **(headers or {})},
        )
        await self._jobs_exchange.publish(message, routing_key=ROUTING_KEY_WORK, timeout=10)

    async def republish(self, message: AbstractIncomingMessage, *, extra_headers: dict[str, Any]) -> None:
        """Publish a copy of a delivered job message (confirmed) — used before acking the original."""
        headers = {k: v for k, v in (message.headers or {}).items() if not k.startswith("x-delivery")}
        headers.update(extra_headers)
        job_id = headers.get("x-job-id", "")
        await self.publish_job_event(event_id=message.message_id or uuid.uuid4(), job_id=job_id,
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
