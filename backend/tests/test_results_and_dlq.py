"""Review follow-up: publish-result validation, delivery-limit/DLQ behaviour, and the
known Phase 2 gap (DEAD_LETTERED state handling).

These prove ORCHESTRATION/INFRASTRUCTURE behaviour with the production JobWorker and
test-only stage stand-ins. They do not prove any real pipeline (YouTube/LLM/Notion) work.
"""
import json

import pytest

from tests.support.helpers import (
    drain_outbox,
    events,
    run_count,
    submit_ok,
    wait_for_status,
    wait_until,
)
from tests.support.rabbit import peek_messages, publish_job_message, queue_state


# ---------------------------------------------------------------- results integrity
def test_invalid_publish_result_fails_instead_of_completing(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx, FIXTURE_RESULT="invalid")
    row = wait_for_status(db, job_id, {"FAILED", "COMPLETED", "COMPLETED_WITH_WARNINGS"})
    assert row["status"] == "FAILED" and row["error"]["code"] == "PUBLISH_RESULT_INVALID"
    assert row["result"] is None
    body = api.get(f"/jobs/{job_id}/results").json()
    assert body == {"status": "FAILED", "result": None, "warnings": [], "error": row["error"]}
    assert "notion" not in json.dumps(body["result"]).lower()


def test_partial_frames_complete_with_warnings(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx, FIXTURE_RESULT="partial")
    row = wait_for_status(db, job_id, {"FAILED", "COMPLETED", "COMPLETED_WITH_WARNINGS"})
    assert row["status"] == "COMPLETED_WITH_WARNINGS"
    assert row["result"]["frames_published"] == 2 and row["result"]["frames_requested"] == 3


# ---------------------------------------------------------------- delivery limit + DLQ
def test_nack_requeue_does_not_increase_delivery_count(api, db, procs, fx):
    """Regression evidence for the bug found in review: with RabbitMQ 4 quorum queues an explicit
    nack(requeue=True) leaves x-delivery-count unchanged, so x-delivery-limit can never fire."""
    import asyncio, os  # noqa: E401
    import aio_pika

    job_id = submit_ok(api)
    drain_outbox()

    async def _nack_three_times():
        conn = await aio_pika.connect(os.environ["TEST_RABBITMQ_URL"])
        try:
            ch = await conn.channel()
            q = await ch.declare_queue("jobs.work.v2", passive=True)
            counts = []
            for _ in range(3):
                m = await q.get(no_ack=False, timeout=5)
                counts.append(int((m.headers or {}).get("x-delivery-count", 0)))
                await m.nack(requeue=True)
            return counts
        finally:
            await conn.close()

    counts = asyncio.run(_nack_three_times())
    print(f"\nEVIDENCE x-delivery-count across 3 explicit nack(requeue=True): {counts}")
    assert counts == [0, 0, 0]


def test_unexpected_errors_are_bounded_and_dead_lettered(api, db, procs, fx):
    """REWRITTEN for Part 3 (P3-D5): an always-failing stage (unknown exception -> transient, D1)
    runs exactly MAX_ATTEMPTS times, then the job is DEAD_LETTERED with a DLQ copy via the outbox."""
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING",
                MAX_ATTEMPTS=3)
    row = wait_for_status(db, job_id, {"DEAD_LETTERED", "FAILED", "COMPLETED"}, timeout=60)
    assert row["status"] == "DEAD_LETTERED", row
    assert row["error"] == {"code": "RETRIES_EXHAUSTED", "last_error_code": "UNEXPECTED_ERROR", "attempts": 3,
                            "message": row["error"]["message"]}
    assert row["attempts"] == 3 and row["infra_attempts"] == 0 and row["retry"] is None
    assert run_count(fx, job_id, "TRANSCRIBING") == 3                      # exactly MAX_ATTEMPTS executions
    kinds = [e["event_type"] for e in events(db, job_id)]
    assert kinds.count("retry_scheduled") == 2 and kinds.count("dead_lettered") == 1

    wait_until(lambda: queue_state("jobs.dlq").messages == 1, timeout=20, what="DLQ copy via the outbox")
    [dead] = peek_messages("jobs.dlq")
    body = json.loads(dead.body)
    assert body["job_id"] == job_id and body["event_type"] == "job.dead_lettered"
    assert body["last_error_code"] == "UNEXPECTED_ERROR"
    wait_until(lambda: not queue_state(f"cred.{job_id}").exists, timeout=10, what="mailbox deleted")
    assert queue_state("jobs.work.v2").messages == 0


def test_dead_lettered_job_state_after_exhausted_retries(api, db, procs, fx):
    """Former strict-xfail Phase 2 requirement, now satisfied for worker-side exhaustion (P3-D5):
    the job becomes DEAD_LETTERED, its message copy reaches jobs.dlq and the mailbox is removed.
    (Broker-initiated dead letters — crash loops — are Part 4.)"""
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING",
                MAX_ATTEMPTS=2)
    wait_until(lambda: queue_state("jobs.dlq").messages == 1, timeout=60, what="message dead-lettered")
    row = wait_for_status(db, job_id, {"DEAD_LETTERED"}, timeout=10)
    assert row["error"]["code"] == "RETRIES_EXHAUSTED"
    wait_until(lambda: not queue_state(f"cred.{job_id}").exists, timeout=10, what="mailbox deleted")


def test_sigkill_redelivery_counts_toward_delivery_limit(api, db, procs, fx):
    """Measures whether a consumer crash increments the quorum queue's delivery count."""
    job_id = submit_ok(api)
    drain_outbox()
    marker = fx / "held"
    holder = procs.start("holder", "tests.support.hold_message", "jobs.work.v2", marker)
    wait_until(marker.exists, timeout=15, what="job message held")
    procs.sigkill(holder.pid)
    wait_until(lambda: queue_state("jobs.work.v2").messages == 1, timeout=10, what="message returned")
    [msg] = peek_messages("jobs.work.v2")
    print(f"\nEVIDENCE x-delivery-count after one consumer crash: {msg.headers.get('x-delivery-count')}")
    assert msg.headers.get("x-delivery-count", 0) >= 1


def test_duplicate_deferral_does_not_consume_attempts(api, db, procs, fx):
    """REWRITTEN for Part 3 (P3-D5): lock-busy duplicates go through the lock delay queue
    (jobs.retry.<LOCK_RETRY_DELAY_SEC>s); repeated deferrals count no attempt and reach no DLQ."""
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx, FIXTURE_GATE="TRANSCRIBING")
    procs.start("worker-b", "tests.support.run_fixture_worker", FIXTURE_DIR=fx, FIXTURE_GATE="TRANSCRIBING")
    for n in ("worker-a", "worker-b"):
        procs.wait_for_log(n, "worker started")
    job_id = submit_ok(api)
    drain_outbox()
    publish_job_message(job_id)
    wait_until(lambda: len(list(fx.glob(f"started.{job_id}.*"))) == 1, what="stage gated")
    wait_until(lambda: procs.all_logs().count("deferring duplicate message") >= 3, timeout=40,
               what="repeated deferrals through the delay queue")
    row = db.execute("SELECT attempts, infra_attempts, retry FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row == {"attempts": 0, "infra_attempts": 0, "retry": None}
    assert queue_state("jobs.dlq").messages == 0
    (fx / f"release.{job_id}").write_text("")
    wait_for_status(db, job_id, {"COMPLETED"})
