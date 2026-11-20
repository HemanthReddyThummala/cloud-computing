"""ADR-14 TRANSCRIBING stage inside the REAL worker (production stage code, real runner, outbox
retries, Redis limiter), with the YouTube client replaced by tests.support.fake_youtube (no network)."""
import asyncio
import os
import time

import redis.asyncio as aioredis

from notesagent.common.config import get_settings
from notesagent.common.errors import Provider, RateLimited
from notesagent.common.ratelimit import RateLimiter
from tests.support.helpers import (
    drain_outbox,
    events,
    job,
    submit_ok,
    wait_for_status,
    wait_until,
)

YT_WORKER = "tests.support.run_youtube_worker"
TERMINAL = {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED"}


def calls(fx, kind):
    return len(list(fx.glob(f"call.{kind}.*")))


def checkpoint(db, job_id):
    rows = db.execute("SELECT stage, data FROM job_checkpoints WHERE job_id = %s AND stage = 'TRANSCRIBING'",
                      (job_id,)).fetchall()
    return rows


def start(procs, fx, name="worker-a", **env):
    procs.start(name, YT_WORKER, FIXTURE_DIR=fx, **env)
    procs.wait_for_log(name, "worker started")


# ===================================================================== success
def test_transcript_end_to_end_with_real_worker(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()
    start(procs, fx, FAKE_YT_WITH_FIXTURES="1")
    row = wait_for_status(db, job_id, TERMINAL)
    assert row["status"] == "COMPLETED"
    assert (row["video_title"], row["duration_sec"]) == ("Distributed Systems Lecture", 600)
    [cp] = checkpoint(db, job_id)
    t = cp["data"]["transcript"]
    assert (t["language_code"], t["is_generated"], t["segment_count"]) == ("en", False, 3)   # manual en preferred
    assert [(s["start"], s["end"], s["text"]) for s in t["segments"]] == [
        (0.0, 2.5, "Welcome to the lecture"), (2.5, 5.75, "Today: distributed queues"),
        (6.75, 10.75, "RabbitMQ & quorum queues")]
    assert (calls(fx, "info"), calls(fx, "list"), calls(fx, "fetch")) == (1, 1, 1)
    status = api.get(f"/jobs/{job_id}").json()
    assert (status["video_title"], status["duration_sec"]) == ("Distributed Systems Lecture", 600)


def test_exactly_1200_seconds_is_accepted(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()
    start(procs, fx, FAKE_YT_DURATION="1200")
    row = wait_for_status(db, job_id, TERMINAL)
    # transcript (and fake summary) done; the job stops honestly at the first not-yet-implemented stage
    assert row["duration_sec"] == 1200 and row["error"]["code"] == "NOTION_PARENT_MISSING"
    assert checkpoint(db, job_id) and calls(fx, "fetch") == 1


# ===================================================================== permanent failures -> FAILED once
def test_longer_than_1200_seconds_fails_before_any_transcript_request(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()
    start(procs, fx, FAKE_YT_DURATION="1201")
    row = wait_for_status(db, job_id, TERMINAL)
    assert row["status"] == "FAILED" and row["error"]["code"] == "VIDEO_TOO_LONG"
    assert "20:00" in row["error"]["message"] and row["attempts"] == 0
    assert (calls(fx, "info"), calls(fx, "list"), calls(fx, "fetch")) == (1, 0, 0)
    assert checkpoint(db, job_id) == []
    assert [e["to_status"] for e in events(db, job_id)][-1] == "FAILED"


def test_unavailable_video_404_fails_without_retry(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()
    start(procs, fx, FAKE_YT="http404")
    row = wait_for_status(db, job_id, TERMINAL)
    assert (row["status"], row["error"]["code"], row["attempts"]) == ("FAILED", "YOUTUBE_VIDEO_UNAVAILABLE", 0)
    time.sleep(1)
    assert calls(fx, "info") == 1                                          # never retried


def test_transcripts_disabled_fails_without_retry(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()
    start(procs, fx, FAKE_YT="disabled")
    row = wait_for_status(db, job_id, TERMINAL)
    assert (row["status"], row["error"]["code"]) == ("FAILED", "YOUTUBE_TRANSCRIPT_UNAVAILABLE")
    assert (calls(fx, "info"), calls(fx, "list"), calls(fx, "fetch")) == (1, 1, 0)


# ===================================================================== temporary failures -> existing retries
def test_youtube_429_uses_shared_cooldown_and_the_existing_retry(api, db, procs, fx):
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    start(procs, fx, FAKE_YT="rate_limited_once:2", FAKE_YT_WITH_FIXTURES="1")
    retry = wait_until(lambda: job(db, job_id)["retry"], timeout=30, what="retry scheduled")
    assert retry["reason_code"] == "YOUTUBE_RATE_LIMITED" and retry["delay_sec"] >= 2
    procs.wait_for_log("worker-a", "provider cooldown shared with all workers")
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert (row["status"], row["attempts"]) == ("COMPLETED", 1)
    assert calls(fx, "info") == 2


def test_transient_network_error_is_retried_then_succeeds(api, db, procs, fx):
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    start(procs, fx, FAKE_YT="network_once", FAKE_YT_WITH_FIXTURES="1")
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert (row["status"], row["attempts"]) == ("COMPLETED", 1)
    reasons = [e["detail"]["reason_code"] for e in events(db, job_id) if e["event_type"] == "retry_scheduled"]
    assert reasons == ["YOUTUBE_UNREACHABLE"]


def test_persistent_network_failure_is_bounded_and_dead_lettered(api, db, procs, fx):
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    start(procs, fx, FAKE_YT="network_always", MAX_ATTEMPTS=2)
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert row["status"] == "DEAD_LETTERED" and row["error"]["code"] == "RETRIES_EXHAUSTED"
    assert row["error"]["last_error_code"] == "YOUTUBE_UNREACHABLE" and calls(fx, "info") == 2


def test_active_youtube_cooldown_delays_the_first_request(api, db, procs, fx):
    async def cooldown():
        client = aioredis.from_url(os.environ["TEST_REDIS_URL"])
        try:
            await RateLimiter(client, get_settings()).report(
                RateLimited("YOUTUBE_RATE_LIMITED", "x", provider=Provider.YOUTUBE, retry_after_sec=3))
        finally:
            await client.aclose()
    start(procs, fx, FAKE_YT_WITH_FIXTURES="1")
    job_id = submit_ok(api)
    t0 = time.time()
    asyncio.run(cooldown())
    drain_outbox()
    wait_for_status(db, job_id, TERMINAL)
    first_call = min(p.stat().st_mtime for p in fx.glob("call.info.*"))
    assert first_call - t0 >= 2.5                                          # waited for the shared cooldown


# ===================================================================== crash / redelivery / idempotency
def test_crash_during_transcribing_redelivers_and_stores_one_checkpoint(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()
    procs.start("worker-a", YT_WORKER, FIXTURE_DIR=fx, FAKE_YT_GATE="info", FAKE_YT_WITH_FIXTURES="1")
    wait_until(lambda: list(fx.glob("started.yt.info.*")), timeout=30, what="worker-a inside the YouTube call")
    procs.sigkill(procs.procs["worker-a"].pid)
    start(procs, fx, name="worker-b", FAKE_YT_WITH_FIXTURES="1")
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert row["status"] == "COMPLETED" and row["attempts"] == 0           # a crash is not a counted attempt
    assert calls(fx, "info") == 2 and len(checkpoint(db, job_id)) == 1     # repeated once, stored once


def test_redelivery_after_checkpoint_does_not_repeat_transcribing(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()
    # SUMMARIZING / EXTRACTING_FRAMES are production stages now (ADR-15/16); pause in the PUBLISHING fixture.
    procs.start("worker-a", YT_WORKER, FIXTURE_DIR=fx, FAKE_YT_WITH_FIXTURES="1", FIXTURE_GATE="PUBLISHING")
    wait_until(lambda: list(fx.glob(f"started.{job_id}.PUBLISHING.*")), timeout=30,
               what="in PUBLISHING")
    procs.sigkill(procs.procs["worker-a"].pid)
    start(procs, fx, name="worker-b", FAKE_YT_WITH_FIXTURES="1")
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert row["status"] == "COMPLETED"
    assert (calls(fx, "info"), calls(fx, "list"), calls(fx, "fetch")) == (1, 1, 1)   # resumed from the checkpoint
    assert len(checkpoint(db, job_id)) == 1
