"""Process helpers: run an async service until SIGTERM/SIGINT."""
from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable


def run_service(main: Callable[[asyncio.Event], Awaitable[None]]) -> None:
    async def _runner() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await main(stop)

    asyncio.run(_runner())
