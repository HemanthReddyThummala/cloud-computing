"""Pipeline stage contract and the production stage registry.

A *stage* is an async function ``(StageContext) -> StageOutput`` for one pipeline status.
Stages must be safe to repeat: after a crash the same stage may run again.

There are no fake or simulated production stages. Implemented so far:
    TRANSCRIBING                -> worker/transcribe.py (yt-dlp + youtube-transcript-api, ADR-14)
    SUMMARIZING                 -> worker/summarize.py (Gemini, ADR-15)
    EXTRACTING_FRAMES           -> worker/frames.py (yt-dlp download + FFmpeg, ADR-16)
    PUBLISHING                  -> worker/publish.py (Notion, ADR-17)
A job that reaches a stage missing from a registry is marked FAILED with PIPELINE_STAGE_UNAVAILABLE —
the worker never pretends a job completed.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from notesagent.common.states import JobStatus


@dataclass
class StageOutput:
    checkpoint: dict[str, Any] = field(default_factory=dict)   # durable result of this stage
    job_fields: dict[str, Any] = field(default_factory=dict)   # e.g. video_title, duration_sec
    warnings: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None                       # PUBLISHING only


@dataclass
class StageContext:
    job_id: uuid.UUID
    job: dict[str, Any]
    checkpoints: dict[str, dict[str, Any]]                     # outputs of earlier stages
    worker_id: str
    _credential_reader: Callable[[], Awaitable[str]]
    # Shared rate limiter (ADR-13). Every external call must first obtain a permit:
    #   await ctx.limiter.acquire(Provider.GEMINI, tokens=...)
    limiter: Any = None
    # ADR-22 demo fault switch: a DemoFaults object ONLY when DEMO_FAULTS_ENABLED=true, otherwise None.
    demo_faults: Any = None

    async def get_notion_token(self) -> str:
        """Decrypt the token from the credential mailbox (non-consuming read)."""
        return await self._credential_reader()


Stage = Callable[[StageContext], Awaitable[StageOutput]]
StageRegistry = Mapping[JobStatus, Stage]


def build_stage_registry(settings=None, *, youtube=None, gemini=None) -> dict[JobStatus, Stage]:
    """Production stages. ``youtube`` / ``gemini`` replace the real clients (tests only)."""
    from notesagent.common.config import get_settings
    from notesagent.worker.frames import make_frames_stage            # lazy: avoids an import cycle
    from notesagent.worker.publish import make_publish_stage
    from notesagent.worker.summarize import make_summarize_stage
    from notesagent.worker.transcribe import make_transcribe_stage
    settings = settings or get_settings()
    return {JobStatus.TRANSCRIBING: make_transcribe_stage(settings, youtube),
            JobStatus.SUMMARIZING: make_summarize_stage(settings, gemini),
            JobStatus.EXTRACTING_FRAMES: make_frames_stage(settings, youtube),
            JobStatus.PUBLISHING: make_publish_stage(settings)}
