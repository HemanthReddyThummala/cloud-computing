"""ADR-12 integration tests: the dead-letter auditor against the real broker and database.

Real broker dead letters are produced by real consumer crashes (SIGKILL) and by a real worker
rejecting a malformed message. Other cases publish crafted dead letters into jobs.dlx with the
header layout RabbitMQ 4.4.1 produces (captured in the broker probe), so each rule can be hit exactly.
"""
import json
import os
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from notesagent.common.broker import QUEUE_DLQ_AUDIT, QUEUE_DLQ_PARKED
from notesagent.common.states import JobStatus as S
from notesagent.worker.runner import advisory_lock_key
from tests.support.helpers import (
    drain_outbox,
    events,
    force_transitions,
    job,
    submit_ok,
    wait_for_status,
    wait_until,
)
from tests.support.rabbit import (
    broker_death_headers,
    peek_messages,
    publish_dead_letter,
    publish_job_message,
    publish_raw,
    queue_state,
)

FIXTURE = "tests.support.run_fixture_worker"
HOOKS = "tests.support.run_worker_hooks"


# ---------------------------------------------------------------- helpers
def audit_rows(db, job_id=None):
    if job_id is None:
        return db.execute("SELECT * FROM dead_letter_audit ORDER BY id").fetchall()
    return db.execute("SELECT * FROM dead_letter_audit WHERE job_id = %s ORDER BY id", (job_id,)).fetchall()


def broker_events(db, job_id):
    return [e for e in events(db, job_id) if e["event_type"] == "dead_lettered_by_broker"]


def job_body(job_id, event_id=None, event_type="job.submitted"):
    return json.dumps({"event_id": event_id or str(uuid.uuid4()), "event_type": event_type,
                       "job_id": job_id, "schema": 1}).encode()


def start_worker(procs, fx, name="worker-a", module=FIXTURE, **env):
    procs.start(name, module, FIXTURE_DIR=fx, **env)
    procs.wait_for_log(name, "worker started")


def crash_loop(procs, fx, queue, *, until, max_kills, prefix="holder"):
    """Repeatedly let a consumer take the message from ``queue`` without acking, then SIGKILL it."""
    kills = 0
    for i in range(max_kills):
        if until():
            break
        marker = fx / f"{prefix}.{i}"
        holder = procs.start(f"{prefix}-{i}", "tests.support.hold_message", queue, marker)
        wait_until(marker.exists, timeout=15, what=f"{queue} message held ({i})")
        procs.sigkill(holder.pid)
        holder.wait(timeout=10)
        kills += 1
        wait_until(lambda: until() or queue_state(queue).messages == 1, timeout=15, what="message returned")
    assert until(), f"condition not reached after {kills} crashes"
    return kills


def wait_audit(db, job_id=None, n=1, timeout=30):
    return wait_until(lambda: (rows := audit_rows(db, job_id)) and len(rows) >= n and rows,
                      timeout=timeout, what="audit row")


# ===================================================================== real broker dead letters
def test_crash_loop_reaches_delivery_limit_and_job_is_dead_lettered(api, db, procs, fx):
    """No worker survives the job (11 real SIGKILLs, QUEUE_DELIVERY_LIMIT=10): RabbitMQ dead-letters the
    message; the auditor moves the job to DEAD_LETTERED. The DLQ copy stays for inspection."""
    job_id = submit_ok(api)
    drain_outbox()
    kills = crash_loop(procs, fx, "jobs.work.v2", until=lambda: queue_state("jobs.dlq").messages == 1,
                       max_kills=12)
    assert kills == 11                                       # delivered limit + 1 times
    assert job(db, job_id)["status"] == "QUEUED"              # nobody told PostgreSQL yet
    [dead] = peek_messages(QUEUE_DLQ_AUDIT)
    assert dead.headers["x-last-death-reason"] == "delivery_limit"

    start_worker(procs, fx)
    row = wait_for_status(db, job_id, {"DEAD_LETTERED"})
    assert row["error"] == {"code": "DEAD_LETTERED_BY_BROKER", "reason": "delivery_limit",
                            "message": row["error"]["message"]} and row["error"]["message"]
    [audit] = audit_rows(db, job_id)
    assert (audit["outcome"], audit["death_reason"], audit["source_queue"], audit["seen_count"]) == \
           ("applied", "delivery_limit", "jobs.work.v2", 1)
    assert len(broker_events(db, job_id)) == 1
    wait_until(lambda: queue_state(QUEUE_DLQ_AUDIT).messages == 0, what="audit message acked")
    assert queue_state("jobs.dlq").messages == 1                       # inspection copy kept
    wait_until(lambda: not queue_state(f"cred.{job_id}").exists, what="mailbox deleted")
    assert api.get(f"/jobs/{job_id}/results").json()["error"]["code"] == "DEAD_LETTERED_BY_BROKER"


def test_malformed_message_with_readable_job_id_is_dead_lettered(api, db, procs, fx):
    start_worker(procs, fx)
    job_id = submit_ok(api)                                   # outbox not drained: only the bad message runs
    publish_raw(b"this is not json", job_id=job_id)           # the worker rejects it -> broker dead-letters it
    row = wait_for_status(db, job_id, {"DEAD_LETTERED"})
    assert row["error"]["code"] == "DEAD_LETTERED_BY_BROKER" and row["error"]["reason"] == "rejected"
    [audit] = audit_rows(db, job_id)
    assert (audit["outcome"], audit["death_reason"]) == ("applied", "rejected")


def test_malformed_message_without_job_id_is_recorded_unreadable(db, procs, fx):
    start_worker(procs, fx)
    publish_raw(b"garbage")                                    # x-job-id "unknown"
    [audit] = wait_audit(db)
    assert (audit["outcome"], audit["job_id"], audit["death_reason"]) == ("unreadable", None, "rejected")
    assert queue_state("jobs.dlq").messages == 1


def test_worker_initiated_copy_is_ignored(api, db, procs, fx):
    """Part 3 exhaustion publishes a DLQ copy (x-event-type=job.dead_lettered) that also reaches the
    audit queue: it is acknowledged without touching the job or the audit table."""
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    start_worker(procs, fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="transient", MAX_ATTEMPTS=1)
    row = wait_for_status(db, job_id, {"DEAD_LETTERED"})
    assert row["error"]["code"] == "RETRIES_EXHAUSTED"
    procs.wait_for_log("worker-a", "worker-initiated dead-letter copy")
    [copy] = peek_messages("jobs.dlq")
    assert copy.headers["x-event-type"] == "job.dead_lettered" and "x-death" not in copy.headers
    wait_until(lambda: queue_state(QUEUE_DLQ_AUDIT).messages == 0, what="copy acked")
    assert audit_rows(db) == [] and job(db, job_id)["error"]["code"] == "RETRIES_EXHAUSTED"
    assert broker_events(db, job_id) == []


def test_outbox_publisher_marks_every_message_with_its_event_type(api):
    job_id = submit_ok(api)
    drain_outbox()
    [msg] = peek_messages("jobs.work.v2")
    assert msg.headers["x-event-type"] == "job.submitted" and msg.headers["x-job-id"] == job_id


# ===================================================================== crafted dead letters (exact rules)
def test_unknown_job_is_recorded(db, procs, fx):
    start_worker(procs, fx)
    ghost = str(uuid.uuid4())
    publish_dead_letter(job_body(ghost), str(uuid.uuid4()), broker_death_headers(ghost))
    [audit] = wait_audit(db)
    assert (audit["outcome"], str(audit["job_id"])) == ("unknown_job", ghost)


def test_completed_job_is_never_overwritten(api, db, procs, fx):
    job_id = submit_ok(api)
    result = {"notion_page_url": "https://www.notion.so/p", "frames_requested": 1, "frames_published": 1}
    force_transitions(job_id, [S.TRANSCRIBING, S.SUMMARIZING, S.EXTRACTING_FRAMES, S.PUBLISHING, S.COMPLETED],
                      result=result)
    start_worker(procs, fx)
    publish_dead_letter(job_body(job_id), str(uuid.uuid4()), broker_death_headers(job_id))
    [audit] = wait_audit(db, job_id)
    assert audit["outcome"] == "already_terminal"
    row = job(db, job_id)
    assert row["status"] == "COMPLETED" and row["result"] == result and row["error"] is None
    assert broker_events(db, job_id) == []


def test_stale_dead_letter_keeps_the_newer_scheduled_retry(api, db, procs, fx):
    job_id = submit_ok(api)
    force_transitions(job_id, [S.TRANSCRIBING])
    current = str(uuid.uuid4())
    retry = {"event_id": current, "attempt": 1, "max_attempts": 5, "budget": "provider",
             "reason_code": "X", "delay_sec": 30, "next_attempt_at": "2099-01-01T00:00:00+00:00"}
    db.execute("UPDATE jobs SET retry = %s WHERE id = %s", (Jsonb(retry), job_id))
    start_worker(procs, fx)
    old = str(uuid.uuid4())
    publish_dead_letter(job_body(job_id, old), old, broker_death_headers(job_id))
    [audit] = wait_audit(db, job_id)
    assert audit["outcome"] == "stale"
    row = job(db, job_id)
    assert row["status"] == "TRANSCRIBING" and row["retry"]["event_id"] == current and row["error"] is None

    # the CURRENT retry message dead-lettered by the broker is applied
    publish_dead_letter(job_body(job_id, current, "job.retry"), current, broker_death_headers(job_id))
    row = wait_for_status(db, job_id, {"DEAD_LETTERED"})
    assert row["retry"] is None and row["error"]["code"] == "DEAD_LETTERED_BY_BROKER"
    assert [r["outcome"] for r in audit_rows(db, job_id)] == ["stale", "applied"]


def test_duplicate_dead_letter_is_recorded_once_with_one_event(api, db, procs, fx):
    job_id = submit_ok(api)
    start_worker(procs, fx)
    mid = str(uuid.uuid4())
    for _ in range(2):
        publish_dead_letter(job_body(job_id, mid), mid, broker_death_headers(job_id))
    wait_until(lambda: (rows := audit_rows(db, job_id)) and rows[0]["seen_count"] == 2, what="second sighting")
    [audit] = audit_rows(db, job_id)
    assert audit["outcome"] == "applied" and audit["detail"]["last_outcome"] == "already_terminal"
    assert audit["dedup_key"] == f"{mid}:delivery_limit"
    assert len(broker_events(db, job_id)) == 1 and job(db, job_id)["status"] == "DEAD_LETTERED"


def test_body_and_header_job_ids_disagree(api, db, procs, fx):
    job_a, job_b = submit_ok(api), submit_ok(api)
    start_worker(procs, fx)
    publish_dead_letter(job_body(job_a), str(uuid.uuid4()), broker_death_headers(job_b))
    [audit] = wait_audit(db)
    assert (audit["outcome"], audit["job_id"], audit["detail"]["problem"]) == \
           ("inconsistent", None, "job_id_mismatch")
    assert job(db, job_a)["status"] == job(db, job_b)["status"] == "QUEUED"


def test_missing_message_id_never_changes_the_job(api, db, procs, fx):
    job_id = submit_ok(api)
    start_worker(procs, fx)
    publish_dead_letter(job_body(job_id), None, broker_death_headers(job_id))
    [audit] = wait_audit(db)
    assert audit["message_id"] is None
    assert audit["outcome"] == "inconsistent" and audit["dedup_key"].startswith("sha256:")
    assert job(db, job_id)["status"] == "QUEUED"


def test_inconsistent_death_metadata_never_changes_the_job(api, db, procs, fx):
    job_id = submit_ok(api)
    start_worker(procs, fx)
    headers = broker_death_headers(job_id)
    headers["x-last-death-queue"] = "jobs.retry.30s"          # disagrees with x-death[0]
    publish_dead_letter(job_body(job_id), str(uuid.uuid4()), headers)
    [audit] = wait_audit(db)
    assert (audit["outcome"], audit["death_reason"], audit["detail"]["problem"]) == \
           ("inconsistent", "unknown", "death_headers_disagree")
    assert job(db, job_id)["status"] == "QUEUED"


# ===================================================================== temporary failures and crashes
def test_busy_lock_defers_with_nack_and_never_parks(api, db, procs, fx):
    job_id = submit_ok(api)
    key = advisory_lock_key(uuid.UUID(job_id))
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (key,))          # a "live worker" owns the job
        start_worker(procs, fx)
        publish_dead_letter(job_body(job_id), str(uuid.uuid4()), broker_death_headers(job_id))
        wait_until(lambda: procs.log("worker-a").count("dead letter deferred") >= 2, timeout=30,
                   what="repeated deferrals")
        assert job(db, job_id)["status"] == "QUEUED" and audit_rows(db) == []
        holder.execute("SELECT pg_advisory_unlock(%s)", (key,))
    wait_for_status(db, job_id, {"DEAD_LETTERED"})
    assert queue_state(QUEUE_DLQ_PARKED).messages == 0
    assert len(broker_events(db, job_id)) == 1


def test_database_outage_requeues_without_loss(api, db, procs, fx):
    job_id = submit_ok(api)
    start_worker(procs, fx, module=HOOKS, HOOK_AUDIT_DB_FAIL=2)
    publish_dead_letter(job_body(job_id), str(uuid.uuid4()), broker_death_headers(job_id))
    wait_for_status(db, job_id, {"DEAD_LETTERED"}, timeout=40)
    assert len(list(fx.glob("hook.audit_db_fail.*"))) == 2
    assert procs.log("worker-a").count("dead letter could not be audited now; requeued") == 2
    assert queue_state(QUEUE_DLQ_PARKED).messages == 0
    [audit] = audit_rows(db, job_id)
    assert audit["seen_count"] == 1 and len(broker_events(db, job_id)) == 1


@pytest.mark.parametrize("when", ["before", "after"])
def test_auditor_crash_before_or_after_commit(api, db, procs, fx, when):
    """SIGKILL the auditor before its transaction / after the commit but before the ack: the message is
    redelivered to another worker; exactly one event either way."""
    job_id = submit_ok(api)
    procs.start("worker-a", HOOKS, FIXTURE_DIR=fx, HOOK_AUDIT_CRASH=when)
    procs.wait_for_log("worker-a", "worker started")
    publish_dead_letter(job_body(job_id), str(uuid.uuid4()), broker_death_headers(job_id))
    wait_until(lambda: procs.procs["worker-a"].poll() is not None, timeout=30, what="auditor crashed")
    assert len(list(fx.glob(f"hook.audit_crash_{when}.*"))) == 1
    assert job(db, job_id)["status"] == ("QUEUED" if when == "before" else "DEAD_LETTERED")

    start_worker(procs, fx, name="worker-b")
    wait_until(lambda: (rows := audit_rows(db, job_id)) and rows[0]["seen_count"] == (1 if when == "before" else 2),
               timeout=90, what="redelivered dead letter audited")
    [audit] = audit_rows(db, job_id)
    assert audit["outcome"] == "applied"
    if when == "after":
        assert audit["detail"]["last_outcome"] == "already_terminal"
    assert job(db, job_id)["status"] == "DEAD_LETTERED" and len(broker_events(db, job_id)) == 1
    wait_until(lambda: queue_state(QUEUE_DLQ_AUDIT).messages == 0, what="acked")
    wait_until(lambda: not queue_state(f"cred.{job_id}").exists, what="mailbox deleted")


def test_audit_crash_loop_parks_the_message_instead_of_losing_it(db, procs, fx):
    """Small limits (work 2, audit 4) on a broker declared only by this test's processes. A real crash
    loop dead-letters the job message (it arrives with x-delivery-count 3, carried over); a second
    crash loop on the audit queue exhausts its limit: the message is PARKED, not lost."""
    limits = {"QUEUE_DELIVERY_LIMIT": 2, "AUDIT_QUEUE_DELIVERY_LIMIT": 4}
    declarer = procs.start("declarer", FIXTURE, FIXTURE_DIR=fx, **limits)
    procs.wait_for_log("declarer", "worker started")
    declarer.terminate()
    declarer.wait(timeout=15)                               # nobody consumes any queue from now on

    job_id = str(uuid.uuid4())
    db.execute("INSERT INTO jobs (id, video_id, youtube_url) VALUES (%s, 'dQw4w9WgXcQ', 'u')", (job_id,))
    event_id = str(uuid.uuid4())
    publish_job_message(job_id, event_id)

    crash_loop(procs, fx, "jobs.work.v2", until=lambda: queue_state(QUEUE_DLQ_AUDIT).messages == 1, max_kills=5,
               prefix="work")
    [arrived] = peek_messages(QUEUE_DLQ_AUDIT)
    assert arrived.headers["x-delivery-count"] == 3                                   # carried over
    kills = crash_loop(procs, fx, QUEUE_DLQ_AUDIT, until=lambda: queue_state(QUEUE_DLQ_PARKED).messages == 1,
                       max_kills=6, prefix="audit")
    assert kills == 2                                            # 4 - 3 + 1: carried count respected
    assert queue_state(QUEUE_DLQ_AUDIT).messages == 0 and queue_state("jobs.dlq").messages == 1
    [parked] = peek_messages(QUEUE_DLQ_PARKED)
    assert parked.message_id == event_id and parked.headers["x-job-id"] == job_id
    deaths = parked.headers["x-death"]
    assert [(x["reason"], x["queue"]) for x in deaths[:2]] == [("delivery_limit", QUEUE_DLQ_AUDIT),
                                                              ("delivery_limit", "jobs.work.v2")]
    assert parked.headers["x-last-death-queue"] == QUEUE_DLQ_AUDIT
    assert peek_messages(QUEUE_DLQ_PARKED)[0].message_id == event_id                # inspection keeps it
