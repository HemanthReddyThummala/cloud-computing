"""RabbitMQ helpers for tests: management API for vhost/queue admin, AMQP for exact state."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass

import aio_pika
import httpx
from aio_pika import DeliveryMode, Message
from aio_pika.exceptions import ChannelNotFoundEntity

VHOST = "notes_test"


class RabbitAdmin:
    def __init__(self) -> None:
        self.client = httpx.Client(
            base_url=os.environ["TEST_RABBITMQ_MGMT_URL"],
            auth=(os.environ["TEST_RABBITMQ_USER"], os.environ["TEST_RABBITMQ_PASS"]),
            timeout=15,
        )

    def reset_vhost(self) -> None:
        """Delete and recreate the test vhost: removes every queue/exchange from earlier tests."""
        self.client.delete(f"/api/vhosts/{VHOST}")
        self.client.put(f"/api/vhosts/{VHOST}").raise_for_status()
        user = os.environ["TEST_RABBITMQ_USER"]
        self.client.put(f"/api/permissions/{VHOST}/{user}",
                        json={"configure": ".*", "write": ".*", "read": ".*"}).raise_for_status()

    def queue_names(self) -> list[str]:
        resp = self.client.get(f"/api/queues/{VHOST}", params={"columns": "name"})
        resp.raise_for_status()
        return [q["name"] for q in resp.json()]

    def queue_details(self, name: str) -> dict:
        resp = self.client.get(f"/api/queues/{VHOST}/{name}")
        resp.raise_for_status()
        return resp.json()


@dataclass
class QueueState:
    exists: bool
    messages: int = 0
    consumers: int = 0


async def _queue_state(name: str) -> QueueState:
    conn = await aio_pika.connect(os.environ["TEST_RABBITMQ_URL"])
    try:
        ch = await conn.channel()
        try:
            q = await ch.declare_queue(name, passive=True)
        except ChannelNotFoundEntity:
            return QueueState(False)
        r = q.declaration_result
        return QueueState(True, r.message_count, r.consumer_count)
    finally:
        await conn.close()


def queue_state(name: str) -> QueueState:
    """Exact, real-time queue state (ready messages, consumers) via passive declare."""
    return asyncio.run(_queue_state(name))


@dataclass
class PeekedMessage:
    body: bytes
    message_id: str | None
    delivery_mode: int | None
    headers: dict


async def _peek(name: str, limit: int) -> list[PeekedMessage]:
    conn = await aio_pika.connect(os.environ["TEST_RABBITMQ_URL"])
    try:
        ch = await conn.channel()
        q = await ch.declare_queue(name, passive=True)
        held, out = [], []
        for _ in range(limit):
            m = await q.get(no_ack=False, fail=False, timeout=5)
            if m is None:
                break
            held.append(m)
            out.append(PeekedMessage(bytes(m.body), m.message_id, int(m.delivery_mode or 0), dict(m.headers or {})))
        for m in held:
            await m.nack(requeue=True)
        return out
    finally:
        await conn.close()


def peek_messages(name: str, limit: int = 10) -> list[PeekedMessage]:
    """Read messages without consuming them (get + nack(requeue))."""
    return asyncio.run(_peek(name, limit))


async def _publish(body: bytes, message_id: str, job_id: str) -> None:
    conn = await aio_pika.connect(os.environ["TEST_RABBITMQ_URL"])
    try:
        ch = await conn.channel(publisher_confirms=True)
        ex = await ch.get_exchange("jobs", ensure=True)
        await ex.publish(Message(body, delivery_mode=DeliveryMode.PERSISTENT, message_id=message_id,
                                 content_type="application/json", headers={"x-job-id": job_id}),
                         routing_key="job.work")
    finally:
        await conn.close()


def publish_job_message(job_id: str, event_id: str | None = None) -> None:
    """Publish a job message exactly like the outbox publisher would (used to create duplicates)."""
    event_id = event_id or str(uuid.uuid4())
    body = json.dumps({"event_id": event_id, "event_type": "job.submitted", "job_id": job_id, "schema": 1}).encode()
    asyncio.run(_publish(body, event_id, job_id))


def publish_raw(body: bytes) -> None:
    asyncio.run(_publish(body, str(uuid.uuid4()), "unknown"))
