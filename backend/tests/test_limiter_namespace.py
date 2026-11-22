"""ADR-23: per-Google-Cloud-project Gemini limiter namespace (GEMINI_LIMITER_NAMESPACE).

Real Redis (the test database, flushed before each test by ``clean_state``). Proves:
- the default (empty) namespace keeps every existing key name byte-for-byte;
- a namespace gives Gemini its own RPM / TPM / RPD counters and its own cooldown;
- Notion and YouTube keys are never namespaced;
- coordination stays atomic and shared across independent processes inside one namespace;
- invalid values are rejected.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
import redis

from notesagent.common.config import get_settings
from notesagent.common.errors import Provider, QuotaExhausted, RateLimited
from notesagent.common.ratelimit import RateLimiter, pacific_day
from tests.test_ratelimit import run_clients, with_limiter

REDIS_URL = os.environ["TEST_REDIS_URL"]
DAY = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
ORIGINAL = ["ratelimit:cooldown:gemini", "ratelimit:gemini:rpm", "ratelimit:gemini:tpm",
            "ratelimit:gemini:rpd:2026-11-20"]


def limiter_for(**overrides) -> RateLimiter:
    return RateLimiter(redis.Redis(), get_settings(**overrides))      # key names only; no Redis call


# ===================================================================== default compatibility
@pytest.mark.parametrize("value", [None, "", "   "])
def test_default_namespace_keeps_every_key_name_byte_for_byte(value, monkeypatch):
    if value is None:
        monkeypatch.delenv("GEMINI_LIMITER_NAMESPACE", raising=False)
        limiter = limiter_for()
    else:
        limiter = limiter_for(gemini_limiter_namespace=value)
    assert limiter.settings.gemini_limiter_namespace == ""
    assert limiter._gemini_keys(DAY) == ORIGINAL
    assert [limiter.cooldown_key(p) for p in Provider if p in (Provider.GEMINI, Provider.NOTION, Provider.YOUTUBE)] \
        == ["ratelimit:cooldown:gemini", "ratelimit:cooldown:notion", "ratelimit:cooldown:youtube"]


def test_default_namespace_writes_the_original_keys_in_redis():
    async def scenario(limiter):
        permit = await limiter.acquire(Provider.GEMINI, tokens=100, max_wait_sec=0)
        await limiter.settle(permit, 150)
        await limiter.report(RateLimited("GEMINI_RATE_LIMITED", "x", provider=Provider.GEMINI, retry_after_sec=5))
        r = limiter.redis
        names = sorted(k.decode() for k in await r.keys("ratelimit:*"))
        assert names == sorted(["ratelimit:cooldown:gemini", "ratelimit:gemini:rpm", "ratelimit:gemini:tpm",
                                f"ratelimit:gemini:rpd:{pacific_day(limiter.clock())}"])
        assert int(await r.get(f"ratelimit:gemini:rpd:{pacific_day(limiter.clock())}")) == 1
        assert [m.decode() for m in await r.zrange("ratelimit:gemini:tpm", 0, -1)] == [f"{permit.reservation}:150"]
    with_limiter(scenario)


# ===================================================================== namespace isolation
def test_namespace_renames_only_the_four_gemini_keys():
    limiter = limiter_for(gemini_limiter_namespace="alt")
    assert limiter._gemini_keys(DAY) == ["ratelimit:cooldown:gemini-alt", "ratelimit:gemini-alt:rpm",
                                         "ratelimit:gemini-alt:tpm", "ratelimit:gemini-alt:rpd:2026-11-20"]
    assert limiter.cooldown_key(Provider.NOTION) == "ratelimit:cooldown:notion"      # unchanged
    assert limiter.cooldown_key(Provider.YOUTUBE) == "ratelimit:cooldown:youtube"    # unchanged


def test_namespace_has_its_own_rpd_and_leaves_the_original_counter_untouched():
    day = pacific_day(datetime.now(timezone.utc))
    r = redis.Redis.from_url(REDIS_URL)
    r.set(f"ratelimit:gemini:rpd:{day}", 16)                               # the original project is used up

    async def original(limiter):
        with pytest.raises(QuotaExhausted) as err:
            await limiter.acquire(Provider.GEMINI, max_wait_sec=0)
        assert err.value.code == "GEMINI_DAILY_LIMIT_REACHED"

    async def alternate(limiter):
        for _ in range(3):
            assert await limiter.acquire(Provider.GEMINI, max_wait_sec=0)
        with pytest.raises(QuotaExhausted) as err:                          # its own limit is enforced
            await limiter.acquire(Provider.GEMINI, max_wait_sec=0)
        assert err.value.code == "GEMINI_DAILY_LIMIT_REACHED"

    with_limiter(original, gemini_rpm_limit=100, gemini_rpd_limit=16)
    with_limiter(alternate, gemini_rpm_limit=100, gemini_rpd_limit=3, gemini_limiter_namespace="alt")
    assert int(r.get(f"ratelimit:gemini:rpd:{day}")) == 16                 # never touched
    assert int(r.get(f"ratelimit:gemini-alt:rpd:{day}")) == 3
    assert r.zcard("ratelimit:gemini:rpm") == 0 and r.zcard("ratelimit:gemini-alt:rpm") == 3


def test_namespace_has_its_own_rpm_and_tpm_windows():
    async def fill_original(limiter):
        await limiter.acquire(Provider.GEMINI, tokens=1000, max_wait_sec=0)
        with pytest.raises(RateLimited) as err:                             # RPM 1 and TPM 1000 both used
            await limiter.acquire(Provider.GEMINI, tokens=1, max_wait_sec=0)
        assert err.value.code == "GEMINI_LOCAL_RATE_LIMIT"

    async def alternate(limiter):
        permit = await limiter.acquire(Provider.GEMINI, tokens=1000, max_wait_sec=0)   # full budget available
        await limiter.settle(permit, 400)                                   # settles in ITS OWN tpm window
        assert [m.decode() for m in await limiter.redis.zrange("ratelimit:gemini-alt:tpm", 0, -1)] \
            == [f"{permit.reservation}:400"]

    limits = dict(gemini_rpm_limit=1, gemini_tpm_limit=1000)
    with_limiter(fill_original, **limits)
    with_limiter(alternate, gemini_limiter_namespace="alt", **limits)
    r = redis.Redis.from_url(REDIS_URL)
    assert [m.decode().rsplit(":", 1)[1] for m in r.zrange("ratelimit:gemini:tpm", 0, -1)] == ["1000"]


# ===================================================================== cooldown isolation
def test_cooldowns_are_isolated_between_namespaces_but_notion_youtube_stay_shared():
    async def alternate_gets_429(limiter):
        await limiter.report(RateLimited("GEMINI_RATE_LIMITED", "x", provider=Provider.GEMINI, retry_after_sec=20))
        with pytest.raises(RateLimited) as err:
            await limiter.acquire(Provider.GEMINI, max_wait_sec=0)
        assert err.value.code == "GEMINI_COOLDOWN_ACTIVE"
        await limiter.report(RateLimited("NOTION_RATE_LIMITED", "x", provider=Provider.NOTION, retry_after_sec=20))

    async def original_unaffected_by_gemini_but_sees_notion(limiter):
        assert await limiter.acquire(Provider.GEMINI, max_wait_sec=0)       # no Gemini cooldown here
        with pytest.raises(RateLimited) as err:
            await limiter.acquire(Provider.NOTION, max_wait_sec=0)          # Notion is not per Gemini project
        assert err.value.code == "NOTION_COOLDOWN_ACTIVE"

    async def original_gets_quota_error(limiter):
        resets = limiter.clock() + timedelta(seconds=30)
        await limiter.report(QuotaExhausted("GEMINI_QUOTA_EXHAUSTED", "x", provider=Provider.GEMINI,
                                            resets_at=resets))

    async def alternate_unaffected(limiter):
        assert await limiter.acquire(Provider.GEMINI, max_wait_sec=0)

    with_limiter(alternate_gets_429, gemini_limiter_namespace="alt")
    r = redis.Redis.from_url(REDIS_URL)
    assert 15000 < r.pttl("ratelimit:cooldown:gemini-alt") <= 20000
    assert r.exists("ratelimit:cooldown:gemini") == 0
    assert r.exists("ratelimit:cooldown:notion") == 1 and r.exists("ratelimit:cooldown:notion-alt") == 0
    with_limiter(original_unaffected_by_gemini_but_sees_notion)

    r.delete("ratelimit:cooldown:gemini-alt", "ratelimit:cooldown:notion")
    with_limiter(original_gets_quota_error)
    assert r.pttl("ratelimit:cooldown:gemini") > 25000
    with_limiter(alternate_unaffected, gemini_limiter_namespace="alt")


# ===================================================================== shared + atomic inside a namespace
def test_namespaced_rpd_is_shared_atomically_by_independent_processes(procs, tmp_path):
    day = pacific_day(datetime.now(timezone.utc))
    redis.Redis.from_url(REDIS_URL).set(f"ratelimit:gemini:rpd:{day}", 16)     # original exhausted
    records = run_clients(procs, tmp_path, "GEMINI", processes=3, count=3, GEMINI_RPM_LIMIT=100,
                          GEMINI_RPD_LIMIT=5, GEMINI_LIMITER_NAMESPACE="alt")
    assert sum(r["ok"] for r in records) == 5                              # exactly the limit, no overshoot
    assert [r["error"] for r in records if not r["ok"]] == ["GEMINI_DAILY_LIMIT_REACHED"] * 4
    r = redis.Redis.from_url(REDIS_URL)
    assert int(r.get(f"ratelimit:gemini-alt:rpd:{day}")) == 5
    assert int(r.get(f"ratelimit:gemini:rpd:{day}")) == 16


# ===================================================================== validation
@pytest.mark.parametrize("value", ["ALT", "Alt", "alt!", "-alt", "alt-", "al t", "alt:x", "gemini:rpd", "alt_1",
                                   "a" * 33, "ä", "alt\n2", "*"])
def test_invalid_namespace_values_are_rejected(value):
    with pytest.raises(SystemExit):
        get_settings(gemini_limiter_namespace=value)


@pytest.mark.parametrize("value,expected", [("alt", "alt"), (" alt ", "alt"), ("proj-2", "proj-2"),
                                            ("a", "a"), ("a" * 32, "a" * 32), ("9", "9")])
def test_valid_namespace_values_are_accepted(value, expected):
    assert get_settings(gemini_limiter_namespace=value).gemini_limiter_namespace == expected
