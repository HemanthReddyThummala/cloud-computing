"""Worker orchestration with real RabbitMQ redelivery and SIGKILL."""
import time

from tests.support.helpers import (
    drain_outbox,
    events,
    force_transitions,
    job,
    run_count,
    started_pids,
    release,
    submit_ok,
    wait_for_status,
    wait_until,
)
from tests.support.rabbit import publish_raw, queue_state
from notesagent.common.states import JobStatus as S


def test_production_worker_never_fakes_completion(api, db, procs, fx):
    """The PRODUCTION stage registry (all four stages, ADR-14..17) must never fake completion: without
    a Notion destination configured it FAILS honestly at PUBLISHING (NOTION_PARENT_MISSING, before any
    Notion request). YouTube and Gemini are replaced by fakes (no network)."""
    job_id = submit_ok(api)
    drain_outbox()
    procs.start("prod-worker", "tests.support.run_youtube_worker", FIXTURE_DIR=fx)
    row = wait_for_status(db, job_id, {"FAILED", "COMPLETED", "COMPLETED_WITH_WARNINGS"})
    assert row["status"] == "FAILED" and row["result"] is None
    assert row["error"]["code"] == "NOTION_PARENT_MISSING"
    assert [(e["event_type"], e["from_status"], e["to_status"]) for e in events(db, job_id)] == [
        ("submitted", None, "QUEUED"), ("claimed", "QUEUED", "TRANSCRIBING"),
        ("transition", "TRANSCRIBING", "SUMMARIZING"), ("transition", "SUMMARIZING", "EXTRACTING_FRAMES"),
        ("transition", "EXTRACTING_FRAMES", "PUBLISHING"), ("transition", "PUBLISHING", "FAILED")]
    wait_until(lambda: not queue_state(f"cred.{job_id}").exists, timeout=10, what="mailbox deleted")
    wait_until(lambda: queue_state("jobs.work.v2").messages == 0, timeout=10, what="message acked")


def test_sigkill_mid_stage_job_is_recovered_by_another_worker(api, db, procs, fx):
    for name in ("worker-a", "worker-b"):
        procs.start(name, "tests.support.run_fixture_worker", FIXTURE_DIR=fx, FIXTURE_GATE="EXTRACTING_FRAMES")
    for name in ("worker-a", "worker-b"):
        procs.wait_for_log(name, "worker started")

    job_id = submit_ok(api)
    drain_outbox()
    [victim] = wait_until(lambda: started_pids(fx, job_id, "EXTRACTING_FRAMES"), what="stage started")
    victim_name = procs.name_of(victim)
    assert job(db, job_id)["status"] == "EXTRACTING_FRAMES"

    killed_at = time.monotonic()
    procs.sigkill(victim)                                   # crash in the middle of a stage
    survivor = wait_until(lambda: [p for p in started_pids(fx, job_id, "EXTRACTING_FRAMES") if p != victim],
                          timeout=30, what="redelivery to the other worker")[0]
    recovery_sec = time.monotonic() - killed_at
    survivor_name = procs.name_of(survivor)
    assert survivor_name != victim_name

    release(fx, job_id)
    wait_for_status(db, job_id, {"COMPLETED"})

    # Completed stages were checkpointed and NOT repeated; only the interrupted stage re-ran.
    assert run_count(fx, job_id, "TRANSCRIBING") == 1
    assert run_count(fx, job_id, "SUMMARIZING") == 1
    assert run_count(fx, job_id, "EXTRACTING_FRAMES") == 2
    assert run_count(fx, job_id, "PUBLISHING") == 1
    assert db.execute("SELECT count(*) AS n FROM job_checkpoints WHERE job_id = %s", (job_id,)).fetchone()["n"] == 4

    evs = events(db, job_id)
    claimed = [e for e in evs if e["event_type"] == "claimed"]
    resumed = [e for e in evs if e["event_type"] == "resumed"]
    assert len(claimed) == 1 and claimed[0]["worker_id"] == victim_name
    assert len(resumed) == 1 and resumed[0]["worker_id"] == survivor_name
    assert resumed[0]["from_status"] == "EXTRACTING_FRAMES" and resumed[0]["detail"]["redelivered"] is True

    wait_until(lambda: queue_state("jobs.work.v2").messages == 0, timeout=10, what="queue drained")
    wait_until(lambda: not queue_state(f"cred.{job_id}").exists, timeout=10, what="mailbox deleted")
    print(f"\nEVIDENCE recovery: {victim_name} SIGKILLed -> {survivor_name} resumed in {recovery_sec:.2f}s")


def test_redelivered_message_for_terminal_job_only_cleans_up(api, db, procs):
    """Simulates a crash after the terminal commit but before mailbox cleanup + ack."""
    job_id = submit_ok(api)
    force_transitions(job_id, [S.FAILED], error={"code": "X", "message": "x"})
    assert queue_state(f"cred.{job_id}").exists
    drain_outbox()
    procs.start("prod-worker", "notesagent.worker")
    wait_until(lambda: not queue_state(f"cred.{job_id}").exists, timeout=15, what="mailbox cleaned up")
    wait_until(lambda: queue_state("jobs.work.v2").messages == 0, timeout=10, what="message acked")
    assert job(db, job_id)["status"] == "FAILED"
    assert [e["event_type"] for e in events(db, job_id)].count("claimed") == 0


def test_malformed_message_is_dead_lettered(procs):
    procs.start("prod-worker", "notesagent.worker")
    procs.wait_for_log("prod-worker", "worker started")
    publish_raw(b"this is not json")
    wait_until(lambda: queue_state("jobs.dlq").messages == 1, timeout=15, what="message in DLQ")
    assert queue_state("jobs.work.v2").messages == 0
