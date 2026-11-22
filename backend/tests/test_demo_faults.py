"""ADR-22 demo fault switch inside the REAL worker (production stages, runner, outbox retries, Redis
limiter, broker DLQ, real FFmpeg) with YouTube / Gemini / Notion faked (no network). Arming uses the real
operator CLI entry point."""
import json
import os
from datetime import datetime, timezone

import httpx
import pytest
import redis

from notesagent.common.ratelimit import pacific_day
from notesagent.ops import demo_faults as cli
from notesagent.worker.demo_faults import video_key
from tests.support.helpers import VIDEO_ID, drain_outbox, events, job, submit_ok, wait_for_status, wait_until
from tests.support.rabbit import peek_messages, queue_state

WORKER = "tests.support.run_youtube_worker"
TERMINAL = {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED"}
REDIS_URL = os.environ["TEST_REDIS_URL"]
PORT = 18767
NOTION = f"http://127.0.0.1:{PORT}"
PARENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NOTION_ENV = {"FAKE_YT_SEGMENTS": 40, "NOTION_API_BASE_URL": f"{NOTION}/v1", "NOTION_DEFAULT_PARENT_ID": PARENT}
ON = {"DEMO_FAULTS_ENABLED": "true"}


def arm(monkeypatch, scenario):
    monkeypatch.setenv("DEMO_FAULTS_ENABLED", "true")            # the operator's worker container has it on
    assert cli.main(["arm", scenario, "--video", VIDEO_ID]) == 0


def start_worker(procs, fx, name="worker-a", **env):
    procs.start(name, WORKER, FIXTURE_DIR=fx, **env)
    procs.wait_for_log(name, "worker started")


def calls(fx, kind):
    return list(fx.glob(f"call.{kind}.*"))


def injected(procs):
    return procs.all_logs().count('"demo fault injected"')


def retries(db, job_id):
    return [e["detail"] for e in events(db, job_id) if e["event_type"] == "retry_scheduled"]


def start_notion(procs, fx):
    procs.start("notion", "tests.support.fake_notion", PORT, FAKE_NOTION_DIR=fx)
    wait_until(lambda: _alive(), timeout=30, what="fake Notion up")


def _alive():
    try:
        return httpx.get(f"{NOTION}/_state", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


# ===================================================================== dlq
def test_dlq_scenario_dead_letters_without_calling_youtube(api, db, procs, fx, monkeypatch):
    arm(monkeypatch, "dlq")
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    start_worker(procs, fx, FAKE_YT_WITH_FIXTURES="1", MAX_ATTEMPTS=3, **ON)
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert (row["status"], row["attempts"]) == ("DEAD_LETTERED", 3)
    assert row["error"]["code"] == "RETRIES_EXHAUSTED" and row["error"]["last_error_code"] == "YOUTUBE_UNREACHABLE"
    assert [d["reason_code"] for d in retries(db, job_id)] == ["YOUTUBE_UNREACHABLE"] * 2
    assert calls(fx, "info") == []                                     # YouTube never contacted
    wait_until(lambda: queue_state("jobs.dlq").messages == 1, timeout=20, what="DLQ copy")
    [copy] = peek_messages("jobs.dlq")
    assert json.loads(copy.body)["last_error_code"] == "YOUTUBE_UNREACHABLE"
    assert injected(procs) == 3
    assert "simulated network failure" not in json.dumps(api.get(f"/jobs/{job_id}").json())


# ===================================================================== gemini 429 once
def test_gemini_429_once_waits_then_completes_with_one_real_request(api, db, procs, fx, monkeypatch):
    arm(monkeypatch, "gemini_429_once")
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    start_worker(procs, fx, FAKE_YT_WITH_FIXTURES="1", **ON)
    retry = wait_until(lambda: job(db, job_id)["retry"], timeout=30, what="retry scheduled")
    assert retry["reason_code"] == "GEMINI_RATE_LIMITED" and retry["delay_sec"] >= 20
    assert job(db, job_id)["status"] == "SUMMARIZING"                  # waiting, not failed
    assert api.get(f"/jobs/{job_id}").json()["retry"]["reason"] == "GEMINI_RATE_LIMITED"
    assert redis.Redis.from_url(REDIS_URL).pttl("ratelimit:cooldown:gemini") > 0     # shared cooldown
    row = wait_for_status(db, job_id, TERMINAL, timeout=90)
    assert (row["status"], row["attempts"]) == ("COMPLETED", 1)
    [detail] = retries(db, job_id)
    assert (detail["http_status"], detail["provider_code"]) == (429, "RESOURCE_EXHAUSTED")
    assert len(list(fx.glob("call.gemini.*"))) == 1                    # exactly ONE (fake) Gemini request
    rpd = redis.Redis.from_url(REDIS_URL).get(f"ratelimit:gemini:rpd:{pacific_day(datetime.now(timezone.utc))}")
    assert int(rpd) == 2                                               # both attempts took a limiter permit
    assert injected(procs) == 1


# ===================================================================== frames
@pytest.mark.parametrize("scenario,published,images", [("frames_partial", 1, 1), ("frames_all", 0, 0)])
def test_frame_scenarios_complete_with_warnings_and_publish(api, db, procs, fx, monkeypatch, scenario,
                                                            published, images):
    start_notion(procs, fx)
    arm(monkeypatch, scenario)
    job_id = submit_ok(api)
    drain_outbox()
    start_worker(procs, fx, **NOTION_ENV, **ON)
    row = wait_for_status(db, job_id, TERMINAL, timeout=90)
    assert row["status"] == "COMPLETED_WITH_WARNINGS", row["error"]
    assert row["result"]["frames_requested"] == 2 and row["result"]["frames_published"] == published
    assert [w["reason"] for w in row["warnings"]] == ["DEMO_INJECTED_FAILURE"] * (2 - published)
    assert all(w["code"] == "FRAME_EXTRACTION_FAILED" for w in row["warnings"])
    [page] = httpx.get(f"{NOTION}/_state", timeout=5).json()["pages"]   # exactly one page
    blocks = page["children"]
    assert len([b for b in blocks if b["type"] == "image"]) == images
    assert any(b["type"].startswith("heading") for b in blocks)        # text notes still published
    assert injected(procs) == 2 - published


# ===================================================================== safety
def test_armed_video_is_ignored_when_the_switch_is_off(api, db, procs, fx, monkeypatch):
    arm(monkeypatch, "dlq")
    job_id = submit_ok(api)
    drain_outbox()
    start_worker(procs, fx, FAKE_YT_WITH_FIXTURES="1", DEMO_FAULTS_ENABLED="false")
    assert wait_for_status(db, job_id, TERMINAL, timeout=60)["status"] == "COMPLETED"
    assert calls(fx, "info") and injected(procs) == 0
    assert redis.Redis.from_url(REDIS_URL).exists(video_key(VIDEO_ID))   # never read, never claimed


def test_second_job_for_the_same_video_does_not_inherit_the_fault(api, db, procs, fx, monkeypatch):
    arm(monkeypatch, "dlq")
    first = submit_ok(api)
    drain_outbox()
    start_worker(procs, fx, FAKE_YT_WITH_FIXTURES="1", MAX_ATTEMPTS=1, **ON)
    assert wait_for_status(db, first, TERMINAL, timeout=60)["status"] == "DEAD_LETTERED"
    second = submit_ok(api)
    drain_outbox()
    assert wait_for_status(db, second, TERMINAL, timeout=60)["status"] == "COMPLETED"
    assert injected(procs) == 1


def test_d1_private_video_still_fails_with_the_switch_on(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()
    start_worker(procs, fx, FAKE_YT="private", **ON)
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert (row["status"], row["error"]["code"], row["attempts"]) == ("FAILED", "YOUTUBE_VIDEO_UNAVAILABLE", 0)
    assert injected(procs) == 0
