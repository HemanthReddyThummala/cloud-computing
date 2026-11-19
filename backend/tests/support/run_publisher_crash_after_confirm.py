"""TEST-ONLY: the real OutboxPublisher, SIGKILLed right after the broker confirm and
before the outbox row is marked published (the worst possible moment)."""
import asyncio
import os
import signal

from notesagent.common.config import get_settings
from notesagent.common.logging import setup_logging
from notesagent.outbox.publisher import OutboxPublisher


def _die(_row) -> None:
    os.kill(os.getpid(), signal.SIGKILL)


async def main() -> None:
    publisher = OutboxPublisher(get_settings(), after_confirm=_die)
    await publisher.open()
    await publisher.publish_one()


if __name__ == "__main__":
    setup_logging("crashing-publisher")
    asyncio.run(main())
