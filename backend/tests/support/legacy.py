"""TEST-ONLY: recreate the Phase 1 broker state (legacy ``jobs.work`` queue) and observe topology."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message

from notesagent.common.broker import (
    EXCHANGE_DLX,
    EXCHANGE_JOBS,
    LEGACY_QUEUE_WORK,
    QUEUE_WORK,
    ROUTING_KEY_WORK,
    legacy_work_queue_arguments,
    work_queue_arguments,
)
from notesagent.common.config import get_settings
from notesagent.ops.rabbitmq_mgmt import ManagementApi


def mgmt_api() -> ManagementApi:
    return ManagementApi(os.environ["TEST_RABBITMQ_MGMT_URL"], os.environ["TEST_RABBITMQ_URL"])


async def _setup(*, legacy: bool, legacy_bound: bool, v2: bool, v2_bound: bool, messages: int,
                 job_ids: list[str] | None) -> None:
    settings = get_settings()
    conn = await aio_pika.connect(os.environ["TEST_RABBITMQ_URL"])
    try:
        ch = await conn.channel(publisher_confirms=True)
        jobs = await ch.declare_exchange(EXCHANGE_JOBS, ExchangeType.DIRECT, durable=True)
        await ch.declare_exchange(EXCHANGE_DLX, ExchangeType.FANOUT, durable=True)
        if legacy:
            q = await ch.declare_queue(LEGACY_QUEUE_WORK, durable=True, arguments=legacy_work_queue_arguments(settings))
            if legacy_bound:
                await q.bind(jobs, ROUTING_KEY_WORK)
            ids = job_ids or [str(uuid.uuid4()) for _ in range(messages)]
            for job_id in ids:
                event_id = str(uuid.uuid4())
                body = json.dumps({"event_id": event_id, "event_type": "job.submitted", "job_id": job_id,
                                   "schema": 1}).encode()
                await ch.default_exchange.publish(        # straight into the legacy queue
                    Message(body, delivery_mode=DeliveryMode.PERSISTENT, message_id=event_id,
                            content_type="application/json", headers={"x-job-id": job_id}),
                    routing_key=LEGACY_QUEUE_WORK)
        if v2:
            q2 = await ch.declare_queue(QUEUE_WORK, durable=True, arguments=work_queue_arguments(settings))
            if v2_bound:
                await q2.bind(jobs, ROUTING_KEY_WORK)
    finally:
        await conn.close()


def setup_broker(*, legacy: bool = True, legacy_bound: bool = True, v2: bool = False, v2_bound: bool = False,
                 messages: int = 0, job_ids: list[str] | None = None) -> None:
    asyncio.run(_setup(legacy=legacy, legacy_bound=legacy_bound, v2=v2, v2_bound=v2_bound,
                       messages=messages, job_ids=job_ids))


def bound_routing_keys(queue: str) -> list[str]:
    return [b["routing_key"] for b in mgmt_api().bindings(EXCHANGE_JOBS, queue)]


def wait_for_stats(queue: str, predicate=lambda d: True, timeout: float = 30) -> dict:
    """Wait until the management API exposes message statistics for ``queue`` (they lag ~5 s)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        doc = mgmt_api().queue(queue)
        if doc and all(isinstance(doc.get(k), int) for k in ("messages_ready", "messages_unacknowledged",
                                                              "messages", "consumers")) and predicate(doc):
            return doc
        time.sleep(0.5)
    raise AssertionError(f"management statistics for {queue} not available/as expected within {timeout}s")
