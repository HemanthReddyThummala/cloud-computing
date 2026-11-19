"""Error types shared across services.

API errors always render as ``{"error": {"code", "message"}}`` with fixed,
pre-written messages — upstream exception text is never echoed to clients.
"""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *, extra: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}, **self.extra}


class BrokerUnavailable(Exception):
    """RabbitMQ is not connected."""


class InvalidTransition(Exception):
    """A requested job status change is not allowed by the state machine."""

    def __init__(self, from_status: str, to_status: str) -> None:
        super().__init__(f"illegal job status transition {from_status} -> {to_status}")
        self.from_status = from_status
        self.to_status = to_status


class StaleJobState(Exception):
    """Compare-and-set failed: the job was no longer in the expected status."""


class FatalStageError(Exception):
    """A non-retryable pipeline error: the job goes to FAILED with this code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
