"""Pure classification of provider failures into retry categories (ADR-10).

Every function here is PURE: no network, database, broker, Redis or worker access, no side
effects, no attempt counting. Input: an HTTP status + headers + body (any HTTP library), or an
exception. Output: a ``PipelineError`` subclass describing what should happen — never the
upstream body text, URL or exception message.

    classify_http(provider, status, headers, body, *, now, method) -> PipelineError | None (None = 2xx)
    classify_exception(provider, exc)                               -> PipelineError
    parse_retry_after(value, *, now, cap)                           -> seconds | None
    next_pacific_midnight(now)                                      -> aware datetime (Gemini RPD reset)
"""
from __future__ import annotations

import json
import math
import re
import socket
import ssl
from collections.abc import Iterator, Mapping
from datetime import datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

from notesagent.common.errors import (
    FatalStageError,
    PipelineError,
    Provider,
    QuotaExhausted,
    RateLimited,
    TransientError,
)

RETRY_AFTER_MAX_SEC = 3600.0
PACIFIC = ZoneInfo("America/Los_Angeles")
_MAX_BODY_BYTES = 64 * 1024          # larger bodies are not parsed (status code alone decides)
_EXCEPTION_CHAIN_DEPTH = 5
_PROVIDER_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")
_NUMBER = re.compile(r"^\d+(\.\d+)?$")
_DURATION = re.compile(r"^(\d+(\.\d+)?)s$")
# Methods whose repetition is known to be safe. Anything else — including a MISSING or unknown
# method — is treated as a write whose outcome may be unknown (conservative default).
_SAFE_TO_REPEAT = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})
_GENERIC_TRANSIENT = frozenset({408, 425, 500, 502, 503, 504})


# ===================================================================== small helpers
def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now


def _header(headers: Any, name: str) -> str | None:
    if not headers:
        return None
    items = headers.items() if isinstance(headers, Mapping) else headers
    try:
        for key, value in items:
            if str(key).lower() == name.lower():
                return str(value)
    except (TypeError, ValueError):
        return None
    return None


def _json_object(body: Any) -> dict[str, Any] | None:
    """Best-effort parse into a dict; anything malformed/oversized/non-object -> None. Never raises."""
    if isinstance(body, dict):
        return body
    try:
        if isinstance(body, (bytes, bytearray)):
            if len(body) > _MAX_BODY_BYTES:
                return None
            body = bytes(body).decode("utf-8")
        if isinstance(body, str):
            if len(body) > _MAX_BODY_BYTES:
                return None
            parsed = json.loads(body)
            return parsed if isinstance(parsed, dict) else None
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return None


def _safe_code(value: Any) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value)
        return text if _PROVIDER_CODE.fullmatch(text) else None
    return None


def parse_retry_after(value: Any, *, now: datetime | None = None, cap: float = RETRY_AFTER_MAX_SEC) -> float | None:
    """Seconds (integer/decimal) or an HTTP date -> relative seconds, clamped to ``cap``.
    Negative, past, non-finite or unparseable values -> None. Never raises."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip()
        if _NUMBER.fullmatch(text):
            seconds = float(text)
        else:
            try:
                when = parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError, OverflowError):
                return None
            if when is None:
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - _now(now)).total_seconds()
            if seconds <= 0:
                return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, cap)


def next_pacific_midnight(now: datetime | None = None) -> datetime:
    """The next 00:00 in America/Los_Angeles (Gemini RPD reset); DST-correct, timezone-aware."""
    local = _now(now).astimezone(PACIFIC)
    return datetime.combine(local.date() + timedelta(days=1), time(0, 0), tzinfo=PACIFIC)


def _detail(status: int | None, provider_code: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"http_status": status, "provider_code": provider_code, **extra}


# ===================================================================== HTTP classification
def classify_http(provider: Provider, status: int, headers: Any = None, body: Any = None, *,
                  now: datetime | None = None, method: str | None = None,
                  retry_after_cap: float = RETRY_AFTER_MAX_SEC) -> PipelineError | None:
    """Classify an HTTP response. Returns None for 2xx. Never raises for any status/headers/body."""
    try:
        status = int(status)
    except (TypeError, ValueError):
        return FatalStageError(f"{provider.value}_UNEXPECTED_HTTP_STATUS", "The provider returned an invalid status.")
    if 200 <= status < 300:
        return None
    current = _now(now)
    document = _json_object(body)
    header_delay = parse_retry_after(_header(headers, "retry-after"), now=current, cap=retry_after_cap)

    if provider is Provider.NOTION:
        result = _notion(status, document, header_delay, method, current, retry_after_cap)
    elif provider is Provider.GEMINI:
        result = _gemini(status, document, header_delay, current, retry_after_cap)
    elif provider is Provider.YOUTUBE:
        result = _youtube(status, header_delay)
    else:
        result = None
    return result or _generic(provider, status, header_delay)


def _generic(provider: Provider, status: int, delay: float | None) -> PipelineError:
    prefix = provider.value
    if status == 429:
        return RateLimited(f"{prefix}_RATE_LIMITED", "The provider is rate limiting requests.",
                           provider=provider, retry_after_sec=delay, detail=_detail(status))
    if status in _GENERIC_TRANSIENT:
        return TransientError(f"{prefix}_HTTP_{status}", "The provider is temporarily unavailable.",
                              provider=provider, detail=_detail(status))
    if 400 <= status < 500:
        return FatalStageError(f"{prefix}_HTTP_{status}", "The provider rejected the request.",
                               provider=provider, detail=_detail(status))
    if 500 <= status < 600:
        return TransientError(f"{prefix}_HTTP_{status}", "The provider reported a server error.",
                              provider=provider, detail=_detail(status))
    return FatalStageError(f"{prefix}_UNEXPECTED_HTTP_STATUS", "The provider returned an unexpected status.",
                           provider=provider, detail=_detail(status))


# ---------------------------------------------------------------- Notion
def _notion(status: int, doc: dict[str, Any] | None, header_delay: float | None, method: str | None,
            now: datetime, cap: float) -> PipelineError | None:
    p = Provider.NOTION
    code = _safe_code((doc or {}).get("code"))
    additional = (doc or {}).get("additional_data")
    additional = additional if isinstance(additional, dict) else {}
    delay = header_delay if header_delay is not None else parse_retry_after(additional.get("retry_after"),
                                                                           now=now, cap=cap)
    if status == 429:
        if additional.get("rate_limit_reason") == "public_api_request_blocked":
            return FatalStageError("NOTION_REQUEST_BLOCKED",
                                   "Notion has restricted this connection's API access; retrying will not help.",
                                   provider=p, detail=_detail(status, code))
        return RateLimited("NOTION_RATE_LIMITED", "Notion is rate limiting requests.", provider=p,
                           retry_after_sec=delay, detail=_detail(status, code))
    if status == 529:  # docs: "retry it the same way as a 429"
        return RateLimited("NOTION_SERVICE_OVERLOAD", "Notion is temporarily overloaded.", provider=p,
                           retry_after_sec=delay, detail=_detail(status, code))
    if status in (500, 502, 503, 504):
        # docs: retry 5xx only for idempotent requests or with own idempotency protection; a 503 write
        # may have been saved. This flag RECORDS UNCERTAINTY — it does not make a retry safe: the
        # publishing stage (Phase 5) must reconcile (read current state) before writing again.
        guidance = additional.get("retry_guidance")
        safe_method = isinstance(method, str) and method.strip().upper() in _SAFE_TO_REPEAT
        unknown = not safe_method or bool(guidance)   # retry_guidance always wins
        committed = additional.get("committed_resource_id")
        extra: dict[str, Any] = {"notion_write_outcome_unknown": unknown}
        if isinstance(committed, str) and _UUID.fullmatch(committed):
            extra["committed_resource_id"] = committed
        return TransientError(f"NOTION_HTTP_{status}", "Notion is temporarily unavailable.", provider=p,
                              detail=_detail(status, code, **extra))
    if status == 409:  # docs: "Make sure the parameters are up to date and try again."
        return TransientError("NOTION_CONFLICT", "Notion reported a conflict while saving.", provider=p,
                              detail=_detail(status, code))
    if status == 401:
        return FatalStageError("NOTION_UNAUTHORIZED",
                               "The Notion token is invalid or expired. Refresh the connection and resubmit.",
                               provider=p, detail=_detail(status, code))
    if status == 403:
        return FatalStageError("NOTION_FORBIDDEN",
                               "The Notion connection lacks permission (page not shared or workspace limit).",
                               provider=p, detail=_detail(status, code))
    if status == 404:
        return FatalStageError("NOTION_OBJECT_NOT_FOUND",
                               "The Notion page was not found or not shared with the connection.",
                               provider=p, detail=_detail(status, code))
    if status == 400:
        return FatalStageError("NOTION_BAD_REQUEST", "Notion rejected the request as invalid.", provider=p,
                               detail=_detail(status, code))
    return None


# ---------------------------------------------------------------- Gemini
def _gemini_error(doc: dict[str, Any] | None) -> dict[str, Any]:
    error = (doc or {}).get("error")
    return error if isinstance(error, dict) else {}


def _gemini_is_daily_quota(error: dict[str, Any]) -> bool:
    if error.get("code") == "quota_exceeded":
        return True
    for item in error.get("details") or []:
        if not isinstance(item, dict) or "QuotaFailure" not in str(item.get("@type", "")):
            continue
        for violation in item.get("violations") or []:
            if isinstance(violation, dict) and "PerDay" in str(violation.get("quotaId", "")):
                return True
    return False


def _gemini_retry_delay(error: dict[str, Any], now: datetime, cap: float) -> float | None:
    for item in error.get("details") or []:
        if isinstance(item, dict) and "RetryInfo" in str(item.get("@type", "")):
            match = _DURATION.fullmatch(str(item.get("retryDelay", "")).strip())
            if match:
                return parse_retry_after(match.group(1), now=now, cap=cap)
    return None


def _gemini(status: int, doc: dict[str, Any] | None, header_delay: float | None, now: datetime,
            cap: float) -> PipelineError | None:
    p = Provider.GEMINI
    error = _gemini_error(doc)
    code = _safe_code(error.get("status")) or _safe_code(error.get("code"))
    if status == 429:
        if _gemini_is_daily_quota(error):
            resets = next_pacific_midnight(now)        # absolute; deliberately NOT capped
            return QuotaExhausted("GEMINI_QUOTA_EXHAUSTED",
                                  "The Gemini daily free-tier quota is used up; it resets at midnight Pacific.",
                                  provider=p, resets_at=resets, detail=_detail(status, code))
        delay = header_delay if header_delay is not None else _gemini_retry_delay(error, now, cap)
        return RateLimited("GEMINI_RATE_LIMITED", "Gemini is rate limiting requests.", provider=p,
                           retry_after_sec=delay, detail=_detail(status, code))
    if status in (500, 503, 504):
        return TransientError(f"GEMINI_HTTP_{status}", "Gemini is temporarily unavailable.", provider=p,
                              detail=_detail(status, code))
    fatal = {400: ("GEMINI_BAD_REQUEST", "Gemini rejected the request as invalid."),
             401: ("GEMINI_AUTH", "The Gemini API key is invalid or not permitted."),
             403: ("GEMINI_AUTH", "The Gemini API key is invalid or not permitted."),
             402: ("GEMINI_PAYMENT_REQUIRED", "Gemini requires payment for this request; paid usage is disabled."),
             404: ("GEMINI_MODEL_NOT_FOUND", "The configured Gemini model was not found.")}
    if status in fatal:
        fatal_code, message = fatal[status]
        return FatalStageError(fatal_code, message, provider=p, detail=_detail(status, code))
    return None


# ---------------------------------------------------------------- YouTube (HTTP level)
def _youtube(status: int, header_delay: float | None) -> PipelineError | None:
    p = Provider.YOUTUBE
    if status == 429:
        return RateLimited("YOUTUBE_RATE_LIMITED", "YouTube is rate limiting requests.", provider=p,
                           retry_after_sec=header_delay, detail=_detail(status))
    if status == 403:                                                   # D2: often a temporary bot check
        return TransientError("YOUTUBE_FORBIDDEN", "YouTube temporarily refused the request.", provider=p,
                              detail=_detail(status))
    if status in (404, 410):                                            # D3: permanent -> FAILED
        return FatalStageError("YOUTUBE_VIDEO_UNAVAILABLE", "The video is unavailable or has been removed.",
                               provider=p, detail=_detail(status))
    if 500 <= status < 600:
        return TransientError(f"YOUTUBE_HTTP_{status}", "YouTube is temporarily unavailable.", provider=p,
                              detail=_detail(status))
    return None


# ===================================================================== exceptions
def _chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception and up to 5 wrapped causes/contexts (cycle-safe)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    depth = 0
    while current is not None and id(current) not in seen and depth <= _EXCEPTION_CHAIN_DEPTH:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__
        depth += 1


def classify_exception(provider: Provider, exc: BaseException) -> PipelineError:
    """Classify an exception raised instead of an HTTP response. Never uses the exception's text."""
    if isinstance(exc, PipelineError):
        return exc
    prefix = provider.value
    chain = list(_chain(exc))
    # TLS first (requirement B) — and connection drops before the generic SSLError (D4a).
    for item in chain:
        name = type(item).__name__
        if isinstance(item, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
            return TransientError(f"{prefix}_TLS_CONNECTION_DROPPED", "The secure connection was dropped.",
                                  provider=provider, detail={"exception_type": name})
        if isinstance(item, (ssl.SSLCertVerificationError, ssl.CertificateError)):
            return FatalStageError(f"{prefix}_TLS_ERROR", "TLS certificate verification failed.",
                                   provider=provider, detail={"exception_type": name})
        if isinstance(item, ssl.SSLError):
            return FatalStageError(f"{prefix}_TLS_ERROR", "The TLS handshake failed.",
                                   provider=provider, detail={"exception_type": name})
    for item in chain:
        if isinstance(item, (TimeoutError, ConnectionError, socket.gaierror, OSError)):
            return TransientError(f"{prefix}_UNREACHABLE", "The provider could not be reached.",
                                  provider=provider, detail={"exception_type": type(item).__name__})
    return TransientError("UNEXPECTED_ERROR", "An unexpected error occurred.", provider=provider,  # D1
                          detail={"exception_type": type(exc).__name__})
