"""Run the outbox publisher: ``python -m notesagent.outbox``."""
from __future__ import annotations

from notesagent.common.config import get_settings
from notesagent.common.logging import setup_logging
from notesagent.common.runtime import run_service
from notesagent.outbox.publisher import OutboxPublisher


def main() -> None:
    settings = get_settings(service_name="outbox-publisher")
    setup_logging("outbox-publisher", settings.log_level)
    run_service(OutboxPublisher(settings).run)


if __name__ == "__main__":
    main()
