"""Transactional-outbox relay.

For each pending row (claimed with ``FOR UPDATE SKIP LOCKED`` so several publishers
can run safely):

    publish to RabbitMQ  ->  wait for publisher confirm  ->  mark row published  ->  COMMIT

A crash after the confirm but before the COMMIT leaves the row pending, so the event is
published again later: delivery is *at least once*. Consumers are idempotent (ADR-05).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg

from notesagent.common.broker import BrokerClient
from notesagent.common.config import Settings
from notesagent.common.db import open_pool
from notesagent.common.jobs_repo import OUTBOX_NOTIFY_CHANNEL

log = logging.getLogger(__name__)

AfterConfirmHook = Callable[[dict[str, Any]], None]


class OutboxPublisher:
    def __init__(self, settings: Settings, *, after_confirm: AfterConfirmHook | None = None) -> None:
        self._settings = settings
        self._after_confirm = after_confirm  # test hook: lets tests kill the process at the worst moment
        self.pool = None
        self.broker: BrokerClient | None = None
        self._wake = asyncio.Event()

    async def open(self) -> None:
        self.pool = await open_pool(self._settings, name="outbox", min_size=1, max_size=2, wait=True)
        self.broker = BrokerClient(self._settings, name="outbox-publisher")
        await self.broker.start(wait=False)

    async def close(self) -> None:
        if self.broker:
            await self.broker.close()
        if self.pool:
            await self.pool.close()

    # ------------------------------------------------------------ core step
    async def publish_one(self) -> bool:
        """Publish the oldest pending event. True if one was published, False if none pending."""
        assert self.pool is not None and self.broker is not None
        row: dict[str, Any] | None = None
        async with self.pool.connection() as conn:
            try:
                async with conn.transaction():
                    cur = await conn.execute(
                        "SELECT id, event_id, job_id, event_type, payload FROM outbox"
                        " WHERE published_at IS NULL ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED")
                    row = await cur.fetchone()
                    if row is None:
                        return False
                    await self.broker.publish_job_event(
                        event_id=row["event_id"], job_id=row["job_id"],
                        body=json.dumps(row["payload"]).encode("utf-8"))
                    # --- the broker has confirmed the message at this point ---
                    if self._after_confirm:
                        self._after_confirm(row)
                    await conn.execute(
                        "UPDATE outbox SET published_at = now(), publish_attempts = publish_attempts + 1,"
                        " last_error = NULL WHERE id = %s", (row["id"],))
            except Exception as exc:
                if row is not None:
                    with contextlib.suppress(Exception):
                        await conn.execute(
                            "UPDATE outbox SET publish_attempts = publish_attempts + 1, last_error = %s"
                            " WHERE id = %s AND published_at IS NULL", (type(exc).__name__, row["id"]))
                raise
        log.info("outbox event published", extra={"event_id": str(row["event_id"]), "job_id": str(row["job_id"])})
        return True

    async def drain(self, limit: int = 100) -> int:
        published = 0
        while published < limit and await self.publish_one():
            published += 1
        return published

    # ------------------------------------------------------------ service loop
    async def run(self, stop: asyncio.Event) -> None:
        await self.open()
        listener = asyncio.create_task(self._listen(stop))
        backoff = 1.0
        interval = self._settings.outbox_poll_interval_ms / 1000
        log.info("outbox publisher started")
        try:
            while not stop.is_set():
                self._touch_heartbeat()
                try:
                    published = await self.drain()
                    backoff = 1.0
                except Exception as exc:  # noqa: BLE001 - broker/db outage: keep rows, retry later
                    log.warning("outbox publish failed; will retry", extra={"error_type": type(exc).__name__,
                                                                            "retry_in_sec": backoff})
                    await self._sleep(stop, backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                if published == 0:
                    await self._wait_for_work(stop, interval)
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener
            await self.close()

    async def _listen(self, stop: asyncio.Event) -> None:
        """LISTEN for the gateway's NOTIFY so new jobs are published immediately (polling is the fallback)."""
        while not stop.is_set():
            try:
                conn = await psycopg.AsyncConnection.connect(
                    self._settings.database_url.get_secret_value(), autocommit=True)
                async with conn:
                    await conn.execute(f"LISTEN {OUTBOX_NOTIFY_CHANNEL}")
                    async for _ in conn.notifies():
                        self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("outbox LISTEN connection lost", extra={"error_type": type(exc).__name__})
                await self._sleep(stop, 2.0)

    async def _wait_for_work(self, stop: asyncio.Event, timeout: float) -> None:
        """Sleep until NOTIFY wakes us, the poll interval passes, or shutdown is requested."""
        waiters = {asyncio.create_task(self._wake.wait()), asyncio.create_task(stop.wait())}
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiters:
                task.cancel()
        self._wake.clear()

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), seconds)

    def _touch_heartbeat(self) -> None:
        with contextlib.suppress(OSError):
            Path(self._settings.outbox_heartbeat_file).write_text(str(time.time()))
