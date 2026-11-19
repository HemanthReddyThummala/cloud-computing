"""TEST-ONLY: take one message from a queue with basic.get (manual ack) and hold it unacked
forever — exactly the state a worker is in between ``get`` and ``nack`` in peek_credential.
The test then SIGKILLs this process to simulate a crash at that point.

Usage: python -m tests.support.hold_message <queue> <marker-file>
"""
import asyncio
import os
import sys
from pathlib import Path

import aio_pika


async def main(queue_name: str, marker: Path) -> None:
    conn = await aio_pika.connect(os.environ["TEST_RABBITMQ_URL"])
    ch = await conn.channel()
    queue = await ch.declare_queue(queue_name, passive=True)
    message = await queue.get(no_ack=False, fail=True, timeout=10)
    marker.write_text(f"held {len(message.body)} bytes")
    await asyncio.Event().wait()  # never ack/nack


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], Path(sys.argv[2])))
