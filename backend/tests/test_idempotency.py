"""Idempotency-Key behaviour for POST /jobs (ADR-04)."""
import uuid
from concurrent.futures import ThreadPoolExecutor

from tests.support.helpers import count, new_token, submit, token_secret_part, wait_until


def _cred_queues(rabbit_admin):
    return [q for q in rabbit_admin.queue_names() if q.startswith("cred.")]


def test_same_key_same_body_returns_original_job(api, db, rabbit_admin):
    key, token = str(uuid.uuid4()), new_token()
    first = submit(api, token=token, key=key)
    second = submit(api, token=token, key=key)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert second.headers["location"] == first.headers["location"]
    assert second.headers.get("idempotent-replayed") == "true" and "idempotent-replayed" not in first.headers
    assert count(db, "jobs") == 1 and count(db, "outbox") == 1
    assert len(_cred_queues(rabbit_admin)) == 1


def test_same_key_different_body_is_rejected(api, db):
    key, token = str(uuid.uuid4()), new_token()
    assert submit(api, token=token, key=key).status_code == 202
    other_video = submit(api, token=token, key=key, url="https://youtu.be/aaaaaaaaaaa")
    other_token = submit(api, token=new_token(), key=key)
    for resp in (other_video, other_token):
        assert resp.status_code == 422 and resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert count(db, "jobs") == 1


def test_no_key_means_independent_jobs(api, db):
    token = new_token()
    a, b = submit(api, token=token), submit(api, token=token)
    assert a.json()["job_id"] != b.json()["job_id"] and count(db, "jobs") == 2


def test_concurrent_retries_with_same_key_create_one_job(api, db, rabbit_admin):
    key, token = str(uuid.uuid4()), new_token()
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: submit(api, token=token, key=key), range(8)))
    assert all(r.status_code == 202 for r in responses), [r.text for r in responses]
    assert len({r.json()["job_id"] for r in responses}) == 1
    assert count(db, "jobs") == 1 and count(db, "outbox") == 1
    # Mailboxes created by losing requests are deleted again.
    wait_until(lambda: len(_cred_queues(rabbit_admin)) == 1, timeout=10, what="one credential mailbox")


def test_expired_key_creates_new_job(api, db):
    key, token = str(uuid.uuid4()), new_token()
    first = submit(api, token=token, key=key).json()["job_id"]
    db.execute("UPDATE idempotency_keys SET created_at = now() - interval '25 hours' WHERE key = %s", (key,))
    second = submit(api, token=token, key=key)
    assert second.status_code == 202 and second.json()["job_id"] != first
    assert "idempotent-replayed" not in second.headers
    assert count(db, "jobs") == 2 and count(db, "idempotency_keys") == 1


def test_fingerprint_does_not_store_token(api, db):
    key, token = str(uuid.uuid4()), new_token()
    submit(api, token=token, key=key)
    row = db.execute("SELECT row_to_json(k)::text AS t FROM idempotency_keys k").fetchone()["t"]
    assert token_secret_part(token) not in row
