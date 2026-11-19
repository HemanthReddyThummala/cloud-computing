"""Pure rules for broker dead letters (ADR-12). No I/O: parsing and decisions only.

A message on ``jobs.dlq.audit`` is one of:
- a **broker dead letter**: RabbitMQ dead-lettered a job message from the work queue
  (``delivery_limit`` after repeated consumer crashes, or ``rejected`` = malformed message);
- a **worker copy**: the DLQ copy a worker publishes (via the outbox) when it dead-letters a job
  itself after exhausting retries (Part 3). The job is already DEAD_LETTERED; nothing to do.
- anything else, or contradictory metadata -> **inconsistent**: recorded, the job is never changed.

Verified on RabbitMQ 4.4.1 (docs/session_logs/phase2_part4_broker_probe.md): ``x-death`` is newest
first; ``x-first-death-*`` describes the OLDEST death (e.g. ``expired`` in a delay queue) and must not
be used; ``x-last-death-*`` describes the newest one.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from notesagent.common import states
from notesagent.common.broker import HEADER_EVENT_TYPE, LEGACY_QUEUE_WORK, QUEUE_WORK

WORK_QUEUES = frozenset({QUEUE_WORK, LEGACY_QUEUE_WORK})
BROKER_REASONS = frozenset({"delivery_limit", "rejected"})
WORKER_COPY_EVENT_TYPE = "job.dead_lettered"

# Outcomes stored in dead_letter_audit.outcome (migration 004).
APPLIED = "applied"
ALREADY_TERMINAL = "already_terminal"
UNKNOWN_JOB = "unknown_job"
UNREADABLE = "unreadable"
STALE = "stale"
INCONSISTENT = "inconsistent"

ERROR_CODE = "DEAD_LETTERED_BY_BROKER"
_MESSAGES = {
    "delivery_limit": "Processing this job repeatedly crashed the workers, so it was moved to the "
                      "dead-letter queue for inspection.",
    "rejected": "The job message could not be read, so it was moved to the dead-letter queue for inspection.",
}


@dataclass(frozen=True)
class DeadLetter:
    kind: str                      # "broker" | "worker_copy" | "inconsistent"
    death_reason: str              # delivery_limit | rejected | unknown (stored in the audit row)
    source_queue: str | None
    job_id: uuid.UUID | None       # resolved job id (None if unreadable or conflicting)
    message_id: str | None
    dedup_key: str
    problem: str | None = None     # why it is inconsistent / unreadable (audit detail)
    detail: dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else None


def _uuid(value: Any) -> uuid.UUID | None:
    text = _text(value)
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _body_json(body: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def dedup_key(message_id: str | None, body: bytes, reason: str) -> str:
    """P4-D8: message id + reason; without a message id, the SHA-256 of the body (never the body)."""
    if message_id:
        return f"{message_id[:255]}:{reason}"
    return f"sha256:{hashlib.sha256(body).hexdigest()}:{reason}"


def resolve_job_id(payload: dict[str, Any] | None, headers: dict[str, Any]) -> tuple[uuid.UUID | None, str | None]:
    """P4-D9. Returns (job_id, problem). Body and header ids must be valid UUIDs; if both are valid
    they must agree; exactly one valid -> use it; none -> unreadable; conflict -> no id."""
    body_id = _uuid(payload.get("job_id")) if payload else None
    header_id = _uuid(headers.get("x-job-id"))
    if body_id and header_id and body_id != header_id:
        return None, "job_id_mismatch"
    job_id = body_id or header_id
    return (job_id, None) if job_id else (None, "job_id_unreadable")


def _death(headers: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """P4-D1. Returns (reason, queue, problem) of the newest death."""
    deaths = headers.get("x-death")
    if not isinstance(deaths, (list, tuple)) or not deaths or not isinstance(deaths[0], dict):
        return None, None, "no_death_metadata"
    newest = deaths[0]
    reason, queue = _text(newest.get("reason")), _text(newest.get("queue"))
    last_reason, last_queue = _text(headers.get("x-last-death-reason")), _text(headers.get("x-last-death-queue"))
    if (reason, queue) != (last_reason, last_queue):
        return None, queue, "death_headers_disagree"
    if queue not in WORK_QUEUES:
        return None, queue, "death_not_from_work_queue"
    if reason not in BROKER_REASONS:
        return None, queue, "unexpected_death_reason"
    return reason, queue, None


def parse(body: bytes, headers: dict[str, Any] | None, message_id: str | None) -> DeadLetter:
    headers = dict(headers or {})
    payload = _body_json(body)
    body_type = payload.get("event_type") if payload else None
    marker = _text(headers.get(HEADER_EVENT_TYPE))
    has_death = bool(headers.get("x-death")) or any(k.startswith("x-last-death") for k in headers)
    job_id, id_problem = resolve_job_id(payload, headers)

    def build(kind: str, reason: str, queue: str | None, problem: str | None) -> DeadLetter:
        return DeadLetter(kind=kind, death_reason=reason, source_queue=queue, job_id=job_id,
                          message_id=message_id, dedup_key=dedup_key(message_id, body, reason), problem=problem,
                          detail={"event_type_header": marker, "body_event_type": body_type})

    if not has_death:
        # P4-D2: a worker copy needs BOTH the explicit marker and the matching body type.
        if marker == WORKER_COPY_EVENT_TYPE and body_type == WORKER_COPY_EVENT_TYPE:
            return build("worker_copy", "unknown", None, None)
        problem = "origin_conflict" if WORKER_COPY_EVENT_TYPE in (marker, body_type) else "no_death_metadata"
        return build("inconsistent", "unknown", None, problem)

    if WORKER_COPY_EVENT_TYPE in (marker, body_type):
        return build("inconsistent", "unknown", None, "origin_conflict")     # a worker copy never has a death
    reason, queue, problem = _death(headers)
    if problem:
        return build("inconsistent", "unknown", queue, problem)
    if id_problem == "job_id_mismatch":
        return build("inconsistent", reason, queue, id_problem)
    return build("broker", reason, queue, id_problem)        # id_problem "job_id_unreadable" -> unreadable


def outcome_without_job(dl: DeadLetter) -> str | None:
    """Outcome decided from the message alone (no database read needed), or None."""
    if dl.kind == "inconsistent":
        return INCONSISTENT
    if dl.job_id is None:
        return UNREADABLE
    if not dl.message_id:
        return INCONSISTENT      # a missing message id is never proof of anything: no transition
    return None


def decide(job: dict[str, Any] | None, dl: DeadLetter) -> str:
    """P4-D3: outcome for a readable broker dead letter, given the locked job row."""
    if job is None:
        return UNKNOWN_JOB
    if states.is_terminal(job["status"]):
        return ALREADY_TERMINAL
    retry = job.get("retry")
    if retry and retry.get("event_id") != dl.message_id:
        return STALE             # a newer retry is scheduled; the dead letter is an outdated copy
    return APPLIED


def job_error(reason: str) -> dict[str, Any]:
    """P4-D6: the error stored on the job (and shown by the API) for an applied broker dead letter."""
    return {"code": ERROR_CODE, "message": _MESSAGES[reason], "reason": reason}
