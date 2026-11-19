"""Failure window of the interim retry path: replacement confirmed, crash before acking the original.

Production JobWorker everywhere; only the stage bodies and the kill point are test-only.
"""
from tests.support.helpers import (
    drain_outbox,
    events,
    release,
    run_count,
    started_pids,
    submit_ok,
    wait_for_status,
    wait_until,
)
from tests.support.rabbit import peek_messages, queue_state


def test_crash_after_retry_republish_leaves_two_copies_but_job_runs_once(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()

    # 1. Worker A: TRANSCRIBING fails once -> retry path -> replacement confirmed -> SIGKILL before ack.
    crasher = procs.start("worker-crash", "tests.support.run_worker_crash_after_retry_publish",
                          FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_TIMES=1)
    assert crasher.wait(timeout=60) == -9
    assert (fx / "killed-after-retry-publish").exists()

    # 2. Exactly the feared state: the original (returned by the broker) AND the replacement.
    wait_until(lambda: queue_state("jobs.work").messages == 2, timeout=15, what="two copies in jobs.work")
    copies = peek_messages("jobs.work")
    assert len({m.message_id for m in copies}) == 1                         # same logical event
    assert sorted(int(m.headers.get("x-processing-attempts", 0)) for m in copies) == [0, 1]
    assert job_status(db, job_id) == "TRANSCRIBING"                          # nothing advanced

    # 3. Two healthy workers race for both copies; SUMMARIZING is gated to hold the job lock
    #    while the other copy is delivered.
    for name in ("worker-a", "worker-b"):
        procs.start(name, "tests.support.run_fixture_worker", FIXTURE_DIR=fx,
                    FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_TIMES=1, FIXTURE_GATE="SUMMARIZING")
    wait_until(lambda: started_pids(fx, job_id, "SUMMARIZING"), timeout=30, what="one copy is executing")
    wait_until(lambda: any(e["event_type"] == "duplicate_deferred" for e in events(db, job_id)),
               timeout=30, what="the other copy deferred by the advisory lock")
    assert len(started_pids(fx, job_id, "SUMMARIZING")) == 1                # never concurrent

    release(fx, job_id)
    wait_for_status(db, job_id, {"COMPLETED"})

    # 4. Effects happened once per stage (TRANSCRIBING: 1 failed attempt + 1 success).
    assert run_count(fx, job_id, "TRANSCRIBING") == 2
    for stage in ("SUMMARIZING", "EXTRACTING_FRAMES", "PUBLISHING"):
        assert run_count(fx, job_id, stage) == 1, stage
    kinds = [e["event_type"] for e in events(db, job_id)]
    assert kinds.count("claimed") == 1
    assert [e["to_status"] for e in events(db, job_id) if e["event_type"] == "transition"] == [
        "SUMMARIZING", "EXTRACTING_FRAMES", "PUBLISHING", "COMPLETED"]       # each CAS transition once

    # 5. The leftover copy is acknowledged as a duplicate of a finished job; nothing reaches the DLQ.
    wait_until(lambda: queue_state("jobs.work").messages == 0, timeout=20, what="duplicate drained")
    wait_until(lambda: "duplicate message for finished job dropped" in procs.all_logs(), timeout=20,
               what="duplicate acknowledged")
    assert queue_state("jobs.dlq").messages == 0


def job_status(db, job_id):
    return db.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()["status"]
