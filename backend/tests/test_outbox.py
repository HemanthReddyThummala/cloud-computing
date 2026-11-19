"""Transactional outbox: confirm-before-mark, failures, crash after confirm, duplicates."""
import asyncio
import json
import time

import pytest

from notesagent.common.config import get_settings
from notesagent.common.errors import BrokerUnavailable
from notesagent.outbox.publisher import OutboxPublisher
from tests.support.helpers import (
    drain_outbox,
    events,
    new_token,
    run_count,
    submit_ok,
    token_secret_part,
    wait_for_status,
    wait_until,
)
from tests.support.rabbit import peek_messages, queue_state


def _outbox(db, job_id):
    return db.execute("SELECT * FROM outbox WHERE job_id = %s", (job_id,)).fetchone()


def test_publisher_marks_event_published_after_confirm(api, db):
    token = new_token()
    job_id = submit_ok(api, token=token)
    assert _outbox(db, job_id)["published_at"] is None

    assert drain_outbox() == 1
    row = _outbox(db, job_id)
    assert row["published_at"] is not None and row["publish_attempts"] == 1 and row["last_error"] is None

    [msg] = peek_messages("jobs.work.v2")
    assert msg.message_id == str(row["event_id"])
    assert msg.delivery_mode == 2  # persistent
    assert json.loads(msg.body)["job_id"] == job_id
    assert token_secret_part(token).encode() not in msg.body
    assert drain_outbox() == 0  # nothing left


def test_publish_failure_keeps_event_pending(api, db):
    job_id = submit_ok(api)

    async def _failing_publish():
        publisher = OutboxPublisher(get_settings())
        await publisher.open()
        await publisher.broker.connection.close()          # broker connection lost
        try:
            with pytest.raises(BrokerUnavailable):
                await publisher.publish_one()
        finally:
            await publisher.close()

    asyncio.run(_failing_publish())
    row = _outbox(db, job_id)
    assert row["published_at"] is None and row["publish_attempts"] == 1
    assert row["last_error"] == "BrokerUnavailable"
    assert queue_state("jobs.work.v2").messages == 0

    assert drain_outbox() == 1                              # recovers once the broker is back
    assert _outbox(db, job_id)["published_at"] is not None


def test_crash_after_confirm_duplicates_message_but_job_runs_once(api, db, procs, fx):
    job_id = submit_ok(api)
    crasher = procs.start("crashing-publisher", "tests.support.run_publisher_crash_after_confirm")
    assert crasher.wait(timeout=30) == -9                   # SIGKILL after confirm, before marking

    row = _outbox(db, job_id)
    assert row["published_at"] is None                      # DB never learned it was published
    assert queue_state("jobs.work.v2").messages == 1           # ...but the broker has the message

    assert drain_outbox() == 1                              # restarted publisher sends it again
    copies = peek_messages("jobs.work.v2")
    assert len(copies) == 2 and {m.message_id for m in copies} == {str(row["event_id"])}

    procs.start("w1", "tests.support.run_fixture_worker", FIXTURE_DIR=fx)
    wait_for_status(db, job_id, {"COMPLETED"})
    for stage in ("TRANSCRIBING", "SUMMARIZING", "EXTRACTING_FRAMES", "PUBLISHING"):
        assert run_count(fx, job_id, stage) == 1, stage     # consumer idempotency: no double work
    assert [e["event_type"] for e in events(db, job_id)].count("claimed") == 1
    wait_until(lambda: queue_state("jobs.work.v2").messages == 0, timeout=15, what="duplicate acknowledged")
    procs.wait_for_log("w1", "duplicate message for finished job dropped")


def test_publisher_service_wakes_on_notify(api, db, procs):
    # Poll interval of 60 s: only the LISTEN/NOTIFY path can publish quickly.
    procs.start("publisher", "notesagent.outbox", OUTBOX_POLL_INTERVAL_MS=60000)
    procs.wait_for_log("publisher", "outbox publisher started")
    time.sleep(2)                                           # let LISTEN be established
    job_id = submit_ok(api)
    wait_until(lambda: _outbox(db, job_id)["published_at"] is not None, timeout=10, what="published via NOTIFY")
    assert queue_state("jobs.work.v2").messages == 1
