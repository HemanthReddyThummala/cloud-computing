"""TRANSCRIBING stage (ADR-14): title, true duration (constraint A) and timestamped transcript.

Order (each YouTube request first takes a permit from the shared limiter, ``Provider.YOUTUBE``):
1. ``yt-dlp`` metadata -> title + duration. A missing/invalid duration fails the job
   (``VIDEO_DURATION_UNKNOWN``); a duration above ``MAX_VIDEO_DURATION_SEC`` fails it with
   ``VIDEO_TOO_LONG`` — deterministically and BEFORE any transcript request. Exactly the maximum
   is accepted.
2. ``youtube-transcript-api`` list -> choose a transcript (preferred languages, manual before
   auto-generated; otherwise the first available one).
3. fetch -> segments ``{start, duration, end, text}`` in seconds, in their original order.

The result is returned as the stage checkpoint; the worker stores it together with the transition to
SUMMARIZING in ONE transaction (and title/duration on the job row), then acks. The stage has no side
effects of its own, so a redelivery before that commit simply runs it again; after the commit the job
resumes at SUMMARIZING and this stage is not repeated.

Errors are raised as classified errors (ADR-10): permanent ones (unavailable, private, deleted,
404/410, no transcript, age-restricted, too long) -> FAILED; 429 / blocking -> RateLimited (shared
cooldown + existing outbox retry); network and 5xx -> TransientError (existing bounded retry).
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable
from typing import Any

from notesagent.common.config import Settings
from notesagent.common.errors import FatalStageError, Provider
from notesagent.worker.stages import StageContext, StageOutput
from notesagent.worker.youtube import YouTubeClient, classify_metadata_error, classify_transcript_error

SOURCE = "youtube-transcript-api"
_MAX_TITLE = 500


def format_duration(seconds: float) -> str:
    total = int(math.ceil(seconds))
    hours, rest = divmod(total, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def valid_duration(value: Any) -> float | None:
    """A usable duration in seconds, or None. Never treats missing/invalid values as zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def check_duration(raw: Any, max_sec: int) -> float:
    duration = valid_duration(raw)
    if duration is None:
        raise FatalStageError("VIDEO_DURATION_UNKNOWN",
                              "The video's duration could not be determined (live or unsupported video).")
    if duration > max_sec:
        raise FatalStageError("VIDEO_TOO_LONG", f"The video is {format_duration(duration)} long; videos up to "
                                                f"{format_duration(max_sec)} are supported.")
    return duration


def select_transcript(transcripts: Iterable[Any], languages: list[str]) -> Any:
    """Preferred languages in priority order (manual before auto-generated), else the first
    available transcript (manual first). Raises the library's NoTranscriptFound-like error
    through the caller if the list is empty."""
    available = list(transcripts)
    for code in languages:
        for want_generated in (False, True):
            for item in available:
                if item.language_code == code and bool(item.is_generated) == want_generated:
                    return item
    for want_generated in (False, True):
        for item in available:
            if bool(item.is_generated) == want_generated:
                return item
    raise FatalStageError("YOUTUBE_TRANSCRIPT_UNAVAILABLE",
                          "This video has no transcript (captions are disabled or missing).", provider=Provider.YOUTUBE)


def to_segments(snippets: Iterable[Any]) -> list[dict[str, Any]]:
    segments = []
    for snippet in snippets:
        text = (getattr(snippet, "text", "") or "").strip()
        start, duration = float(getattr(snippet, "start", 0.0)), float(getattr(snippet, "duration", 0.0))
        if not text or not math.isfinite(start) or not math.isfinite(duration):
            continue
        segments.append({"start": round(start, 3), "duration": round(max(duration, 0.0), 3),
                         "end": round(start + max(duration, 0.0), 3), "text": text})
    return segments


def make_transcribe_stage(settings: Settings, client: Any | None = None):
    youtube = client or YouTubeClient(timeout_sec=settings.youtube_request_timeout_sec)

    async def call(ctx: StageContext, classify, fn, *args, demo_hook=None):
        await ctx.limiter.acquire(Provider.YOUTUBE)          # shared limiter BEFORE every YouTube request
        try:
            if demo_hook is not None:                        # ADR-22: may raise INSTEAD of the request
                await demo_hook(ctx)
            return await asyncio.to_thread(fn, *args)
        except Exception as exc:  # noqa: BLE001 - classified; upstream text is never kept
            raise classify(exc) from None

    async def transcribe(ctx: StageContext) -> StageOutput:
        video_id = ctx.job["video_id"]
        hook = None
        if ctx.demo_faults is not None:                      # ADR-22 (disabled by default)
            await ctx.demo_faults.bind(ctx)
            hook = ctx.demo_faults.before_youtube_metadata
        info = await call(ctx, classify_metadata_error, youtube.video_info, video_id, demo_hook=hook)
        if not isinstance(info, dict):
            raise FatalStageError("VIDEO_DURATION_UNKNOWN", "The video's metadata could not be read.")
        duration = check_duration(info.get("duration"), settings.max_video_duration_sec)   # before the transcript
        title = str(info.get("title") or "").strip()[:_MAX_TITLE] or None

        transcripts = await call(ctx, classify_transcript_error, youtube.transcript_list, video_id)
        chosen = select_transcript(transcripts, settings.transcript_language_list)
        fetched = await call(ctx, classify_transcript_error, youtube.fetch, chosen)
        segments = to_segments(getattr(fetched, "snippets", fetched))
        if not segments:
            raise FatalStageError("YOUTUBE_TRANSCRIPT_UNAVAILABLE", "The video's transcript is empty.",
                                  provider=Provider.YOUTUBE)

        duration_sec = int(round(duration))
        checkpoint = {
            "video_id": video_id, "title": title, "duration_sec": duration_sec, "duration_exact": duration,
            "transcript": {"source": SOURCE, "language_code": getattr(fetched, "language_code", chosen.language_code),
                           "language": getattr(fetched, "language", getattr(chosen, "language", None)),
                           "is_generated": bool(getattr(fetched, "is_generated", chosen.is_generated)),
                           "segment_count": len(segments), "segments": segments},
        }
        return StageOutput(checkpoint=checkpoint, job_fields={"video_title": title, "duration_sec": duration_sec})

    return transcribe
