"""Pure retry decisions (Phase 2, Part 3 — ADR-11).

Given the job row, a classified retryable error and the settings, decide WHAT happens next:
schedule a retry (which counter, attempt number, delay, deadline, delay-queue bucket, new event
id) or dead-letter the job (terminal error document). No I/O here; the worker executes the plan
in ONE PostgreSQL transaction together with the outbox row (jobs_repo.schedule_retry /
jobs_repo.dead_letter).

Rules (approved P3-D1..D6):
  * Provider errors use ``attempts`` / MAX_ATTEMPTS; ``RateLimiterUnavailable`` uses
    ``infra_attempts`` / INFRA_MAX_ATTEMPTS (the provider was not called).
  * MAX_ATTEMPTS counts total executions: the n-th failure dead-letters when n >= limit.
  * Provider delay = BACKOFF_SCHEDULE_SEC[min(n-1, last)] (last step repeats); for RateLimited it is
    max(step, retry_after_sec). QuotaExhausted uses the normal step (no waiting until reset).
  * The deadline ``next_attempt_at`` stores the actually selected delay. Long delays are reached by
    delay-queue hops; a stage never runs before the deadline.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from notesagent.common.config import Settings
from notesagent.common.errors import PipelineError, QuotaExhausted, RateLimited, RetryBudget

ATTEMPT_COLUMNS = {RetryBudget.PROVIDER: "attempts", RetryBudget.INFRASTRUCTURE: "infra_attempts"}


@dataclass(frozen=True)
class RetryPlan:
    attempt_column: str
    attempt: int                 # failures so far, including this one
    max_attempts: int
    delay_sec: float
    next_attempt_at: datetime
    routing_key_seconds: int     # first delay-queue bucket
    event_id: uuid.UUID          # NEW identity: the only message allowed to run the next attempt
    retry_doc: dict[str, Any]


@dataclass(frozen=True)
class DeadLetterPlan:
    attempt_column: str
    attempt: int
    error: dict[str, Any]
    event_id: uuid.UUID          # identity of the DLQ copy (outbox event)


def backoff_step(attempt: int, schedule: list[int]) -> int:
    return schedule[min(attempt, len(schedule)) - 1]


def choose_bucket(remaining_sec: float, buckets: list[int]) -> int:
    """Largest bucket <= remaining (never overshoots by more than one small hop), else the smallest."""
    ordered = sorted(buckets)
    fitting = [b for b in ordered if b <= remaining_sec]
    return fitting[-1] if fitting else ordered[0]


def plan_failure(job: dict[str, Any], error: PipelineError, settings: Settings, now: datetime) -> RetryPlan | DeadLetterPlan:
    if not error.retryable:
        raise ValueError("plan_failure is only for retryable errors")
    infra = error.budget is RetryBudget.INFRASTRUCTURE
    column = ATTEMPT_COLUMNS[error.budget]
    attempt = int(job[column]) + 1
    limit = settings.infra_max_attempts if infra else settings.max_attempts

    if attempt >= limit:
        terminal: dict[str, Any] = {
            "code": "RETRIES_EXHAUSTED",
            "message": ("The shared rate limiter stayed unavailable; the job was not completed." if infra else
                        "The job kept failing with a retryable error and its retry budget is exhausted."),
            "last_error_code": error.code,
            "attempts": attempt,
        }
        if isinstance(error, QuotaExhausted):
            terminal["resets_at"] = error.resets_at.isoformat()      # clarification A: keep the reset time
        return DeadLetterPlan(column, attempt, terminal, uuid.uuid4())

    if infra:
        delay = float(settings.infra_retry_delay_sec)
    else:
        delay = float(backoff_step(attempt, settings.backoff_delays))
        if isinstance(error, RateLimited) and error.retry_after_sec is not None:
            delay = max(delay, float(error.retry_after_sec))       # stored deadline uses the real delay
    deadline = now + timedelta(seconds=delay)
    event_id = uuid.uuid4()
    doc: dict[str, Any] = {
        "event_id": str(event_id),
        "attempt": attempt,
        "max_attempts": limit,
        "budget": error.budget.value,
        "reason_code": error.code,
        "provider": error.provider.value if error.provider else None,
        "delay_sec": delay,
        "next_attempt_at": deadline.isoformat(),
    }
    if isinstance(error, QuotaExhausted):
        doc["resets_at"] = error.resets_at.isoformat()
    return RetryPlan(column, attempt, limit, delay, deadline,
                     choose_bucket(delay, settings.delay_queue_seconds), event_id, doc)


def seconds_until(retry_doc: dict[str, Any], now: datetime) -> float:
    """Time left before a scheduled retry may run (<= 0 means it may run now)."""
    deadline = datetime.fromisoformat(retry_doc["next_attempt_at"])
    return (deadline - now).total_seconds()


def public_retry_view(retry_doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """What GET /jobs/{id} shows: no event_id, budget or other internals."""
    if not retry_doc:
        return None
    return {"reason": retry_doc.get("reason_code"), "attempt": retry_doc.get("attempt"),
            "max_attempts": retry_doc.get("max_attempts"), "next_attempt_at": retry_doc.get("next_attempt_at")}
