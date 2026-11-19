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
            q = await ch.declare_queue("jobs.work", passive=True)
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
    """An always-failing stage: the worker's own attempt counter bounds retries, then the DLQ."""
    job_id = submit_ok(api)
    drain_outbox()
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING")
    wait_until(lambda: queue_state("jobs.dlq").messages == 1, timeout=60, what="message dead-lettered")
    assert queue_state("jobs.work").messages == 0

    attempts = run_count(fx, job_id, "TRANSCRIBING")
    [dead] = peek_messages("jobs.dlq")
    assert json.loads(dead.body)["job_id"] == job_id
    print(f"\nEVIDENCE bounded retries: stage attempts={attempts}; DLQ headers="
          f"{ {k: v for k, v in dead.headers.items() if k.startswith(('x-first-death', 'x-processing'))} }")
    assert attempts == 10                                       # QUEUE_DELIVERY_LIMIT=10 attempts in total
    assert dead.headers.get("x-first-death-reason") == "rejected"
    assert dead.headers.get("x-processing-attempts") == 9       # 9 republished retries + final attempt

    # KNOWN GAP (Phase 2): PostgreSQL is NOT told about the dead-lettering.
    row = db.execute("SELECT status, error FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row["status"] == "TRANSCRIBING" and row["error"] is None
    assert [e["event_type"] for e in events(db, job_id)].count("resumed") == attempts - 1


@pytest.mark.xfail(strict=True, reason="PHASE 2 REQUIREMENT: a job whose message reaches the DLQ must "
                                       "become DEAD_LETTERED (and its credential mailbox removed).")
def test_phase2_requirement_dead_lettered_job_state(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING")
    wait_until(lambda: queue_state("jobs.dlq").messages == 1, timeout=60, what="message dead-lettered")
    wait_for_status(db, job_id, {"DEAD_LETTERED"}, timeout=10)


def test_sigkill_redelivery_counts_toward_delivery_limit(api, db, procs, fx):
    """Measures whether a consumer crash increments the quorum queue's delivery count."""
    job_id = submit_ok(api)
    drain_outbox()
    marker = fx / "held"
    holder = procs.start("holder", "tests.support.hold_message", "jobs.work", marker)
    wait_until(marker.exists, timeout=15, what="job message held")
    procs.sigkill(holder.pid)
    wait_until(lambda: queue_state("jobs.work").messages == 1, timeout=10, what="message returned")
    [msg] = peek_messages("jobs.work")
    print(f"\nEVIDENCE x-delivery-count after one consumer crash: {msg.headers.get('x-delivery-count')}")
    assert msg.headers.get("x-delivery-count", 0) >= 1


def test_duplicate_deferral_does_not_consume_delivery_attempts(api, db, procs, fx):
    """Lock-busy duplicates are republished (new message), so they never exhaust the delivery limit."""
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx, FIXTURE_GATE="TRANSCRIBING")
    procs.start("worker-b", "tests.support.run_fixture_worker", FIXTURE_DIR=fx, FIXTURE_GATE="TRANSCRIBING")
    for n in ("worker-a", "worker-b"):
        procs.wait_for_log(n, "worker started")
    job_id = submit_ok(api)
    drain_outbox()
    publish_job_message(job_id)
    wait_until(lambda: len(list(fx.glob(f"started.{job_id}.*"))) == 1, what="stage gated")
    wait_until(lambda: procs.all_logs().count("deferring duplicate message") >= 12, timeout=30,
               what="more deferrals than the delivery limit")
    assert queue_state("jobs.dlq").messages == 0
    (fx / f"release.{job_id}").write_text("")
    wait_for_status(db, job_id, {"COMPLETED"})
