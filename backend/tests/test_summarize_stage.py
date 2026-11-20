"""ADR-15 SUMMARIZING stage inside the REAL worker (production stage code, real runner, outbox
retries, Redis limiter) with YouTube and Gemini replaced by fakes (no network)."""
import json
import os
import time
from datetime import datetime, timezone

import redis

from notesagent.common.ratelimit import pacific_day
from tests.support.helpers import drain_outbox, events, job, submit_ok, wait_for_status, wait_until

WORKER = "tests.support.run_youtube_worker"
TERMINAL = {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED"}
REDIS_URL = os.environ["TEST_REDIS_URL"]
FAKE_KEY = "AIzaFAKE-integration-key-0123456789abcd"


def gemini_calls(fx, kind="*"):
    return sorted(fx.glob(f"call.gemini.{kind}.*"), key=lambda p: p.stat().st_mtime)


def summary_cp(db, job_id):
    rows = db.execute("SELECT data FROM job_checkpoints WHERE job_id = %s AND stage = 'SUMMARIZING'",
                      (job_id,)).fetchall()
    return [r["data"] for r in rows]


def start(procs, fx, name="worker-a", **env):
    procs.start(name, WORKER, FIXTURE_DIR=fx, **env)
    procs.wait_for_log(name, "worker started")


def submit(api):
    job_id = submit_ok(api)
    drain_outbox()
    return job_id


# ===================================================================== success
def test_short_transcript_is_summarized_and_job_stops_at_the_next_missing_stage(api, db, procs, fx):
    job_id = submit(api)
    start(procs, fx)
    row = wait_for_status(db, job_id, TERMINAL)
    # PUBLISHING exists now (ADR-17); without a Notion destination it fails honestly before any request
    assert row["status"] == "FAILED" and row["error"]["code"] == "NOTION_PARENT_MISSING"
    [cp] = summary_cp(db, job_id)
    assert cp["chunk_count"] == 1 and cp["gemini_requests"] == 1 and len(gemini_calls(fx)) == 1
    assert cp["summary"] and cp["key_points"] and cp["sections"]
    # short fake transcript: 3 non-empty segments starting at 0.0, 2.5, 6.75 s. The fake selects
    # segments 0 and 2; they are 6.75 s apart (< 10 s) -> de-duplicated to the first one.
    assert [(t["segment_index"], t["timestamp_sec"]) for t in cp["important_timestamps"]] == [(0, 0.0)]
    for t in cp["important_timestamps"]:
        assert t["caption"] and t["reason"] and t["transcript_excerpt"]
    assert [e["to_status"] for e in events(db, job_id)] == ["QUEUED", "TRANSCRIBING", "SUMMARIZING",
                                                            "EXTRACTING_FRAMES", "PUBLISHING", "FAILED"]
    r = redis.Redis.from_url(REDIS_URL)                                         # one permit per request
    assert int(r.get(f"ratelimit:gemini:rpd:{pacific_day(datetime.now(timezone.utc))}")) == 1


def test_full_pipeline_with_fixture_stand_ins_completes(api, db, procs, fx):
    job_id = submit(api)
    start(procs, fx, FAKE_YT_WITH_FIXTURES="1")
    assert wait_for_status(db, job_id, TERMINAL)["status"] == "COMPLETED"
    assert len(summary_cp(db, job_id)) == 1


def test_long_transcript_is_chunked_and_merged_in_order(api, db, procs, fx):
    job_id = submit(api)
    start(procs, fx, FAKE_YT_SEGMENTS=120, TRANSCRIPT_CHUNK_TOKENS=1000, GEMINI_RPM_LIMIT=100)
    wait_for_status(db, job_id, TERMINAL, timeout=60)
    [cp] = summary_cp(db, job_id)
    chunk_calls = [json.loads(p.read_text()) for p in gemini_calls(fx, "chunk")]
    n = cp["chunk_count"]
    assert n > 1 and len(chunk_calls) == n and len(gemini_calls(fx, "merge")) == 1 and cp["gemini_requests"] == n + 1
    ranges = sorted((c["first"], c["last"]) for c in chunk_calls)
    assert ranges[0][0] == 0 and ranges[-1][1] == 119
    assert all(b[0] == a[1] + 1 for a, b in zip(ranges, ranges[1:]))          # contiguous, no overlap
    assert cp["summary"] == "Overall summary of the whole video."
    starts = [s["start_sec"] for s in cp["sections"]]
    ts = [t["timestamp_sec"] for t in cp["important_timestamps"]]
    assert starts == sorted(starts) and ts == sorted(ts) and max(ts) > 300     # original video time
    assert all(t["timestamp_sec"] == t["segment_index"] * 5.0 for t in cp["important_timestamps"])
    r = redis.Redis.from_url(REDIS_URL)
    assert int(r.get(f"ratelimit:gemini:rpd:{pacific_day(datetime.now(timezone.utc))}")) == n + 1


# ===================================================================== errors -> existing retries
def test_gemini_429_retry_info_is_honoured_and_shared(api, db, procs, fx):
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    start(procs, fx, FAKE_GEMINI="rate_limited_once:3", FAKE_YT_WITH_FIXTURES="1")
    retry = wait_until(lambda: job(db, job_id)["retry"], timeout=30, what="retry scheduled")
    assert retry["reason_code"] == "GEMINI_RATE_LIMITED" and retry["delay_sec"] >= 3
    ttl = redis.Redis.from_url(REDIS_URL).pttl("ratelimit:cooldown:gemini")
    assert 1500 < ttl <= 3000                                                   # shared cooldown = RetryInfo
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert (row["status"], row["attempts"]) == ("COMPLETED", 1)


def test_gemini_daily_quota_waits_for_the_pacific_reset(api, db, procs, fx):
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    start(procs, fx, FAKE_GEMINI="quota")
    retry = wait_until(lambda: job(db, job_id)["retry"], timeout=30, what="retry scheduled")
    assert retry["reason_code"] == "GEMINI_QUOTA_EXHAUSTED" and retry["resets_at"]
    resets = datetime.fromisoformat(retry["resets_at"])
    assert resets.astimezone(timezone.utc) > datetime.now(timezone.utc)
    assert job(db, job_id)["status"] == "SUMMARIZING"                           # waiting, not failed
    until_ms = int(redis.Redis.from_url(REDIS_URL).get("ratelimit:cooldown:gemini"))
    assert abs(until_ms / 1000 - resets.timestamp()) < 5                        # cooldown until the reset


def test_server_error_and_network_error_are_retried(api, db, procs, fx):
    for scenario, code in (("server_once", "GEMINI_HTTP_503"), ("network_once", "GEMINI_UNREACHABLE")):
        for p in fx.glob("call.*"):
            p.unlink()
        job_id = submit_ok(api)
        procs.start(f"publisher-{scenario}", "notesagent.outbox")
        start(procs, fx, name=f"worker-{scenario}", FAKE_GEMINI=scenario, FAKE_YT_WITH_FIXTURES="1")
        row = wait_for_status(db, job_id, TERMINAL, timeout=60)
        assert (row["status"], row["attempts"]) == ("COMPLETED", 1), scenario
        reasons = [e["detail"]["reason_code"] for e in events(db, job_id) if e["event_type"] == "retry_scheduled"]
        assert reasons == [code]
        procs.stop_all()


def test_malformed_output_is_retried_then_dead_lettered(api, db, procs, fx):
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    start(procs, fx, FAKE_GEMINI="invalid_json", MAX_ATTEMPTS=2)
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert row["status"] == "DEAD_LETTERED" and row["error"]["last_error_code"] == "GEMINI_INVALID_OUTPUT"
    assert len(gemini_calls(fx)) == 2 and summary_cp(db, job_id) == []


def test_blocked_output_fails(api, db, procs, fx):
    job_id = submit(api)
    start(procs, fx, FAKE_GEMINI="blocked")
    row = wait_for_status(db, job_id, TERMINAL)
    assert (row["status"], row["error"]["code"], row["attempts"]) == ("FAILED", "GEMINI_OUTPUT_BLOCKED", 0)


# ===================================================================== idempotency / redelivery
def test_redelivery_after_checkpoint_reuses_the_summary(api, db, procs, fx):
    job_id = submit(api)
    procs.start("worker-a", WORKER, FIXTURE_DIR=fx, FAKE_YT_WITH_FIXTURES="1", FIXTURE_GATE="PUBLISHING")
    wait_until(lambda: list(fx.glob(f"started.{job_id}.PUBLISHING.*")), timeout=30, what="past SUMMARIZING")
    procs.sigkill(procs.procs["worker-a"].pid)
    start(procs, fx, name="worker-b", FAKE_YT_WITH_FIXTURES="1")
    assert wait_for_status(db, job_id, TERMINAL, timeout=60)["status"] == "COMPLETED"
    assert len(gemini_calls(fx)) == 1 and len(summary_cp(db, job_id)) == 1    # Gemini not called again


def test_crash_during_summarizing_repeats_only_summarizing(api, db, procs, fx):
    job_id = submit(api)
    procs.start("worker-a", WORKER, FIXTURE_DIR=fx, FAKE_GEMINI_GATE="1", FAKE_YT_WITH_FIXTURES="1")
    wait_until(lambda: list(fx.glob("started.gemini.*")), timeout=30, what="inside the Gemini call")
    procs.sigkill(procs.procs["worker-a"].pid)
    start(procs, fx, name="worker-b", FAKE_YT_WITH_FIXTURES="1")
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert row["status"] == "COMPLETED" and row["attempts"] == 0
    assert len(gemini_calls(fx)) == 2 and len(summary_cp(db, job_id)) == 1
    assert len(list(fx.glob("call.info.*"))) == 1                               # TRANSCRIBING not repeated


# ===================================================================== no key leakage
def test_api_key_never_appears_in_logs_or_job_state(api, db, procs, fx):
    job_id = submit(api)
    start(procs, fx, FAKE_GEMINI="leaky_once", GEMINI_API_KEY=FAKE_KEY)
    row = wait_for_status(db, job_id, TERMINAL)
    assert row["error"]["code"] == "GEMINI_BAD_REQUEST"                         # 400 -> FAILED once
    time.sleep(0.5)
    assert FAKE_KEY not in procs.all_logs()
    assert FAKE_KEY not in json.dumps(api.get(f"/jobs/{job_id}").json())
    assert FAKE_KEY not in json.dumps([dict(e) for e in events(db, job_id)], default=str)
