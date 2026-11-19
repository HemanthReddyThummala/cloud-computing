"""Encrypted per-job credential mailbox (ADR-03)."""
import hashlib
import os
import uuid

from fastapi.testclient import TestClient

from notesagent.common.config import get_settings
from notesagent.common.crypto import CredentialCipher
from notesagent.gateway.main import create_app
from tests.support.helpers import (
    drain_outbox,
    files,
    new_token,
    release,
    started_pids,
    submit_ok,
    token_secret_part,
    wait_for_status,
    wait_until,
)
from tests.support.rabbit import peek_messages, queue_state

DAY_MS = 24 * 3600 * 1000


def test_mailbox_is_encrypted_durable_and_expiring(api, rabbit_admin):
    token = new_token()
    job_id = submit_ok(api, token=token)
    name = f"cred.{job_id}"

    details = rabbit_admin.queue_details(name)
    assert details["durable"] is True
    assert details["arguments"]["x-expires"] == DAY_MS and details["arguments"]["x-message-ttl"] == DAY_MS

    [msg] = peek_messages(name)
    assert msg.delivery_mode == 2
    assert token_secret_part(token).encode() not in msg.body          # ciphertext only
    cipher = CredentialCipher(os.environ["JOB_PAYLOAD_ENCRYPTION_KEY"])
    assert cipher.open(uuid.UUID(job_id), msg.body) == token
    assert queue_state(name).messages == 1                             # reading did not consume it


def test_mailbox_survives_worker_crash_and_is_deleted_after_completion(api, db, procs, fx):
    for name in ("worker-a", "worker-b"):
        procs.start(name, "tests.support.run_fixture_worker", FIXTURE_DIR=fx,
                    FIXTURE_GATE="PUBLISHING", FIXTURE_TOKEN_STAGE="PUBLISHING")
    for name in ("worker-a", "worker-b"):
        procs.wait_for_log(name, "worker started")

    token = new_token()
    job_id = submit_ok(api, token=token)
    drain_outbox()
    [victim] = wait_until(lambda: started_pids(fx, job_id, "PUBLISHING"), what="publishing started")
    procs.sigkill(victim)                                              # crash after reading the token

    wait_until(lambda: queue_state(f"cred.{job_id}").messages == 1, timeout=10, what="credential still present")
    wait_until(lambda: [p for p in started_pids(fx, job_id, "PUBLISHING") if p != victim], timeout=30,
               what="other worker resumed")
    release(fx, job_id)
    wait_for_status(db, job_id, {"COMPLETED"})

    digests = {p.read_text() for p in files(fx, f"tokenhash.{job_id}.")}
    assert len(files(fx, f"tokenhash.{job_id}.")) == 2                 # both workers obtained it
    assert digests == {hashlib.sha256(token.encode()).hexdigest()}
    wait_until(lambda: not queue_state(f"cred.{job_id}").exists, timeout=10, what="mailbox deleted")
    assert token_secret_part(token) not in procs.all_logs()           # never logged


def test_mailbox_expires_after_ttl(rabbit_admin):
    # NOTE: a queue.declare (even passive) or basic.get counts as "using" the queue and resets
    # x-expires, so this test observes the queue only via the management API listing.
    settings = get_settings(credential_ttl_hours=3 / 3600)            # 3 seconds
    with TestClient(create_app(settings)) as client:
        job_id = submit_ok(client)
    name = f"cred.{job_id}"
    assert name in rabbit_admin.queue_names()
    wait_until(lambda: name not in rabbit_admin.queue_names(), timeout=30, interval=0.5,
               what="mailbox expired by x-expires")
