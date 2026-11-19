"""Pipeline stage contract and the production stage registry.

A *stage* is an async function ``(StageContext) -> StageOutput`` for one pipeline status.
Stages must be safe to repeat: after a crash the same stage may run again.

Phase 1 ships NO stage implementations (no fake or simulated stages). Real stages are
registered in later phases:
    TRANSCRIBING / SUMMARIZING  -> Phase 3 (YouTube + Gemini)
    EXTRACTING_FRAMES           -> Phase 4 (ffmpeg)
    PUBLISHING                  -> Phase 5 (Notion)
Until then the worker marks a job FAILED with PIPELINE_STAGE_UNAVAILABLE — it never
pretends a job completed.
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

    async def get_notion_token(self) -> str:
        """Decrypt the token from the credential mailbox (non-consuming read)."""
        return await self._credential_reader()


Stage = Callable[[StageContext], Awaitable[StageOutput]]
StageRegistry = Mapping[JobStatus, Stage]


def build_stage_registry() -> dict[JobStatus, Stage]:
    """Production stages. Empty in Phase 1 by design."""
    return {}
