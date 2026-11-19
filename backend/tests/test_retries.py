"""Part 3: classified retries via the outbox, backoff, DEAD_LETTERED on exhaustion, crash windows.

Real PostgreSQL, RabbitMQ, outbox publisher and worker processes. Stage bodies are test-only
stand-ins that raise CLASSIFIED errors (tests/support/fixture_stages.py); crash/DB-failure hooks
are test-only (tests/support/run_worker_hooks.py). Test settings: BACKOFF_SCHEDULE_SEC=2,4,8,
LOCK_RETRY_DELAY_SEC=5, INFRA_RETRY_DELAY_SEC=30 (overridden per test where noted).
"""
import json
import time
from datetime import datetime, timezone

from tests.support.helpers import (
    drain_outbox,
    events,
    files,
    job,
    release,
    run_count,
    started_pids,
    submit_ok,
    wait_for_status,
    wait_until,
)
from tests.support.rabbit import peek_messages, queue_state

FIXTURE = "tests.support.run_fixture_worker"
HOOKS = "tests.support.run_worker_hooks"
TERMINAL = {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED"}


def run_times(fx, job_id, stage):
    return sorted(p.stat().st_mtime for p in files(fx, f"run.{job_id}.{stage}."))


def kinds(db, job_id):
    return [e["event_type"] for e in events(db, job_id)]


def scheduled(db, job_id):
    return [e["detail"] for e in events(db, job_id) if e["event_type"] == "retry_scheduled"]


def outbox_rows(db, job_id, event_type):
    return db.execute("SELECT * FROM outbox WHERE job_id = %s AND event_type = %s ORDER BY id",
                      (job_id, event_type)).fetchall()


# ===================================================================== budgets, backoff, classification
def test_backoff_schedule_last_step_repeats_then_dead_letter(api, db, procs, fx):
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    procs.start("worker-a", FIXTURE, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="transient",
                MAX_ATTEMPTS=5)
    row = wait_for_status(db, job_id, TERMINAL, timeout=90)
    assert row["status"] == "DEAD_LETTERED" and row["attempts"] == 5
    assert row["error"]["code"] == "RETRIES_EXHAUSTED" and row["error"]["last_error_code"] == "FIXTURE_TRANSIENT"
    assert [d["delay_sec"] for d in scheduled(db, job_id)] == [2, 4, 8, 8]    # last step repeats
    times = run_times(fx, job_id, "TRANSCRIBING")
    assert len(times) == 5                                                    # exactly MAX_ATTEMPTS executions
    gaps = [b - a for a, b in zip(times, times[1:])]
    for gap, delay in zip(gaps, [2, 4, 8, 8]):
        assert delay - 0.3 <= gap <= delay + 6, (gaps, "never earlier than the scheduled delay")
    print(f"\nEVIDENCE backoff gaps: {[round(g, 2) for g in gaps]} for delays [2, 4, 8, 8]")
    wait_until(lambda: queue_state("jobs.dlq").messages == 1, timeout=20, what="DLQ copy")
    assert len(outbox_rows(db, job_id, "job.retry")) == 4 and len(outbox_rows(db, job_id, "job.dead_lettered")) == 1


def test_fatal_error_fails_once_without_retry(api, db, procs, fx):
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    procs.start("worker-a", FIXTURE, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="fatal")
    row = wait_for_status(db, job_id, TERMINAL)
    assert row["status"] == "FAILED" and row["error"]["code"] == "YOUTUBE_VIDEO_UNAVAILABLE"
    assert row["attempts"] == 0 and row["retry"] is None and run_count(fx, job_id, "TRANSCRIBING") == 1
    assert outbox_rows(db, job_id, "job.retry") == [] and outbox_rows(db, job_id, "job.dead_lettered") == []
    time.sleep(1)
    assert queue_state("jobs.dlq").messages == 0


def test_rate_limited_retry_after_is_honoured_with_uncounted_hops(api, db, procs, fx):
    """retry_after=9 > every bucket <= 9 (2,4,5,8): first hop 8 s, then a re-hop; never earlier than 9 s."""
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    procs.start("worker-a", FIXTURE, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="rate_limited:9",
                FIXTURE_RAISE_TIMES=1)
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert row["status"] == "COMPLETED" and row["attempts"] == 1               # hops counted nothing
    [detail] = scheduled(db, job_id)
    assert detail["delay_sec"] == 9
    first, second = run_times(fx, job_id, "TRANSCRIBING")
    assert second - first >= 9 - 0.3
    log = procs.log("worker-a")
    assert "early retry delivery re-delayed" in log
    hop_ids = {json.loads(line)["message_id"] for line in log.splitlines()
               if line.startswith("{") and "early retry delivery re-delayed" in line}
    assert hop_ids == {detail["event_id"]}                                     # same event id across hops
    print(f"\nEVIDENCE Retry-After 9 s: executed after {second - first:.2f} s via hops")


def test_quota_exhausted_uses_normal_budget_and_keeps_reset_time(api, db, procs, fx):
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    procs.start("worker-a", FIXTURE, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="quota",
                MAX_ATTEMPTS=3)
    started = time.monotonic()
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert time.monotonic() - started < 40                                     # did not wait for the reset
    assert row["status"] == "DEAD_LETTERED" and row["error"]["last_error_code"] == "GEMINI_QUOTA_EXHAUSTED"
    resets_at = datetime.fromisoformat(row["error"]["resets_at"])
    assert resets_at > datetime.now(timezone.utc) and resets_at.tzinfo is not None
    body = api.get(f"/jobs/{job_id}/results").json()
    assert body["status"] == "DEAD_LETTERED" and body["error"]["resets_at"] == row["error"]["resets_at"]
    assert body["result"] is None


def test_rate_limiter_unavailable_uses_only_the_infra_budget(api, db, procs, fx):
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    procs.start("worker-a", FIXTURE, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="infra",
                INFRA_MAX_ATTEMPTS=3, INFRA_RETRY_DELAY_SEC=2, MAX_ATTEMPTS=1)
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert row["status"] == "DEAD_LETTERED", row        # MAX_ATTEMPTS=1 would have dead-lettered at once if counted
    assert row["attempts"] == 0 and row["infra_attempts"] == 3
    assert row["error"]["last_error_code"] == "RATE_LIMITER_UNAVAILABLE"
    assert run_count(fx, job_id, "TRANSCRIBING") == 3


def test_retry_state_is_visible_while_waiting_without_internals(api, db, procs, fx):
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    procs.start("worker-a", FIXTURE, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="rate_limited:25",
                FIXTURE_RAISE_TIMES=1)
    wait_until(lambda: (job(db, job_id) or {}).get("retry"), timeout=20, what="retry scheduled")
    body = api.get(f"/jobs/{job_id}").json()
    assert body["status"] == "TRANSCRIBING"                                     # waiting, not failed
    assert set(body["retry"]) == {"reason", "attempt", "max_attempts", "next_attempt_at"}
    assert body["retry"]["reason"] == "NOTION_RATE_LIMITED" and body["retry"]["attempt"] == 1
    assert "event_id" not in json.dumps(body) and "budget" not in json.dumps(body)


# ===================================================================== message identity (clarification C)
def test_retry_event_identity_end_to_end(api, db, procs, fx):
    job_id = submit_ok(api)
    assert drain_outbox() == 1
    submission_id = str(db.execute("SELECT event_id FROM outbox WHERE job_id = %s AND event_type = 'job.submitted'",
                                   (job_id,)).fetchone()["event_id"])
    procs.start("worker-a", FIXTURE, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="transient",
                FIXTURE_RAISE_TIMES=2)
    wait_until(lambda: (job(db, job_id) or {}).get("retry"), timeout=20, what="first retry scheduled")
    retry = job(db, job_id)["retry"]
    [row] = outbox_rows(db, job_id, "job.retry")
    assert str(row["event_id"]) == retry["event_id"] != submission_id         # new identity, not the original's
    assert row["destination_exchange"] == "jobs.retry" and row["destination_routing_key"] == "2s"
    assert drain_outbox() == 1
    [msg] = peek_messages("jobs.retry.2s")
    assert msg.message_id == retry["event_id"]                                 # AMQP message_id == event id
    wait_until(lambda: len(scheduled(db, job_id)) == 2, timeout=20, what="second retry")
    drain_outbox()
    first, second = (d["event_id"] for d in scheduled(db, job_id))
    assert first != second                                                      # each retry gets a new id
    wait_for_status(db, job_id, {"COMPLETED"}, timeout=30)


# ===================================================================== crash windows (real SIGKILL)
def test_crash_mid_stage_before_retry_tx_counts_nothing(api, db, procs, fx):
    for name in ("worker-a", "worker-b"):
        procs.start(name, FIXTURE, FIXTURE_DIR=fx, FIXTURE_GATE="TRANSCRIBING")
        procs.wait_for_log(name, "worker started")
    job_id = submit_ok(api)
    drain_outbox()
    [victim] = wait_until(lambda: started_pids(fx, job_id, "TRANSCRIBING"), what="stage started")
    procs.sigkill(victim)
    wait_until(lambda: [p for p in started_pids(fx, job_id, "TRANSCRIBING") if p != victim], timeout=30,
               what="redelivered")
    release(fx, job_id)
    row = wait_for_status(db, job_id, {"COMPLETED"})
    assert row["attempts"] == 0 and row["infra_attempts"] == 0 and "retry_scheduled" not in kinds(db, job_id)


def test_crash_after_retry_commit_before_ack(api, db, procs, fx):
    """Original redelivered after the retry was committed: it is stale (id != retry.event_id),
    dropped without counting; the scheduled retry then runs once."""
    procs.start("worker-crash", HOOKS, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="transient",
                FIXTURE_RAISE_TIMES=1, HOOK_CRASH_AFTER="schedule_retry")
    procs.wait_for_log("worker-crash", "worker started")
    job_id = submit_ok(api)
    drain_outbox()
    assert procs.procs["worker-crash"].wait(timeout=30) == -9
    assert files(fx, "hook.crash_after_schedule_retry.")
    assert job(db, job_id)["attempts"] == 1                                    # committed before the crash
    procs.start("publisher", "notesagent.outbox")
    procs.start("worker-b", FIXTURE, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="transient",
                FIXTURE_RAISE_TIMES=1)
    row = wait_for_status(db, job_id, {"COMPLETED"}, timeout=40)
    assert row["attempts"] == 1                                                # not counted twice
    assert run_count(fx, job_id, "TRANSCRIBING") == 2
    assert kinds(db, job_id).count("retry_scheduled") == 1
    procs.wait_for_log("worker-b", "stale message dropped")
    assert queue_state("jobs.dlq").messages == 0


def test_duplicate_retry_copies_after_outbox_publisher_crash(api, db, procs, fx):
    """Publisher killed after the broker confirmed the retry copy -> it is published twice.
    The advisory lock + gating keep it to ONE execution and ONE counted attempt."""
    for name in ("worker-a", "worker-b"):
        procs.start(name, FIXTURE, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="transient",
                    FIXTURE_RAISE_TIMES=1, FIXTURE_GATE="SUMMARIZING")
        procs.wait_for_log(name, "worker started")
    job_id = submit_ok(api)
    assert drain_outbox() == 1                                                 # submission
    wait_until(lambda: len(scheduled(db, job_id)) == 1, timeout=20, what="retry scheduled")
    crasher = procs.start("crashing-publisher", "tests.support.run_publisher_crash_after_confirm")
    assert crasher.wait(timeout=30) == -9                                      # retry copy #1 confirmed, row unmarked
    assert outbox_rows(db, job_id, "job.retry")[0]["published_at"] is None
    assert drain_outbox() == 1                                                 # retry copy #2 (same event id)
    wait_until(lambda: started_pids(fx, job_id, "SUMMARIZING"), timeout=30, what="retry ran")
    wait_until(lambda: "duplicate_deferred" in kinds(db, job_id) or "deferring" in procs.all_logs()
               or "stale message dropped" in procs.all_logs(), timeout=30, what="second copy handled")
    release(fx, job_id)
    row = wait_for_status(db, job_id, {"COMPLETED"}, timeout=40)
    assert row["attempts"] == 1
    assert run_count(fx, job_id, "TRANSCRIBING") == 2                          # 1 failure + 1 retry, never 3
    assert run_count(fx, job_id, "SUMMARIZING") == 1
    assert kinds(db, job_id).count("retry_started") == 1
    wait_until(lambda: queue_state("jobs.work.v2").messages == 0, timeout=20, what="copies drained")
    assert queue_state("jobs.dlq").messages == 0


def test_crash_during_the_retried_stage(api, db, procs, fx):
    """The retry message is redelivered after a crash and still authorised; the crash is not counted."""
    for name in ("worker-a", "worker-b"):
        procs.start(name, FIXTURE, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="transient",
                    FIXTURE_RAISE_TIMES=1, FIXTURE_GATE="TRANSCRIBING")
        procs.wait_for_log(name, "worker started")
    procs.start("publisher", "notesagent.outbox")
    job_id = submit_ok(api)
    [victim] = wait_until(lambda: started_pids(fx, job_id, "TRANSCRIBING"), timeout=30, what="retried stage running")
    retry_event = job(db, job_id)["retry"]["event_id"]
    procs.sigkill(victim)
    wait_until(lambda: [p for p in started_pids(fx, job_id, "TRANSCRIBING") if p != victim], timeout=30,
               what="retry redelivered to the other worker")
    assert job(db, job_id)["retry"]["event_id"] == retry_event                 # same authorisation
    release(fx, job_id)
    row = wait_for_status(db, job_id, {"COMPLETED"}, timeout=40)
    assert row["attempts"] == 1                                                # crash not counted
    assert run_count(fx, job_id, "TRANSCRIBING") == 3                          # fail, killed, success
    assert kinds(db, job_id).count("retry_started") == 2


def test_crash_after_dead_letter_commit_before_ack(api, db, procs, fx, diag):
    procs.start("worker-crash", HOOKS, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="transient",
                MAX_ATTEMPTS=1, HOOK_CRASH_AFTER="dead_letter")
    diag.mark("worker-crash spawned")
    procs.wait_for_log("worker-crash", "worker started")
    diag.mark("worker-crash ready")
    job_id = submit_ok(api)
    diag.track_job(job_id)
    drain_outbox()
    diag.mark("submission published")
    assert procs.procs["worker-crash"].wait(timeout=30) == -9
    diag.mark("worker-crash exited")
    assert job(db, job_id)["status"] == "DEAD_LETTERED"
    assert queue_state(f"cred.{job_id}").exists                                # cleanup never ran
    procs.start("publisher", "notesagent.outbox")
    procs.start("worker-b", FIXTURE, FIXTURE_DIR=fx)
    diag.mark("publisher + worker-b spawned")
    wait_until(lambda: not queue_state(f"cred.{job_id}").exists, timeout=20, what="mailbox cleaned up on redelivery")
    diag.mark("mailbox cleaned up")
    wait_until(lambda: queue_state("jobs.dlq").messages == 1, timeout=20, what="single DLQ copy")
    diag.mark("DLQ copy observed")
    assert run_count(fx, job_id, "TRANSCRIBING") == 1 and kinds(db, job_id).count("dead_lettered") == 1
    wait_until(lambda: queue_state("jobs.work.v2").messages == 0, timeout=10, what="acked")
    diag.mark("work queue drained (end)")


# ===================================================================== PostgreSQL / broker unavailable (P3-D4)
def test_db_failure_in_retry_transaction_counts_nothing(api, db, procs, fx):
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    procs.start("worker-a", HOOKS, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_KIND="transient",
                FIXTURE_RAISE_TIMES=3, HOOK_DB_FAIL_RETRY_TX=2)
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert row["status"] == "COMPLETED"
    assert row["attempts"] == 1                                                # only the successful retry tx counted
    assert len(files(fx, "hook.db_fail_retry_tx.")) == 2
    assert run_count(fx, job_id, "TRANSCRIBING") == 4
    assert procs.log("worker-a").count("message delayed without counting an attempt") == 2


def test_lock_unavailable_delays_without_counting(api, db, procs, fx, diag):
    job_id = submit_ok(api)
    diag.track_job(job_id)
    drain_outbox()
    diag.mark("submission published")
    procs.start("worker-a", HOOKS, FIXTURE_DIR=fx, HOOK_DB_FAIL_LOCK=1)
    diag.mark("worker-a spawned")
    row = wait_for_status(db, job_id, TERMINAL, timeout=30)
    diag.mark(f"terminal status observed: {row['status']}")
    assert row["status"] == "COMPLETED" and row["attempts"] == 0 and row["infra_attempts"] == 0
    assert files(fx, "hook.db_fail_lock.")
    assert "message delayed without counting an attempt" in procs.log("worker-a")


def test_delay_publish_failure_leaves_message_unacked_then_recovers(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()
    procs.start("worker-a", HOOKS, FIXTURE_DIR=fx, HOOK_DB_FAIL_LOCK=2, HOOK_DELAY_PUBLISH_FAIL=1)
    row = wait_for_status(db, job_id, TERMINAL, timeout=45)
    assert row["status"] == "COMPLETED" and row["attempts"] == 0
    log = procs.log("worker-a")
    assert "could not delay the message; leaving it unacknowledged" in log
    assert log.count("message delayed without counting an attempt") == 2
    assert run_count(fx, job_id, "TRANSCRIBING") == 1
