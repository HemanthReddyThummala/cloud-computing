"""ADR-12 pure rules: broker death parsing (P4-D1), worker-copy detection (D2), stale decision (D3),
dedup key (D8), job id resolution (D9), error document (D6) and the audit delivery-limit rule (D5).
Header layouts are the ones captured from RabbitMQ 4.4.1 (docs/session_logs/phase2_part4_broker_probe.md)."""
import hashlib
import json
import pathlib
import re
import uuid

import pytest

from notesagent.common.config import get_settings
from notesagent.worker import dead_letters as d

JOB = str(uuid.uuid4())
MID = str(uuid.uuid4())


def body(event_type="job.submitted", job_id=JOB):
    return json.dumps({"event_id": MID, "event_type": event_type, "job_id": job_id, "schema": 1}).encode()


def death(reason, queue, exchange="jobs"):
    return {"count": 1, "reason": reason, "queue": queue, "time": "2026-11-19T11:26:50+00:00",
            "exchange": exchange, "routing-keys": ["job.work"]}


def headers(deaths, *, last=None, first=None, job_id=JOB, event_type="job.submitted"):
    last = last or (deaths[0]["reason"], deaths[0]["queue"])
    first = first or (deaths[-1]["reason"], deaths[-1]["queue"])
    return {"x-job-id": job_id, "x-event-type": event_type, "x-death": deaths, "x-delivery-count": 11,
            "x-last-death-reason": last[0], "x-last-death-queue": last[1],
            "x-first-death-reason": first[0], "x-first-death-queue": first[1]}


# ---------------------------------------------------------------- P4-D1 death parsing
def test_delivery_limit_from_work_queue():
    dl = d.parse(body(), headers([death("delivery_limit", "jobs.work.v2")]), MID)
    assert (dl.kind, dl.death_reason, dl.source_queue, dl.job_id) == ("broker", "delivery_limit", "jobs.work.v2",
                                                                      uuid.UUID(JOB))
    assert d.outcome_without_job(dl) is None


def test_rejected_from_work_queue():
    dl = d.parse(body(), headers([death("rejected", "jobs.work.v2")]), MID)
    assert (dl.kind, dl.death_reason) == ("broker", "rejected")


def test_retry_hop_then_crash_loop_uses_newest_death_not_first():
    """Captured A3/F1 layout: x-first-death-reason is 'expired' (delay queue); the real reason is newest."""
    hs = headers([death("delivery_limit", "jobs.work.v2"), death("expired", "jobs.retry.10s", "jobs.retry")])
    assert hs["x-first-death-reason"] == "expired"
    dl = d.parse(body(event_type="job.retry"), hs, MID)
    assert (dl.kind, dl.death_reason, dl.source_queue) == ("broker", "delivery_limit", "jobs.work.v2")


def test_legacy_work_queue_is_accepted():
    dl = d.parse(body(), headers([death("delivery_limit", "jobs.work")]), MID)
    assert (dl.kind, dl.source_queue) == ("broker", "jobs.work")


@pytest.mark.parametrize("hs,problem", [
    (headers([death("delivery_limit", "jobs.work.v2")], last=("rejected", "jobs.work.v2")), "death_headers_disagree"),
    (headers([death("delivery_limit", "jobs.work.v2")], last=("delivery_limit", "jobs.dlq.audit")),
     "death_headers_disagree"),
    (headers([death("expired", "jobs.retry.10s")]), "death_not_from_work_queue"),
    (headers([death("delivery_limit", "jobs.dlq.audit")]), "death_not_from_work_queue"),
    (headers([death("maxlen", "jobs.work.v2")]), "unexpected_death_reason"),
    ({"x-job-id": JOB, "x-event-type": "job.submitted", "x-death": [],
      "x-last-death-reason": "delivery_limit", "x-last-death-queue": "jobs.work.v2"}, "no_death_metadata"),
    ({"x-job-id": JOB, "x-event-type": "job.submitted", "x-death": [death("delivery_limit", "jobs.work.v2")]},
     "death_headers_disagree"),
])
def test_missing_or_inconsistent_death_metadata_never_transitions(hs, problem):
    dl = d.parse(body(), hs, MID)
    assert (dl.kind, dl.death_reason, dl.problem) == ("inconsistent", "unknown", problem)
    assert d.outcome_without_job(dl) == d.INCONSISTENT


def test_bytes_header_values_are_decoded():
    hs = headers([{"reason": b"delivery_limit", "queue": b"jobs.work.v2"}])
    hs["x-last-death-reason"], hs["x-last-death-queue"] = b"delivery_limit", b"jobs.work.v2"
    assert d.parse(body(), hs, MID).kind == "broker"


# ---------------------------------------------------------------- P4-D2 worker copies
def test_worker_copy_needs_marker_and_body_type():
    hs = {"x-job-id": JOB, "x-event-type": "job.dead_lettered"}
    assert d.parse(body("job.dead_lettered"), hs, MID).kind == "worker_copy"


@pytest.mark.parametrize("marker,body_type", [("job.dead_lettered", "job.submitted"),
                                              (None, "job.dead_lettered"),
                                              ("job.submitted", "job.dead_lettered")])
def test_conflicting_origin_metadata_is_inconsistent(marker, body_type):
    hs = {"x-job-id": JOB, **({"x-event-type": marker} if marker else {})}
    dl = d.parse(body(body_type), hs, MID)
    assert (dl.kind, dl.problem) == ("inconsistent", "origin_conflict")


def test_worker_marker_on_a_broker_dead_letter_is_inconsistent():
    hs = headers([death("delivery_limit", "jobs.work.v2")], event_type="job.dead_lettered")
    assert d.parse(body(), hs, MID).problem == "origin_conflict"


def test_message_without_any_death_or_marker_is_inconsistent():
    dl = d.parse(body(), {"x-job-id": JOB, "x-event-type": "job.submitted"}, MID)
    assert (dl.kind, dl.problem) == ("inconsistent", "no_death_metadata")


# ---------------------------------------------------------------- P4-D9 job id resolution
def test_malformed_body_uses_valid_header_id():
    dl = d.parse(b"this is not json", headers([death("rejected", "jobs.work.v2")]), MID)
    assert (dl.kind, dl.job_id, dl.death_reason) == ("broker", uuid.UUID(JOB), "rejected")


def test_body_and_header_ids_disagree():
    other = str(uuid.uuid4())
    dl = d.parse(body(job_id=other), headers([death("delivery_limit", "jobs.work.v2")]), MID)
    assert (dl.kind, dl.job_id, dl.problem) == ("inconsistent", None, "job_id_mismatch")
    assert d.outcome_without_job(dl) == d.INCONSISTENT


@pytest.mark.parametrize("body_bytes,header_id", [(b"garbage", "unknown"), (b"garbage", None),
                                                  (json.dumps({"job_id": "nope"}).encode(), "also-nope")])
def test_no_valid_job_id_is_unreadable(body_bytes, header_id):
    hs = headers([death("rejected", "jobs.work.v2")], job_id=header_id)
    if header_id is None:
        del hs["x-job-id"]
    dl = d.parse(body_bytes, hs, MID)
    assert (dl.kind, dl.job_id, dl.problem) == ("broker", None, "job_id_unreadable")
    assert d.outcome_without_job(dl) == d.UNREADABLE


def test_only_header_or_only_body_id_is_used():
    hs = headers([death("delivery_limit", "jobs.work.v2")])
    del hs["x-job-id"]
    assert d.parse(body(), hs, MID).job_id == uuid.UUID(JOB)


# ---------------------------------------------------------------- P4-D8 dedup key
def test_dedup_key_message_id_plus_reason():
    assert d.dedup_key(MID, b"x", "delivery_limit") == f"{MID}:delivery_limit"


def test_dedup_key_without_message_id_hashes_the_body():
    key = d.dedup_key(None, b"secret body", "rejected")
    assert key == f"sha256:{hashlib.sha256(b'secret body').hexdigest()}:rejected"
    assert "secret" not in key
    assert len(d.dedup_key("m" * 1000, b"", "delivery_limit")) <= 300


# ---------------------------------------------------------------- P4-D3 decision
def _broker_dl(message_id=MID):
    return d.parse(body(), headers([death("delivery_limit", "jobs.work.v2")]), message_id)


@pytest.mark.parametrize("job,expected", [
    (None, d.UNKNOWN_JOB),
    ({"status": "COMPLETED", "retry": None}, d.ALREADY_TERMINAL),
    ({"status": "DEAD_LETTERED", "retry": None}, d.ALREADY_TERMINAL),
    ({"status": "FAILED", "retry": None}, d.ALREADY_TERMINAL),
    ({"status": "TRANSCRIBING", "retry": None}, d.APPLIED),
    ({"status": "QUEUED", "retry": None}, d.APPLIED),
    ({"status": "SUMMARIZING", "retry": {"event_id": MID}}, d.APPLIED),
    ({"status": "SUMMARIZING", "retry": {"event_id": str(uuid.uuid4())}}, d.STALE),
])
def test_decide(job, expected):
    assert d.decide(job, _broker_dl()) == expected


def test_missing_message_id_is_never_treated_as_stale_or_applied():
    dl = _broker_dl(message_id=None)
    assert d.outcome_without_job(dl) == d.INCONSISTENT


# ---------------------------------------------------------------- P4-D6 error document
@pytest.mark.parametrize("reason", ["delivery_limit", "rejected"])
def test_job_error_shape(reason):
    err = d.job_error(reason)
    assert set(err) == {"code", "message", "reason"}
    assert err["code"] == "DEAD_LETTERED_BY_BROKER" and err["reason"] == reason and err["message"]


# ---------------------------------------------------------------- P4-D5 audit delivery limit
def test_audit_limit_defaults_above_the_carried_count():
    s = get_settings(queue_delivery_limit=10)
    assert s.effective_audit_queue_delivery_limit == 21
    assert get_settings(queue_delivery_limit=3).effective_audit_queue_delivery_limit == 14


@pytest.mark.parametrize("work,audit", [(10, 11), (10, 5), (3, 4)])
def test_audit_limit_must_exceed_work_limit_plus_one(work, audit):
    with pytest.raises(SystemExit, match="value_error"):
        get_settings(queue_delivery_limit=work, audit_queue_delivery_limit=audit)


def test_explicit_audit_limit_is_accepted():
    assert get_settings(queue_delivery_limit=10, audit_queue_delivery_limit=12).effective_audit_queue_delivery_limit == 12


# ---------------------------------------------------------------- P4-D4/D7 nack only
def test_reject_with_requeue_is_never_used():
    """RabbitMQ 4.4.1 counts reject(requeue=True) toward the delivery limit; nack(requeue=True) not."""
    root = pathlib.Path(__file__).resolve().parents[1] / "notesagent"
    offenders = [str(p) for p in root.rglob("*.py")
                 if re.search(r"\.reject\(\s*requeue\s*=\s*True", p.read_text(encoding="utf-8"))]
    assert offenders == []
