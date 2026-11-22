"""Shared external-API rate limiter (ADR-13), backed by the existing Redis.

Every worker process asks this limiter for permission BEFORE each external call:

    permit = await limiter.acquire(Provider.GEMINI, tokens=estimated_input_tokens)
    ... call Gemini ...
    await limiter.settle(permit, actual_input_tokens)        # optional TPM correction

Each check is ONE atomic Redis Lua script that uses Redis' own clock (``TIME``), so all workers see
the same time and two workers can never both take the last slot.

Limits
- **Gemini** (per Google project): requests per minute and input tokens per minute as sliding
  60-second windows, plus requests per day counted per *Pacific* calendar day (Google resets RPD at
  midnight Pacific). The configured values are used as given (the user's AI Studio limits × 0.8).
- **Notion**: requests per second per connection, enforced as strict spacing (2.5/s = one request
  every 400 ms; Notion documents an average of 3/s per connection).
- **YouTube**: no documented quota, so no bucket — only the shared cooldown below.

Shared cooldown: when any provider answers 429 (or a quota is exhausted), ``report()`` stores
"do not call before T" in Redis; every worker's ``acquire()`` for that provider waits for it. T is
``Retry-After`` when the provider sent one, otherwise ``RATE_LIMIT_DEFAULT_COOLDOWN_SEC``; for a daily
quota it is the quota reset time.

When a limit is reached, ``acquire`` waits (sleeps) if the wait fits into
``RATE_LIMIT_MAX_WAIT_SEC``; otherwise it raises a classified ``RateLimited`` / ``QuotaExhausted`` and
the existing retry system (ADR-11) re-queues the job. If Redis cannot be reached, ``acquire`` raises
``RateLimiterUnavailable`` (infrastructure budget) — the provider is never called without a permit.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from redis.exceptions import RedisError

from notesagent.common.config import Settings
from notesagent.common.errors import (
    FatalStageError,
    PipelineError,
    Provider,
    QuotaExhausted,
    RateLimited,
    RateLimiterUnavailable,
)
from notesagent.common.http_errors import PACIFIC, next_pacific_midnight

log = logging.getLogger(__name__)

KEY_PREFIX = "ratelimit"
GEMINI_WINDOW_SEC = 60.0
LIMITED_PROVIDERS = (Provider.GEMINI, Provider.NOTION, Provider.YOUTUBE)

# Codes of errors raised by the limiter itself (no provider was called). They are not reported back
# as a provider 429 cooldown.
LOCAL_CODES = frozenset({"GEMINI_LOCAL_RATE_LIMIT", "NOTION_LOCAL_RATE_LIMIT", "GEMINI_DAILY_LIMIT_REACHED",
                         "GEMINI_COOLDOWN_ACTIVE", "NOTION_COOLDOWN_ACTIVE", "YOUTUBE_COOLDOWN_ACTIVE",
                         "RATE_LIMITER_UNAVAILABLE"})

_GRANTED, _WAIT, _DAILY_EXHAUSTED, _COOLDOWN = 1, 0, 2, 3

# Redis time in milliseconds (shared clock for every worker).
_NOW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
"""

_COOLDOWN_CHECK = """
local cooldown = tonumber(redis.call('GET', KEYS[1]) or '0')
if cooldown > now then return {3, cooldown - now, now} end
"""

# KEYS: cooldown, rpm zset, tpm zset, rpd counter
# ARGV: rpm_limit, tpm_limit, rpd_limit, window_ms, tokens, member, rpd_ttl_ms
_GEMINI = _NOW + _COOLDOWN_CHECK + """
if tonumber(redis.call('GET', KEYS[4]) or '0') >= tonumber(ARGV[3]) then return {2, 0, now} end
local window = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - window)
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now - window)
local wait = 0
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[1]) then
  local oldest = redis.call('ZRANGE', KEYS[2], 0, 0, 'WITHSCORES')
  wait = tonumber(oldest[2]) + window - now
end
local tokens = tonumber(ARGV[5])
if tokens > 0 then
  local entries = redis.call('ZRANGE', KEYS[3], 0, -1, 'WITHSCORES')
  local used = 0
  for i = 1, #entries, 2 do used = used + tonumber(string.match(entries[i], ':(%d+)$')) end
  local excess = used + tokens - tonumber(ARGV[2])
  if excess > 0 then
    local freed = 0
    for i = 1, #entries, 2 do
      freed = freed + tonumber(string.match(entries[i], ':(%d+)$'))
      if freed >= excess then
        local w = tonumber(entries[i + 1]) + window - now
        if w > wait then wait = w end
        break
      end
    end
  end
end
if wait > 0 then return {0, wait, now} end
redis.call('ZADD', KEYS[2], now, ARGV[6])
redis.call('PEXPIRE', KEYS[2], window * 2)
if tokens > 0 then
  redis.call('ZADD', KEYS[3], now, ARGV[6] .. ':' .. ARGV[5])
  redis.call('PEXPIRE', KEYS[3], window * 2)
end
redis.call('INCR', KEYS[4])
redis.call('PEXPIRE', KEYS[4], tonumber(ARGV[7]))
return {1, 0, now}
"""

# KEYS: cooldown, next-allowed-time key.  ARGV: interval_ms
_SPACING = _NOW + _COOLDOWN_CHECK + """
local next_at = tonumber(redis.call('GET', KEYS[2]) or '0')
if next_at > now then return {0, next_at - now, now} end
local interval = tonumber(ARGV[1])
redis.call('SET', KEYS[2], now + interval, 'PX', interval + 1000)
return {1, 0, now}
"""

# KEYS: cooldown
_COOLDOWN_ONLY = _NOW + _COOLDOWN_CHECK + "return {1, 0, now}"

# KEYS: cooldown.  ARGV: duration_ms.  Never shortens an existing cooldown.
_SET_COOLDOWN = _NOW + """
local until_at = now + tonumber(ARGV[1])
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if until_at > current then
  redis.call('SET', KEYS[1], until_at, 'PX', tonumber(ARGV[1]))
  return until_at
end
return current
"""

# KEYS: tpm zset.  ARGV: old member, new member.  Corrects a reservation's token count.
_SETTLE = """
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score then return 0 end
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[1], score, ARGV[2])
return 1
"""


@dataclass(frozen=True)
class Permit:
    provider: Provider
    granted_at_ms: int          # Redis time
    reservation: str
    tokens: int = 0


def pacific_day(now: datetime) -> str:
    """The Pacific calendar date that Google's RPD quota counts against (DST-correct)."""
    return now.astimezone(PACIFIC).date().isoformat()


class RateLimiter:
    def __init__(self, redis, settings: Settings, *, gemini_window_sec: float = GEMINI_WINDOW_SEC,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.redis = redis
        self.settings = settings
        self.window_ms = int(gemini_window_sec * 1000)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._gemini = redis.register_script(_GEMINI)
        self._spacing = redis.register_script(_SPACING)
        self._cooldown_only = redis.register_script(_COOLDOWN_ONLY)
        self._set_cooldown = redis.register_script(_SET_COOLDOWN)
        self._settle = redis.register_script(_SETTLE)

    # ------------------------------------------------------------ keys
    @property
    def _gemini_name(self) -> str:
        """``gemini``, or ``gemini-<namespace>`` (ADR-23: one set of counters per Google Cloud project).
        The default empty namespace keeps the original key names byte-for-byte."""
        namespace = self.settings.gemini_limiter_namespace
        return f"gemini-{namespace}" if namespace else "gemini"

    def cooldown_key(self, provider: Provider) -> str:
        provider = Provider(provider)
        name = self._gemini_name if provider is Provider.GEMINI else provider.value.lower()
        return f"{KEY_PREFIX}:cooldown:{name}"

    def _gemini_keys(self, now: datetime) -> list[str]:
        base = f"{KEY_PREFIX}:{self._gemini_name}"
        return [self.cooldown_key(Provider.GEMINI), f"{base}:rpm", f"{base}:tpm", f"{base}:rpd:{pacific_day(now)}"]

    # ------------------------------------------------------------ acquire
    async def acquire(self, provider: Provider, *, tokens: int = 0, connection: str = "default",
                      max_wait_sec: float | None = None) -> Permit:
        """Wait for permission to call ``provider`` once (with ``tokens`` estimated input tokens for
        Gemini). ``connection`` separates Notion connections (pass a hash, never a token)."""
        provider = Provider(provider)
        if provider not in LIMITED_PROVIDERS:
            raise ValueError(f"no rate limit defined for {provider}")
        if tokens < 0 or (provider is Provider.GEMINI and tokens > self.settings.gemini_tpm_limit):
            raise FatalStageError("LLM_REQUEST_TOO_LARGE",
                                  "The request is larger than the configured tokens-per-minute limit.")
        max_wait = self.settings.rate_limit_max_wait_sec if max_wait_sec is None else max_wait_sec
        deadline = time.monotonic() + max_wait
        reservation = uuid.uuid4().hex
        while True:
            now = self.clock()
            status, wait_ms, redis_now = await self._check(provider, tokens, connection, reservation, now)
            if status == _GRANTED:
                return Permit(provider, int(redis_now), reservation, tokens)
            if status == _DAILY_EXHAUSTED:
                raise QuotaExhausted("GEMINI_DAILY_LIMIT_REACHED",
                                     "The configured Gemini requests-per-day limit is used up.",
                                     provider=provider, resets_at=next_pacific_midnight(now))
            wait = wait_ms / 1000
            if time.monotonic() + wait > deadline:
                code = (f"{provider.value}_COOLDOWN_ACTIVE" if status == _COOLDOWN
                        else f"{provider.value}_LOCAL_RATE_LIMIT")
                raise RateLimited(code, "Waiting for the shared rate limit.", provider=provider,
                                  retry_after_sec=round(wait, 3))
            await asyncio.sleep(wait + random.uniform(0.0, 0.05))   # small jitter: workers do not wake together

    async def _check(self, provider: Provider, tokens: int, connection: str, reservation: str,
                     now: datetime) -> tuple[int, int, int]:
        try:
            if provider is Provider.GEMINI:
                ttl_ms = int((next_pacific_midnight(now) - now + timedelta(days=1)).total_seconds() * 1000)
                result = await self._gemini(
                    keys=self._gemini_keys(now),
                    args=[self.settings.gemini_rpm_limit, self.settings.gemini_tpm_limit,
                          self.settings.gemini_rpd_limit, self.window_ms, int(tokens), reservation, ttl_ms])
            elif provider is Provider.NOTION:
                interval_ms = int(-(-1000 // self.settings.notion_rps_limit))      # ceil: never faster
                result = await self._spacing(
                    keys=[self.cooldown_key(provider), f"{KEY_PREFIX}:notion:next:{connection}"],
                    args=[interval_ms])
            else:
                result = await self._cooldown_only(keys=[self.cooldown_key(provider)])
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            log.warning("rate limiter unavailable; provider not called",
                        extra={"provider": provider.value, "error_type": type(exc).__name__})
            raise RateLimiterUnavailable("RATE_LIMITER_UNAVAILABLE",
                                         "The shared rate limiter is unavailable.", provider=provider) from None
        return int(result[0]), int(result[1]), int(result[2])

    # ------------------------------------------------------------ after the call
    async def settle(self, permit: Permit, actual_tokens: int) -> None:
        """Replace the estimated input tokens of a Gemini permit with the real count (best effort)."""
        if permit.provider is not Provider.GEMINI or actual_tokens == permit.tokens or permit.tokens <= 0:
            return
        try:
            await self._settle(keys=[f"{KEY_PREFIX}:{self._gemini_name}:tpm"],
                               args=[f"{permit.reservation}:{permit.tokens}",
                                     f"{permit.reservation}:{max(0, int(actual_tokens))}"])
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            log.warning("could not settle token count", extra={"error_type": type(exc).__name__})

    async def report(self, error: PipelineError) -> float | None:
        """A provider answered 429 / quota exhausted: share a cooldown with every worker.
        Returns the cooldown in seconds, or None if nothing was recorded. Best effort: a Redis
        failure here is logged, never raised (the retry itself is still scheduled)."""
        if not isinstance(error, (RateLimited, QuotaExhausted)) or error.code in LOCAL_CODES:
            return None
        if error.provider not in LIMITED_PROVIDERS:
            return None
        if isinstance(error, QuotaExhausted):
            seconds = max(1.0, (error.resets_at - self.clock()).total_seconds())
        else:
            seconds = error.retry_after_sec or self.settings.rate_limit_default_cooldown_sec
        try:
            await self._set_cooldown(keys=[self.cooldown_key(error.provider)], args=[int(seconds * 1000)])
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            log.warning("could not record provider cooldown",
                        extra={"provider": error.provider.value, "error_type": type(exc).__name__})
            return None
        log.info("provider cooldown shared with all workers",
                 extra={"provider": error.provider.value, "code": error.code, "cooldown_sec": round(seconds, 3)})
        return seconds
