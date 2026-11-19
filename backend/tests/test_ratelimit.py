"""ADR-13 shared rate limiter: real Redis (db 15), several independent processes, and the worker's
existing retry path. Gemini windows are shortened (LIMITER_WINDOW_SEC / gemini_window_sec) so tests
run in seconds; the algorithm is the same as with the production 60-second window."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
import redis
import redis.asyncio as aioredis

from notesagent.common.config import get_settings
from notesagent.common.errors import (
    FatalStageError,
    Provider,
    QuotaExhausted,
    RateLimited,
    RateLimiterUnavailable,
    RetryBudget,
)
from notesagent.common.http_errors import next_pacific_midnight
from notesagent.common.ratelimit import RateLimiter, pacific_day
from tests.support.helpers import drain_outbox, job, submit_ok, wait_until

FIXTURE = "tests.support.run_fixture_worker"
REDIS_URL = os.environ["TEST_REDIS_URL"]


# ---------------------------------------------------------------- helpers
def run(coro):
    return asyncio.run(coro)


async def _with_limiter(fn, *, window=60.0, clock=None, url=REDIS_URL, **overrides):
    client = aioredis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    try:
        return await fn(RateLimiter(client, get_settings(**overrides), gemini_window_sec=window, clock=clock))
    finally:
        await client.aclose()


def with_limiter(fn, **kwargs):
    return run(_with_limiter(fn, **kwargs))


def run_clients(procs, tmp_path, provider, *, processes=3, count=4, tokens=0, timeout=90, **env):
    """Start independent limiter processes, release them together, return all their records."""
    go = tmp_path / "go"
    outs = [tmp_path / f"client{i}.jsonl" for i in range(processes)]
    for i, out in enumerate(outs):
        procs.start(f"client-{i}", "tests.support.limiter_client", out, go, provider, count, tokens,
                    **{k: str(v) for k, v in env.items()})
    wait_until(lambda: all((out.parent / f"{out.name}.ready").exists() for out in outs), timeout=30,
               what="clients ready")
    go.write_text("")
    wait_until(lambda: all(procs.procs[f"client-{i}"].poll() is not None for i in range(processes)),
               timeout=timeout, what="clients finished")
    assert all(procs.procs[f"client-{i}"].returncode == 0 for i in range(processes)), procs.all_logs()
    records = [json.loads(line) for out in outs for line in out.read_text().splitlines()]
    assert len(records) == processes * count
    return records


def max_in_window(times_ms, window_ms):
    """Largest number of grants inside any window (t - window, t] ending at a grant."""
    times = sorted(times_ms)
    return max(sum(1 for s in times if t - window_ms < s <= t) for t in times)


def max_tokens_in_window(grants, window_ms):
    return max(sum(g["tokens"] for g in grants if t["at_ms"] - window_ms < g["at_ms"] <= t["at_ms"]) for t in grants)


# ===================================================================== 1. Gemini RPM across processes
def test_gemini_rpm_is_shared_by_independent_processes(procs, tmp_path):
    records = run_clients(procs, tmp_path, "GEMINI", processes=3, count=4,
                          GEMINI_RPM_LIMIT=4, LIMITER_WINDOW_SEC=2)
    grants = [r["at_ms"] for r in records if r["ok"]]
    assert len(grants) == 12                                         # all served by waiting
    assert max_in_window(grants, 2000) == 4                          # never more than 4 per window
    assert max(grants) - min(grants) >= 4000                         # 12 requests need 3 windows
    print(f"\nEVIDENCE RPM: 12 grants over {max(grants) - min(grants)} ms, max 4 per 2 s window")


# ===================================================================== 2. input TPM across processes
def test_gemini_input_tpm_is_shared_by_independent_processes(procs, tmp_path):
    records = run_clients(procs, tmp_path, "GEMINI", processes=3, count=3, tokens=300,
                          GEMINI_RPM_LIMIT=100, GEMINI_TPM_LIMIT=1000, LIMITER_WINDOW_SEC=2)
    grants = [r for r in records if r["ok"]]
    assert len(grants) == 9
    assert max_tokens_in_window(grants, 2000) <= 1000                # 3 x 300 fit, a 4th would not
    span = max(g["at_ms"] for g in grants) - min(g["at_ms"] for g in grants)
    assert span >= 4000
    print(f"\nEVIDENCE TPM: max {max_tokens_in_window(grants, 2000)} tokens per 2 s window (limit 1000)")


def test_settle_corrects_the_token_estimate():
    async def scenario(limiter):
        permit = await limiter.acquire(Provider.GEMINI, tokens=300, max_wait_sec=0)
        await limiter.settle(permit, 900)                            # the real request was bigger
        with pytest.raises(RateLimited) as err:
            await limiter.acquire(Provider.GEMINI, tokens=200, max_wait_sec=0)
        assert err.value.code == "GEMINI_LOCAL_RATE_LIMIT" and 0 < err.value.retry_after_sec <= 2
        assert await limiter.acquire(Provider.GEMINI, tokens=100, max_wait_sec=0)   # 900 + 100 fits
    with_limiter(scenario, window=2, gemini_rpm_limit=100, gemini_tpm_limit=1000)


def test_request_larger_than_tpm_is_fatal():
    async def scenario(limiter):
        with pytest.raises(FatalStageError) as err:
            await limiter.acquire(Provider.GEMINI, tokens=1001)
        assert err.value.code == "LLM_REQUEST_TOO_LARGE"
    with_limiter(scenario, gemini_tpm_limit=1000)


# ===================================================================== 3. RPD and the Pacific reset
def test_gemini_rpd_is_shared_by_independent_processes(procs, tmp_path):
    records = run_clients(procs, tmp_path, "GEMINI", processes=3, count=3,
                          GEMINI_RPM_LIMIT=100, GEMINI_RPD_LIMIT=5)
    assert sum(r["ok"] for r in records) == 5
    assert [r["error"] for r in records if not r["ok"]] == ["GEMINI_DAILY_LIMIT_REACHED"] * 4


def test_rpd_resets_at_midnight_pacific():
    now = {"t": datetime(2026, 11, 20, 7, 59, tzinfo=timezone.utc)}          # 23:59 PST on Nov 19

    async def scenario(limiter):
        for _ in range(2):
            await limiter.acquire(Provider.GEMINI, max_wait_sec=0)
        with pytest.raises(QuotaExhausted) as err:
            await limiter.acquire(Provider.GEMINI, max_wait_sec=0)
        assert err.value.code == "GEMINI_DAILY_LIMIT_REACHED"
        assert err.value.resets_at == datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)   # 00:00 PST
        now["t"] = datetime(2026, 11, 20, 8, 0, 1, tzinfo=timezone.utc)      # 00:00:01 PST on Nov 20
        assert await limiter.acquire(Provider.GEMINI, max_wait_sec=0)       # new Pacific day
    with_limiter(scenario, clock=lambda: now["t"], gemini_rpm_limit=100, gemini_rpd_limit=2)


@pytest.mark.parametrize("utc,day,reset", [
    ("2026-11-20T07:59:59+00:00", "2026-11-19", "2026-11-20T08:00:00+00:00"),   # PST (UTC-8)
    ("2026-11-20T08:00:00+00:00", "2026-11-20", "2026-11-21T08:00:00+00:00"),
    ("2026-07-01T06:59:59+00:00", "2026-06-30", "2026-07-01T07:00:00+00:00"),   # PDT (UTC-7)
    ("2026-03-08T08:30:00+00:00", "2026-03-08", "2026-03-09T07:00:00+00:00"),   # DST starts that day
    ("2026-11-01T07:30:00+00:00", "2026-11-01", "2026-11-02T08:00:00+00:00"),   # DST ends that day
])
def test_pacific_day_and_reset_are_dst_correct(utc, day, reset):
    now = datetime.fromisoformat(utc)
    assert pacific_day(now) == day
    assert next_pacific_midnight(now) == datetime.fromisoformat(reset)


# ===================================================================== 4. Notion across processes
def test_notion_rate_is_shared_by_independent_processes(procs, tmp_path):
    records = run_clients(procs, tmp_path, "NOTION", processes=3, count=4, NOTION_RPS_LIMIT=2.5)
    grants = sorted(r["at_ms"] for r in records if r["ok"])
    assert len(grants) == 12
    gaps = [b - a for a, b in zip(grants, grants[1:])]
    assert min(gaps) >= 400                                           # 2.5/s = one call per 400 ms
    print(f"\nEVIDENCE Notion: 12 calls, minimum gap {min(gaps)} ms, span {grants[-1] - grants[0]} ms")


def test_notion_connections_are_limited_separately():
    async def scenario(limiter):
        await limiter.acquire(Provider.NOTION, connection="a", max_wait_sec=0)
        await limiter.acquire(Provider.NOTION, connection="b", max_wait_sec=0)      # other connection
        with pytest.raises(RateLimited) as err:
            await limiter.acquire(Provider.NOTION, connection="a", max_wait_sec=0)
        assert err.value.code == "NOTION_LOCAL_RATE_LIMIT"
    with_limiter(scenario)


# ===================================================================== 5 + 6. shared 429 cooldown, Retry-After
def test_provider_429_in_one_worker_cools_down_the_others(api, db, procs, fx, tmp_path):
    """A worker's stage gets a Gemini 429 with Retry-After 3 s: the worker's existing failure path
    records a shared cooldown; an independent process then cannot call Gemini before it ends."""
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    procs.start("worker-a", FIXTURE, FIXTURE_DIR=fx, FIXTURE_RAISE="TRANSCRIBING", FIXTURE_RAISE_TIMES=1,
                FIXTURE_RAISE_KIND="rate_limited:3:GEMINI")
    retry = wait_until(lambda: job(db, job_id)["retry"], timeout=30, what="retry scheduled")
    r = redis.Redis.from_url(REDIS_URL)
    until_ms, ttl_ms = int(r.get("ratelimit:cooldown:gemini")), r.pttl("ratelimit:cooldown:gemini")
    assert 1500 < ttl_ms <= 3000                                      # Retry-After 3 s honoured
    assert retry["reason_code"] == "GEMINI_RATE_LIMITED" and retry["delay_sec"] >= 3   # existing retry path
    procs.wait_for_log("worker-a", "provider cooldown shared with all workers")

    records = run_clients(procs, tmp_path, "GEMINI", processes=1, count=1, LIMITER_MAX_WAIT=10)
    assert records[0]["ok"] and records[0]["at_ms"] >= until_ms       # waited for the cooldown


def test_retry_after_sets_the_cooldown_length():
    async def scenario(limiter):
        r = limiter.redis
        assert await limiter.report(RateLimited("GEMINI_RATE_LIMITED", "x", provider=Provider.GEMINI,
                                                retry_after_sec=2.5)) == 2.5
        assert 2000 < await r.pttl("ratelimit:cooldown:gemini") <= 2500
        with pytest.raises(RateLimited) as err:
            await limiter.acquire(Provider.GEMINI, max_wait_sec=0)
        assert err.value.code == "GEMINI_COOLDOWN_ACTIVE" and 2.0 < err.value.retry_after_sec <= 2.5

        # a shorter later 429 never shortens the cooldown
        await limiter.report(RateLimited("GEMINI_RATE_LIMITED", "x", provider=Provider.GEMINI, retry_after_sec=0.5))
        assert await r.pttl("ratelimit:cooldown:gemini") > 2000

        # no Retry-After -> RATE_LIMIT_DEFAULT_COOLDOWN_SEC
        assert await limiter.report(RateLimited("NOTION_RATE_LIMITED", "x", provider=Provider.NOTION)) == 7
        assert 6000 < await r.pttl("ratelimit:cooldown:notion") <= 7000

        # exhausted provider quota -> until its reset time
        resets = limiter.clock() + timedelta(seconds=30)
        await limiter.report(QuotaExhausted("GEMINI_QUOTA_EXHAUSTED", "x", provider=Provider.GEMINI, resets_at=resets))
        assert 28000 < await r.pttl("ratelimit:cooldown:gemini") <= 30000

        # the limiter's own errors are not provider 429s
        assert await limiter.report(RateLimited("YOUTUBE_COOLDOWN_ACTIVE", "x", provider=Provider.YOUTUBE,
                                                retry_after_sec=5)) is None
        assert await r.exists("ratelimit:cooldown:youtube") == 0
    with_limiter(scenario, rate_limit_default_cooldown_sec=7)


def test_youtube_has_no_invented_quota_only_the_shared_cooldown():
    async def scenario(limiter):
        for _ in range(50):                                            # no numeric limit
            await limiter.acquire(Provider.YOUTUBE, max_wait_sec=0)
        await limiter.report(RateLimited("YOUTUBE_RATE_LIMITED", "x", provider=Provider.YOUTUBE, retry_after_sec=1))
        with pytest.raises(RateLimited) as err:
            await limiter.acquire(Provider.YOUTUBE, max_wait_sec=0)
        assert err.value.code == "YOUTUBE_COOLDOWN_ACTIVE"
        start = asyncio.get_running_loop().time()
        await limiter.acquire(Provider.YOUTUBE, max_wait_sec=5)        # waits the cooldown out
        assert asyncio.get_running_loop().time() - start >= 0.9
    with_limiter(scenario)


# ===================================================================== 7. Redis unavailable -> fail closed
def test_redis_down_raises_rate_limiter_unavailable_and_never_calls_the_provider():
    calls = []

    async def scenario(limiter):
        for provider in (Provider.GEMINI, Provider.NOTION, Provider.YOUTUBE):
            with pytest.raises(RateLimiterUnavailable) as err:
                await limiter.acquire(provider)
                calls.append(provider)                                  # the "provider call"
            assert err.value.code == "RATE_LIMITER_UNAVAILABLE"
            assert err.value.budget is RetryBudget.INFRASTRUCTURE
        assert await limiter.report(RateLimited("GEMINI_RATE_LIMITED", "x", provider=Provider.GEMINI)) is None
    with_limiter(scenario, url="redis://127.0.0.1:1/0")
    assert calls == []


def test_worker_with_redis_down_takes_the_infrastructure_retry_path(api, db, procs, fx):
    job_id = submit_ok(api)
    drain_outbox()
    procs.start("worker-a", FIXTURE, FIXTURE_DIR=fx, FIXTURE_LIMIT_STAGE="TRANSCRIBING",
                REDIS_URL="redis://redis:1/15")                         # nothing listens there
    wait_until(lambda: job(db, job_id)["retry"], timeout=30, what="infrastructure retry scheduled")
    row = job(db, job_id)
    assert row["retry"]["reason_code"] == "RATE_LIMITER_UNAVAILABLE" and row["retry"]["budget"] == "infrastructure"
    assert (row["attempts"], row["infra_attempts"]) == (0, 1)          # provider budget untouched
    assert row["status"] == "TRANSCRIBING"                              # waiting, not failed
    assert list(fx.glob("call.*")) == []                                # provider never called


# ===================================================================== 8. concurrency cannot bypass
def test_concurrent_requests_cannot_bypass_the_limits():
    async def scenario():
        settings = get_settings(gemini_rpm_limit=4, gemini_tpm_limit=1000)
        clients = [aioredis.from_url(REDIS_URL) for _ in range(4)]
        limiters = [RateLimiter(c, settings, gemini_window_sec=30) for c in clients]
        try:
            async def attempt(limiter, provider, tokens=0):
                try:
                    await limiter.acquire(provider, tokens=tokens, max_wait_sec=0)
                    return True
                except RateLimited:
                    return False
            rpm = await asyncio.gather(*(attempt(limiters[i % 4], Provider.GEMINI) for i in range(40)))
            assert sum(rpm) == 4
            await clients[0].delete("ratelimit:gemini:rpm", "ratelimit:gemini:tpm")
            tpm = await asyncio.gather(*(attempt(limiters[i % 4], Provider.GEMINI, 300) for i in range(12)))
            assert sum(tpm) == 3                                        # 3 x 300 <= 1000 < 4 x 300
            notion = await asyncio.gather(*(attempt(limiters[i % 4], Provider.NOTION) for i in range(20)))
            assert sum(notion) == 1                                     # strict spacing
        finally:
            for c in clients:
                await c.aclose()
    run(scenario())


# ===================================================================== configuration
def test_default_limits_match_the_agreed_values():
    s = get_settings()
    assert (s.gemini_rpm_limit, s.gemini_tpm_limit, s.gemini_rpd_limit) == (4, 200_000, 16)
    assert (s.notion_rps_limit, s.rate_limit_max_wait_sec, s.rate_limit_default_cooldown_sec) == (2.5, 60.0, 10.0)


@pytest.mark.parametrize("override", [{"gemini_rpm_limit": 0}, {"notion_rps_limit": 0},
                                      {"gemini_tpm_limit": -1}, {"rate_limit_default_cooldown_sec": 0}])
def test_invalid_limits_are_rejected(override):
    with pytest.raises(SystemExit):
        get_settings(**override)


def test_unique_redis_keys_per_pacific_day():
    keys = RateLimiter(redis.Redis(), get_settings())._gemini_keys(datetime(2026, 11, 20, 12, tzinfo=timezone.utc))
    assert keys == ["ratelimit:cooldown:gemini", "ratelimit:gemini:rpm", "ratelimit:gemini:tpm",
                    "ratelimit:gemini:rpd:2026-11-20"]
