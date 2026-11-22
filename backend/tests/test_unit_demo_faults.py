"""ADR-22 demo fault switch — unit tests. Bindings use the REAL test Redis (db 15) so GETDEL atomicity is
exercised; injected exceptions go through the REAL production classifiers."""
import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
import redis
import redis.asyncio as aioredis

from notesagent.common.config import get_settings
from notesagent.common.errors import RateLimited, TransientError
from notesagent.ops import demo_faults as cli
from notesagent.worker.demo_faults import (ARM_TTL_SEC, DemoFaults, Scenario, frame_fails, gemini_429_error,
                                           job_key, video_key, youtube_network_error)
from notesagent.worker.gemini import classify_gemini_error
from notesagent.worker.stages import StageContext
from notesagent.worker.youtube import classify_metadata_error

REDIS_URL = os.environ["TEST_REDIS_URL"]


@pytest.fixture(autouse=True)
def clean_demo_keys():
    r = redis.Redis.from_url(REDIS_URL)
    for k in r.scan_iter("demo:fault:*"):
        r.delete(k)
    yield
    for k in r.scan_iter("demo:fault:*"):
        r.delete(k)


def ctx(video="dQw4w9WgXcQ", attempts=0):
    return SimpleNamespace(job_id=uuid.uuid4(), job={"video_id": video, "attempts": attempts})


def run(coro_fn):
    async def wrapper():
        client = aioredis.from_url(REDIS_URL)
        try:
            return await coro_fn(DemoFaults(client))
        finally:
            await client.aclose()
    return asyncio.run(wrapper())


def arm(video, scenario):
    redis.Redis.from_url(REDIS_URL).set(video_key(video), scenario, ex=ARM_TTL_SEC)


# ===================================================================== defaults / disabled
def test_disabled_by_default():
    assert get_settings().demo_faults_enabled is False
    assert StageContext.__dataclass_fields__["demo_faults"].default is None


# ===================================================================== injected errors = real classification
def test_injected_errors_take_the_production_classification_paths():
    yt = classify_metadata_error(youtube_network_error())
    assert isinstance(yt, TransientError) and yt.code == "YOUTUBE_UNREACHABLE"       # retryable, not D-1 FAILED
    gm = classify_gemini_error(gemini_429_error())
    assert isinstance(gm, RateLimited) and gm.code == "GEMINI_RATE_LIMITED" and gm.retry_after_sec == 20


def test_frame_selection():
    assert [frame_fails(Scenario.FRAMES_PARTIAL, i) for i in range(5)] == [False, True, False, True, False]
    assert all(frame_fails(Scenario.FRAMES_ALL, i) for i in range(5))
    assert not any(frame_fails(s, i) for s in (None, Scenario.DLQ, Scenario.GEMINI_429_ONCE) for i in range(5))


# ===================================================================== binding
def test_binding_is_one_shot_and_only_for_the_armed_video():
    arm("dQw4w9WgXcQ", Scenario.DLQ)

    async def go(f):
        a, b, other = ctx(), ctx(), ctx(video="jNQXAC9IVRw")
        assert await f.bind(other) is None                     # other video: nothing
        assert await f.bind(a) == Scenario.DLQ                 # first job claims it
        assert await f.bind(b) is None                         # second job of the same video: nothing
        assert await f.bind(a) == Scenario.DLQ                 # retries of the bound job keep it
        assert await f.scenario(a.job_id) == Scenario.DLQ and await f.scenario(b.job_id) is None
        return a
    a = run(go)
    r = redis.Redis.from_url(REDIS_URL)
    assert not r.exists(video_key("dQw4w9WgXcQ")) and 0 < r.ttl(job_key(a.job_id)) <= 7200


def test_concurrent_claims_bind_exactly_one_job():
    arm("dQw4w9WgXcQ", Scenario.FRAMES_ALL)

    async def go(f):
        results = await asyncio.gather(*(f.bind(ctx()) for _ in range(10)))
        return [r for r in results if r]
    assert run(go) == [Scenario.FRAMES_ALL]


def test_unknown_scenario_values_are_ignored():
    r = redis.Redis.from_url(REDIS_URL)
    r.set(video_key("dQw4w9WgXcQ"), "something_else", ex=60)
    assert run(lambda f: f.bind(ctx())) is None


# ===================================================================== scenario hooks
def test_dlq_fails_metadata_on_every_attempt():
    arm("dQw4w9WgXcQ", Scenario.DLQ)

    async def go(f):
        c = ctx()
        await f.bind(c)
        for attempt in range(3):
            c.job["attempts"] = attempt
            with pytest.raises(Exception) as caught:
                await f.before_youtube_metadata(c)
            assert classify_metadata_error(caught.value).code == "YOUTUBE_UNREACHABLE"
        await f.before_gemini_request(c)                       # other hooks untouched
    run(go)


def test_gemini_429_is_injected_exactly_once_per_job():
    arm("dQw4w9WgXcQ", Scenario.GEMINI_429_ONCE)

    async def go(f):
        c = ctx()
        await f.bind(c)
        await f.before_youtube_metadata(c)                     # no YouTube fault
        with pytest.raises(Exception) as caught:
            await f.before_gemini_request(c)
        assert classify_gemini_error(caught.value).code == "GEMINI_RATE_LIMITED"
        for attempt in (0, 1, 2):                              # never again, whatever the attempt counter says
            c.job["attempts"] = attempt
            await f.before_gemini_request(c)
    run(go)


def test_unbound_jobs_get_no_fault():
    async def go(f):
        c = ctx()
        assert await f.bind(c) is None
        await f.before_youtube_metadata(c)
        await f.before_gemini_request(c)
        assert not f.frame_injected(c, await f.scenario(c.job_id), 1)
    run(go)


# ===================================================================== fail-safe
class BrokenRedis:
    async def get(self, *a, **k):
        raise ConnectionError("redis down")
    getdel = set = get


def test_redis_errors_never_inject_a_fault():
    async def go():
        f, c = DemoFaults(BrokenRedis()), ctx()
        assert await f.bind(c) is None and await f.scenario(c.job_id) is None
        await f.before_youtube_metadata(c)
        await f.before_gemini_request(c)
    asyncio.run(go())


def test_gemini_marker_failure_means_no_injection():
    class MarkerFails:
        async def get(self, key):
            return Scenario.GEMINI_429_ONCE.value.encode()

        async def set(self, *a, **k):
            raise ConnectionError("down")
    asyncio.run(DemoFaults(MarkerFails()).before_gemini_request(ctx()))      # no exception


# ===================================================================== CLI
def test_cli_refuses_to_arm_when_disabled(monkeypatch, capsys):
    monkeypatch.delenv("DEMO_FAULTS_ENABLED", raising=False)
    monkeypatch.setattr(cli, "_client", lambda s: pytest.fail("must not touch Redis when disabled"))
    assert cli.main(["arm", "dlq", "--video", "dQw4w9WgXcQ"]) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_cli_validates_input(monkeypatch, capsys):
    monkeypatch.setenv("DEMO_FAULTS_ENABLED", "true")
    assert cli.main(["arm", "dlq", "--video", "not-an-id"]) == 2
    with pytest.raises(SystemExit):
        cli.main(["arm", "everything_fails", "--video", "dQw4w9WgXcQ"])


def test_cli_arm_status_disarm(monkeypatch, capsys):
    monkeypatch.setenv("DEMO_FAULTS_ENABLED", "true")
    assert cli.main(["arm", "frames_partial", "--video", "dQw4w9WgXcQ"]) == 0
    r = redis.Redis.from_url(REDIS_URL)
    assert r.get(video_key("dQw4w9WgXcQ")) == b"frames_partial" and 0 < r.ttl(video_key("dQw4w9WgXcQ")) <= 900
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "demo:fault:video:dQw4w9WgXcQ = frames_partial" in out and "redis://" not in out
    assert cli.main(["disarm", "--video", "dQw4w9WgXcQ"]) == 0
    assert not r.exists(video_key("dQw4w9WgXcQ"))
