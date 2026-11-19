"""TEST-ONLY: an independent process that takes permits from the shared rate limiter.

Usage: python -m tests.support.limiter_client <out-file> <go-file> <provider> <count> [tokens]

Env (test knobs): LIMITER_WINDOW_SEC (Gemini window, default 60), LIMITER_MAX_WAIT (default 60) and
the normal settings (GEMINI_RPM_LIMIT, GEMINI_TPM_LIMIT, GEMINI_RPD_LIMIT, NOTION_RPS_LIMIT, REDIS_URL).
Writes one JSON line per attempt: {"ok": bool, "at_ms": redis time, "tokens": n, "error": code}.
Waits for <go-file> to exist so several processes start at the same moment.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import redis.asyncio as aioredis

from notesagent.common.config import get_settings
from notesagent.common.errors import PipelineError, Provider
from notesagent.common.ratelimit import RateLimiter


async def main(out: Path, go: Path, provider: str, count: int, tokens: int) -> None:
    settings = get_settings()
    client = aioredis.from_url(settings.redis_url.get_secret_value(), socket_timeout=2, socket_connect_timeout=2)
    limiter = RateLimiter(client, settings, gemini_window_sec=float(os.environ.get("LIMITER_WINDOW_SEC", "60")))
    max_wait = float(os.environ.get("LIMITER_MAX_WAIT", "60"))
    (out.parent / f"{out.name}.ready").write_text("")
    while not go.exists():
        time.sleep(0.01)
    with out.open("a") as fh:
        for _ in range(count):
            try:
                permit = await limiter.acquire(Provider(provider), tokens=tokens, max_wait_sec=max_wait)
                fh.write(json.dumps({"ok": True, "at_ms": permit.granted_at_ms, "tokens": tokens}) + "\n")
            except PipelineError as exc:
                fh.write(json.dumps({"ok": False, "error": exc.code}) + "\n")
            fh.flush()
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]),
                     int(sys.argv[5]) if len(sys.argv) > 5 else 0))
