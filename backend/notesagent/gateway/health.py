"""GET /healthz — honest dependency checks (only error *types* are reported, never messages)."""
from __future__ import annotations

import asyncio
from typing import Any

from notesagent.common.broker import QUEUE_WORK

_CHECK_TIMEOUT = 3.0


async def _postgres(state) -> dict[str, Any]:
    async with state.pool.connection(timeout=_CHECK_TIMEOUT) as conn:
        await conn.execute("SELECT 1")
        cur = await conn.execute("SELECT count(*) AS n FROM outbox WHERE published_at IS NULL")
        row = await cur.fetchone()
    return {"status": "ok", "outbox_pending": row["n"]}


async def _rabbitmq(state) -> dict[str, Any]:
    if not state.broker.ready:
        return {"status": "down", "error": "NotConnected"}
    ch = await state.broker.connection.channel(publisher_confirms=False)
    try:
        queue = await ch.declare_queue(QUEUE_WORK, passive=True)
        result = queue.declaration_result
        return {"status": "ok", "work_queue_messages": result.message_count,
                "work_queue_consumers": result.consumer_count}
    finally:
        await ch.close()


async def _redis(state) -> dict[str, Any]:
    await state.redis.ping()
    workers = [k async for k in state.redis.scan_iter(match="workers:heartbeat:*", count=100)]
    return {"status": "ok", "live_workers": len(workers)}


async def _run(check, state) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(check(state), _CHECK_TIMEOUT + 1)
    except Exception as exc:  # noqa: BLE001
        return {"status": "down", "error": type(exc).__name__}


async def check_health(state) -> tuple[bool, dict[str, Any]]:
    postgres, rabbitmq, redis = await asyncio.gather(
        _run(_postgres, state), _run(_rabbitmq, state), _run(_redis, state))
    checks = {"gateway": {"status": "ok"}, "postgres": postgres, "rabbitmq": rabbitmq, "redis": redis}
    healthy = all(c["status"] == "ok" for c in checks.values())
    return healthy, {"status": "ok" if healthy else "unavailable", "checks": checks}
