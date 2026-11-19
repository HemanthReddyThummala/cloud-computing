"""Credential mailbox under crashes, concurrent holders and TTL (review follow-up).

Every test drives the PRODUCTION JobWorker / BrokerClient; only the stage bodies are
test stand-ins (tests/support/fixture_stages.py).
"""
import asyncio
import hashlib
import time

from fastapi.testclient import TestClient

from notesagent.common.broker import BrokerClient
from notesagent.common.config import get_settings
from notesagent.gateway.main import create_app
from tests.support.helpers import drain_outbox, files, job, new_token, submit_ok, wait_for_status, wait_until
from tests.support.rabbit import peek_messages, queue_state


def _start_holder(procs, fx, job_id):
    """Hold the credential message unacked in another process (= a worker between get and nack)."""
    marker = fx / "holder.held"
    proc = procs.start("holder", "tests.support.hold_message", f"cred.{job_id}", marker)
    wait_until(marker.exists, timeout=15, what="credential held by another process")
    return proc


def test_crash_between_get_and_nack_returns_the_same_message(api, procs, fx):
    token = new_token()
    job_id = submit_ok(api, token=token)
    [original] = peek_messages(f"cred.{job_id}")

    holder = _start_holder(procs, fx, job_id)
    assert queue_state(f"cred.{job_id}").messages == 0          # held (unacked) elsewhere
    procs.sigkill(holder.pid)                                    # crash before nack
    wait_until(lambda: queue_state(f"cred.{job_id}").messages == 1, timeout=10, what="message returned")

    [returned] = peek_messages(f"cred.{job_id}")
    assert returned.body == original.body                        # same message, not a republished copy
    assert returned.message_id == original.message_id == job_id


def test_worker_waits_for_credential_held_by_crashed_peer(api, db, procs, fx):
    """Without the fix the job FAILED immediately with NOTION_CREDENTIALS_UNAVAILABLE."""
    token = new_token()
    job_id = submit_ok(api, token=token)
    holder = _start_holder(procs, fx, job_id)

    drain_outbox()
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx,
                FIXTURE_TOKEN_STAGE="TRANSCRIBING", CREDENTIAL_READ_WAIT_SEC=60)
    procs.wait_for_log("worker-a", "credential mailbox temporarily empty")
    assert job(db, job_id)["status"] == "TRANSCRIBING"           # waiting, not failed

    procs.sigkill(holder.pid)                                    # broker now returns the message
    wait_for_status(db, job_id, {"COMPLETED"}, timeout=30)
    [digest] = {p.read_text() for p in files(fx, f"tokenhash.{job_id}.")}
    assert digest == hashlib.sha256(token.encode()).hexdigest()


def test_missing_mailbox_fails_immediately(api, db, procs, fx, rabbit_admin):
    job_id = submit_ok(api)
    asyncio.run(_delete_mailbox(job_id))
    drain_outbox()
    started = time.monotonic()
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx,
                FIXTURE_TOKEN_STAGE="TRANSCRIBING", CREDENTIAL_READ_WAIT_SEC=60)
    row = wait_for_status(db, job_id, {"FAILED", "COMPLETED"}, timeout=30)
    assert row["status"] == "FAILED" and row["error"]["code"] == "NOTION_CREDENTIALS_UNAVAILABLE"
    assert row["result"] is None
    assert time.monotonic() - started < 20                       # did not wait the 60 s


def test_mailbox_without_message_fails_after_bounded_wait(api, db, procs, fx):
    job_id = submit_ok(api)
    holder = _start_holder(procs, fx, job_id)                    # never released
    drain_outbox()
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx,
                FIXTURE_TOKEN_STAGE="TRANSCRIBING", CREDENTIAL_READ_WAIT_SEC=3)
    row = wait_for_status(db, job_id, {"FAILED", "COMPLETED"}, timeout=30)
    assert row["status"] == "FAILED" and row["error"]["code"] == "NOTION_CREDENTIALS_UNAVAILABLE"
    assert holder.poll() is None


def test_requeue_keeps_the_original_message_ttl():
    """Repeated get+nack must NOT extend the credential's lifetime beyond submission + TTL."""
    settings = get_settings(credential_ttl_hours=4 / 3600)      # 4 seconds
    with TestClient(create_app(settings)) as client:
        job_id = submit_ok(client)
        submitted = time.monotonic()

    async def _peek_loop():
        broker = BrokerClient(get_settings(), name="ttl-test")
        await broker.start()
        seen = []
        try:
            while time.monotonic() - submitted < 7:
                peek = await broker.peek_credential(job_id)     # production read path
                seen.append((round(time.monotonic() - submitted, 1), peek.body is not None))
                await asyncio.sleep(0.25)
        finally:
            await broker.close()
        return seen

    seen = asyncio.run(_peek_loop())
    present = [t for t, ok in seen if ok]
    assert present, "credential should be readable before the TTL"
    assert max(present) < 5.0, f"credential outlived its TTL despite requeues: {seen}"
    assert not seen[-1][1]


async def _delete_mailbox(job_id):
    broker = BrokerClient(get_settings(), name="test")
    await broker.start()
    try:
        await broker.delete_credential(job_id)
    finally:
        await broker.close()
