"""Demo fault switch (ADR-22): deterministic, live failure demonstrations for spec §6.

Disabled by default. Two independent gates must both be open for a fault to happen:

1. ``DEMO_FAULTS_ENABLED=true`` in the WORKER's environment. If false, the runner passes no
   ``DemoFaults`` object to the stages (``ctx.demo_faults is None``) and nothing below ever runs.
2. An operator arms ONE video for ONE scenario through the CLI inside a worker container
   (``python -m notesagent.ops.demo_faults arm <scenario> --video <id>``). The arming lives in Redis
   with a 15-minute TTL. The first job of that video to start TRANSCRIBING claims it atomically
   (``GETDEL``) and binds it to its job ID; only that job's stages and retries see the fault.

Each fault is raised at the provider/stage boundary INSTEAD of the external call it replaces, using the
same exception type a real failure produces, so the unchanged production code classifies it, retries,
cools down, warns, publishes or dead-letters. Any Redis problem means "no fault" (fail-safe).
"""
from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)

ARM_TTL_SEC = 15 * 60            # an unused arming expires after 15 minutes
BIND_TTL_SEC = 2 * 60 * 60       # a bound job keeps its scenario across retries for up to 2 hours
FRAME_FAILURE_REASON = "DEMO_INJECTED_FAILURE"


class Scenario(StrEnum):
    DLQ = "dlq"                          # retryable YouTube network failure on every attempt -> DLQ
    GEMINI_429_ONCE = "gemini_429_once"  # one Gemini 429 (Retry-After 20 s), then the real request
    FRAMES_PARTIAL = "frames_partial"    # every second frame fails (index 0 always succeeds)
    FRAMES_ALL = "frames_all"            # every frame fails -> text-only page


GEMINI_RETRY_AFTER = "20s"


def video_key(video_id: str) -> str:
    return f"demo:fault:video:{video_id}"


def job_key(job_id: Any) -> str:
    return f"demo:fault:job:{job_id}"


def gemini_done_key(job_id: Any) -> str:
    return f"demo:fault:job:{job_id}:gemini_429_done"


def frame_fails(scenario: str | None, index: int) -> bool:
    """Pure decision for the frame loop."""
    if scenario == Scenario.FRAMES_ALL:
        return True
    if scenario == Scenario.FRAMES_PARTIAL:
        return index % 2 == 1
    return False


def youtube_network_error() -> Exception:
    """What yt-dlp raises when YouTube cannot be reached (classified as YOUTUBE_UNREACHABLE)."""
    from yt_dlp.networking.exceptions import TransportError
    return TransportError("demo fault: simulated network failure")


def gemini_429_error() -> Exception:
    """What google-genai raises for a Gemini 429 with RetryInfo (classified as GEMINI_RATE_LIMITED)."""
    from google.genai import errors
    return errors.ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "demo fault: simulated rate limit",
        "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": GEMINI_RETRY_AFTER}]}})


def _decode(value: Any) -> str | None:
    if value is None:
        return None
    text = value.decode() if isinstance(value, bytes) else str(value)
    return text if text in set(Scenario) else None


class DemoFaults:
    """Created by the runner ONLY when ``demo_faults_enabled`` is true."""

    def __init__(self, redis: Any) -> None:
        self.redis = redis

    async def _safe(self, op, *args, **kwargs):
        try:
            return await op(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - fail-safe: Redis trouble never injects a fault
            log.debug("demo fault lookup failed; no fault", extra={"error_type": type(exc).__name__})
            return None

    async def bind(self, ctx: Any) -> str | None:
        """Claim an armed video for this job (atomic GETDEL); returns this job's scenario, if any."""
        bound = _decode(await self._safe(self.redis.get, job_key(ctx.job_id)))
        if bound:
            return bound
        armed = _decode(await self._safe(self.redis.getdel, video_key(ctx.job["video_id"])))
        if not armed:
            return None
        if await self._safe(self.redis.set, job_key(ctx.job_id), armed, ex=BIND_TTL_SEC) is None:
            return None                                       # could not bind -> no fault (fail-safe)
        log.warning("demo fault bound to job", extra={"job_id": str(ctx.job_id), "scenario": armed,
                                                       "video_id": ctx.job["video_id"]})
        return armed

    async def scenario(self, job_id: Any) -> str | None:
        return _decode(await self._safe(self.redis.get, job_key(job_id)))

    def _injected(self, ctx: Any, scenario: str, stage: str, **extra: Any) -> None:
        log.warning("demo fault injected", extra={"job_id": str(ctx.job_id), "scenario": scenario, "stage": stage,
                                                   "attempt": ctx.job.get("attempts"), **extra})

    async def before_youtube_metadata(self, ctx: Any) -> None:
        """TRANSCRIBING, first YouTube request: `dlq` fails it on every attempt."""
        if await self.scenario(ctx.job_id) == Scenario.DLQ:
            self._injected(ctx, Scenario.DLQ, "TRANSCRIBING")
            raise youtube_network_error()

    async def before_gemini_request(self, ctx: Any) -> None:
        """SUMMARIZING, before the Gemini request: `gemini_429_once` fails exactly one request per job."""
        if await self.scenario(ctx.job_id) != Scenario.GEMINI_429_ONCE:
            return
        first = await self._safe(self.redis.set, gemini_done_key(ctx.job_id), "1", nx=True, ex=BIND_TTL_SEC)
        if first:                                             # None (Redis error) or False (already done)
            self._injected(ctx, Scenario.GEMINI_429_ONCE, "SUMMARIZING", retry_after=GEMINI_RETRY_AFTER)
            raise gemini_429_error()

    def frame_injected(self, ctx: Any, scenario: str | None, index: int) -> bool:
        if frame_fails(scenario, index):
            self._injected(ctx, scenario, "EXTRACTING_FRAMES", frame_index=index)
            return True
        return False
