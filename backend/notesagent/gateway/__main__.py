"""Run the gateway: ``python -m notesagent.gateway``."""
from __future__ import annotations

import uvicorn

from notesagent.common.config import get_settings
from notesagent.common.logging import setup_logging


def main() -> None:
    settings = get_settings(service_name="gateway")
    setup_logging("gateway", settings.log_level)
    uvicorn.run(
        "notesagent.gateway.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        log_config=None,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
