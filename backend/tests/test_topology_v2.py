"""Topology v2: queues/bindings/arguments, delay queues, outbox destinations, legacy drain."""
import asyncio
import uuid

import psycopg
from psycopg.rows import dict_row

from notesagent.common import jobs_repo
from notesagent.common.broker import (
    EXCHANGE_DLX,
    EXCHANGE_RETRY,
    LEGACY_QUEUE_WORK,
    QUEUE_DLQ_AUDIT,
    QUEUE_DLQ_PARKED,
    QUEUE_WORK,
    audit_queue_arguments,
    delay_queue_arguments,
    delay_queue_name,
    parked_queue_arguments,
    work_queue_arguments,
)
from notesagent.common.config import get_settings
from tests.support.helpers import drain_outbox, submit_ok, wait_for_status, wait_until
from tests.support.legacy import bound_routing_keys, mgmt_api, setup_broker
from tests.support.rabbit import queue_state

TEST_DELAYS = [2, 4, 5, 8, 30]   # BACKOFF_SCHEDULE_SEC=2,4,8 (conftest) + lock 5 + infra 30


def test_fresh_broker_gets_v2_topology_and_no_legacy_queue(api, rabbit_admin):
    settings = get_settings()
    v2 = rabbit_admin.queue_details(QUEUE_WORK)
    assert v2["durable"] and v2["arguments"] == work_queue_arguments(settings)
    assert v2["arguments"]["x-dead-letter-strategy"] == "at-least-once"
    assert v2["arguments"]["x-overflow"] == "reject-publish"
    assert bound_routing_keys(QUEUE_WORK) == ["job.work"]
    assert LEGACY_QUEUE_WORK not in rabbit_admin.queue_names()              # never declared any more
    assert queue_state("jobs.dlq").exists

    assert get_settings().delay_queue_seconds == TEST_DELAYS
    for seconds in TEST_DELAYS:
        name = delay_queue_name(seconds)
        details = rabbit_admin.queue_details(name)
        assert details["arguments"] == delay_queue_arguments(seconds)
        bindings = [b["routing_key"] for b in mgmt_api().bindings(EXCHANGE_RETRY, name)]
        assert bindings == [f"{seconds}s"]


def test_dead_letter_audit_and_parking_queues(api, rabbit_admin):
    """ADR-12 / P4-D5: audit queue bound to the DLX fanout, dead-lettering at-least-once (needs
    reject-publish) to an unbound parking queue that never discards."""
    settings = get_settings()
    audit = rabbit_admin.queue_details(QUEUE_DLQ_AUDIT)
    assert audit["durable"] and audit["arguments"] == audit_queue_arguments(settings) == {
        "x-queue-type": "quorum", "x-delivery-limit": settings.queue_delivery_limit + 11,
        "x-dead-letter-exchange": "", "x-dead-letter-routing-key": QUEUE_DLQ_PARKED,
        "x-dead-letter-strategy": "at-least-once", "x-overflow": "reject-publish"}
    assert audit["arguments"]["x-delivery-limit"] > settings.queue_delivery_limit + 1     # carried-over count
    parked = rabbit_admin.queue_details(QUEUE_DLQ_PARKED)
    assert parked["durable"] and parked["arguments"] == parked_queue_arguments() == {
        "x-queue-type": "quorum", "x-delivery-limit": -1}
    for name in (QUEUE_DLQ_AUDIT, QUEUE_DLQ_PARKED):
        assert "x-message-ttl" not in rabbit_admin.queue_details(name)["arguments"]
        assert "x-max-length" not in rabbit_admin.queue_details(name)["arguments"]
    assert [b["destination"] for b in mgmt_api().bindings(EXCHANGE_DLX, QUEUE_DLQ_AUDIT)] == [QUEUE_DLQ_AUDIT]
    assert [b["destination"] for b in mgmt_api().bindings(EXCHANGE_DLX, "jobs.dlq")] == ["jobs.dlq"]
    parked_bindings = rabbit_admin.client.get(f"/api/queues/notes_test/{QUEUE_DLQ_PARKED}/bindings").json()
    assert [b["source"] for b in parked_bindings] == [""]           # only the implicit default binding


def test_services_coexist_with_existing_legacy_queue(rabbit_admin):
    """Phase 1 state present: starting v2 services must not redeclare/alter/delete jobs.work."""
    setup_broker(legacy=True, legacy_bound=True, messages=1)
    from fastapi.testclient import TestClient
    from notesagent.gateway.main import create_app
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
    legacy = rabbit_admin.queue_details(LEGACY_QUEUE_WORK)
    assert "x-dead-letter-strategy" not in legacy["arguments"]               # untouched
    assert queue_state(LEGACY_QUEUE_WORK).messages == 1
    assert bound_routing_keys(LEGACY_QUEUE_WORK) == ["job.work"]             # unbinding is the script's job


def test_outbox_row_with_retry_destination_reaches_delay_queue_then_work_queue(api, db):
    job_id = submit_ok(api)
    assert drain_outbox() == 1
    assert queue_state(QUEUE_WORK).messages == 1

    async def _insert_retry_event():
        conn = await psycopg.AsyncConnection.connect(get_settings().database_url.get_secret_value(),
                                                     autocommit=True, row_factory=dict_row)
        async with conn:
            await jobs_repo.insert_outbox(conn, event_id=uuid.uuid4(), job_id=uuid.UUID(job_id),
                                          event_type="job.retry", payload={"job_id": job_id},
                                          destination_exchange=EXCHANGE_RETRY, destination_routing_key="2s")
    asyncio.run(_insert_retry_event())
    assert drain_outbox() == 1
    assert queue_state(delay_queue_name(2)).messages == 1                   # waiting in the delay queue
    wait_until(lambda: queue_state(QUEUE_WORK).messages == 2, timeout=15, what="TTL expiry back to v2")
    assert queue_state(delay_queue_name(2)).messages == 0


def test_unroutable_outbox_destination_stays_pending(api, db):
    job_id = submit_ok(api)
    assert drain_outbox() == 1

    async def _insert_bad_event():
        conn = await psycopg.AsyncConnection.connect(get_settings().database_url.get_secret_value(),
                                                     autocommit=True, row_factory=dict_row)
        async with conn:
            await jobs_repo.insert_outbox(conn, event_id=uuid.uuid4(), job_id=uuid.UUID(job_id),
                                          event_type="job.retry", payload={"job_id": job_id},
                                          destination_exchange=EXCHANGE_RETRY, destination_routing_key="999s")
    asyncio.run(_insert_bad_event())
    try:
        drain_outbox()
    except Exception:  # noqa: BLE001 - the publisher raises on an unroutable (returned) message
        pass
    row = db.execute("SELECT published_at, last_error FROM outbox WHERE destination_routing_key = '999s'").fetchone()
    assert row["published_at"] is None and row["last_error"]                 # never silently dropped


def test_worker_drains_legacy_queue(api, db, procs, fx, rabbit_admin):
    job_id = submit_ok(api)                                   # real job row (its outbox row is NOT published)
    setup_broker(legacy=True, legacy_bound=False, job_ids=[job_id])
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx)
    wait_for_status(db, job_id, {"COMPLETED"})
    procs.wait_for_log("worker-a", "processing message from legacy queue")
    assert queue_state(LEGACY_QUEUE_WORK).messages == 0
    assert queue_state(LEGACY_QUEUE_WORK).consumers == 0                     # drain uses basic.get, no consumer


def test_worker_stops_draining_when_legacy_queue_is_deleted(procs, fx):
    setup_broker(legacy=True, legacy_bound=False)
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx)
    procs.wait_for_log("worker-a", "draining legacy work queue")

    async def _delete():
        import aio_pika, os  # noqa: E401
        conn = await aio_pika.connect(os.environ["TEST_RABBITMQ_URL"])
        async with conn:
            ch = await conn.channel()
            await ch.queue_delete(LEGACY_QUEUE_WORK)
    asyncio.run(_delete())
    procs.wait_for_log("worker-a", "drain finished")
    assert procs.procs["worker-a"].poll() is None                           # worker keeps running on v2
