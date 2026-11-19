"""Error types shared across services.

API errors always render as ``{"error": {"code", "message"}}`` with fixed,
pre-written messages — upstream exception text is never echoed to clients.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, NoReturn


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


# ===================================================================== pipeline error classes (ADR-10)
#
# Classification only: these objects describe WHAT should happen (fail now / retry under which
# budget / minimum delay). Acting on them (counting attempts, scheduling, dead-lettering) is the
# worker's job (Phase 2, Parts 3-5).
#
# Safety: an instance holds only a stable code, a fixed message, the provider and whitelisted
# scalar details. It never stores upstream response bodies, URLs or exception text.

class Provider(StrEnum):
    GEMINI = "GEMINI"
    NOTION = "NOTION"
    YOUTUBE = "YOUTUBE"
    GENERIC = "HTTP"


class RetryBudget(StrEnum):
    NONE = "none"                    # FAILED immediately
    PROVIDER = "provider"            # MAX_ATTEMPTS
    INFRASTRUCTURE = "infrastructure"  # INFRA_MAX_ATTEMPTS (no provider call was made)


_DETAIL_KEYS = frozenset({"http_status", "provider_code", "retry_after_sec", "resets_at", "exception_type",
                          "notion_write_outcome_unknown", "committed_resource_id"})
_MAX_MESSAGE = 200


def _safe_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if key in _DETAIL_KEYS and (value is None or isinstance(value, (bool, int, float, str))):
            clean[key] = value[:64] if isinstance(value, str) else value
    return clean


class PipelineError(Exception):
    """Base for classified pipeline errors."""

    budget: RetryBudget = RetryBudget.PROVIDER

    def __init__(self, code: str, message: str, *, provider: Provider | None = None,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message[:_MAX_MESSAGE]
        self.provider = provider
        self.detail = _safe_detail(detail)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def __repr__(self) -> str:
        provider = self.provider.value if self.provider else None
        return f"{type(self).__name__}(code={self.code!r}, provider={provider!r})"

    @property
    def retryable(self) -> bool:
        return self.budget is not RetryBudget.NONE


class FatalStageError(PipelineError):
    """A non-retryable pipeline error: the job goes to FAILED with this code.

    Constructor unchanged since Phase 1: ``FatalStageError(code, message)``.
    """

    budget = RetryBudget.NONE


class RetryableError(PipelineError):
    """Retrying may succeed; bounded by a retry budget."""


class TransientError(RetryableError):
    """Temporary failure: 5xx, timeout, connection problem, unexpected exception."""


class RateLimited(RetryableError):
    """The provider asked us to slow down. ``retry_after_sec`` is a RELATIVE delay, already capped."""

    def __init__(self, code: str, message: str, *, provider: Provider | None = None,
                 retry_after_sec: float | None = None, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, provider=provider, detail={**(detail or {}), "retry_after_sec": retry_after_sec})
        self.retry_after_sec = retry_after_sec


class QuotaExhausted(RetryableError):
    """A daily quota is used up. ``resets_at`` is an ABSOLUTE, timezone-aware time — never capped."""

    def __init__(self, code: str, message: str, *, provider: Provider | None = None,
                 resets_at: datetime, detail: dict[str, Any] | None = None) -> None:
        if resets_at.tzinfo is None or resets_at.utcoffset() is None:
            raise ValueError("resets_at must be timezone-aware")
        super().__init__(code, message, provider=provider,
                         detail={**(detail or {}), "resets_at": resets_at.isoformat()})
        self.resets_at = resets_at


class RateLimiterUnavailable(RetryableError):
    """Our shared rate limiter (Redis) is unavailable; the provider was NOT called."""

    budget = RetryBudget.INFRASTRUCTURE


def raise_classified(error: PipelineError) -> NoReturn:
    """Raise a classified error WITHOUT chaining the upstream exception (``from None``), so
    tracebacks and logs can never carry upstream exception text."""
    raise error from None
