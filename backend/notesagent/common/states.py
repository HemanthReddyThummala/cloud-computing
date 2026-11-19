"""Job lifecycle state machine (PROJECT_SPEC §4).

The same transition table is enforced twice: here in Python, and by a database
trigger (migrations/001_init.sql), so a bug in one layer cannot corrupt state.
"""
from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    TRANSCRIBING = "TRANSCRIBING"
    SUMMARIZING = "SUMMARIZING"
    EXTRACTING_FRAMES = "EXTRACTING_FRAMES"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"


PIPELINE_ORDER: tuple[JobStatus, ...] = (
    JobStatus.TRANSCRIBING,
    JobStatus.SUMMARIZING,
    JobStatus.EXTRACTING_FRAMES,
    JobStatus.PUBLISHING,
)

TERMINAL: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS, JobStatus.FAILED, JobStatus.DEAD_LETTERED}
)

_FORWARD: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.TRANSCRIBING}),
    JobStatus.TRANSCRIBING: frozenset({JobStatus.SUMMARIZING}),
    JobStatus.SUMMARIZING: frozenset({JobStatus.EXTRACTING_FRAMES}),
    JobStatus.EXTRACTING_FRAMES: frozenset({JobStatus.PUBLISHING}),
    JobStatus.PUBLISHING: frozenset({JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS}),
}

# "any non-terminal -> FAILED / DEAD_LETTERED"
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    s: _FORWARD.get(s, frozenset()) | frozenset({JobStatus.FAILED, JobStatus.DEAD_LETTERED})
    for s in JobStatus
    if s not in TERMINAL
}

_PROGRESS: dict[JobStatus, int] = {
    JobStatus.QUEUED: 0,
    JobStatus.TRANSCRIBING: 10,
    JobStatus.SUMMARIZING: 30,
    JobStatus.EXTRACTING_FRAMES: 55,
    JobStatus.PUBLISHING: 85,
    JobStatus.COMPLETED: 100,
    JobStatus.COMPLETED_WITH_WARNINGS: 100,
}


def is_terminal(status: JobStatus | str) -> bool:
    return JobStatus(status) in TERMINAL


def can_transition(from_status: JobStatus | str, to_status: JobStatus | str) -> bool:
    return JobStatus(to_status) in ALLOWED_TRANSITIONS.get(JobStatus(from_status), frozenset())


def next_stage(status: JobStatus | str) -> JobStatus | None:
    """The next pipeline stage after ``status`` (None after PUBLISHING)."""
    status = JobStatus(status)
    if status is JobStatus.QUEUED:
        return PIPELINE_ORDER[0]
    if status in PIPELINE_ORDER:
        idx = PIPELINE_ORDER.index(status)
        return PIPELINE_ORDER[idx + 1] if idx + 1 < len(PIPELINE_ORDER) else None
    return None


def progress_for(status: JobStatus | str) -> int | None:
    """Progress % on entering ``status``; None means 'keep the current value' (failures)."""
    return _PROGRESS.get(JobStatus(status))


def all_transition_pairs() -> set[tuple[str, str]]:
    return {(a.value, b.value) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets}
