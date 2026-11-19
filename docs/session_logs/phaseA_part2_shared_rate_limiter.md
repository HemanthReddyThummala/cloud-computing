# Session log — Phase A, Part 2: shared external-API rate limiter

- **Started:** 2026-11-20 01:09 (host clock)
- **Branch:** `phase-2`; HEAD = `main` = `phase1-baseline` = `49eed18`; 0 staged; 50 status entries
  (all earlier Phase 2 / Phase A work uncommitted and preserved).
- **Objective:** one rate limiter shared by all worker processes through the existing Redis:
  - Gemini: RPM 4, input TPM 200,000, RPD 16 (user's AI Studio values × 0.8; reset at midnight Pacific).
  - Notion: 2.5 requests/s per connection.
  - YouTube: no invented quota, only a shared 429 cooldown.
  - A shared, Retry-After-honouring cooldown for all providers.
  - Fail closed (`RateLimiterUnavailable`, infra budget) when Redis is down.
  - Retries reuse the existing outbox retry system.
- **Out of scope:** YouTube/Gemini/FFmpeg/Notion implementations, the extension, new services or
  dependencies, model changes, the user's `.env`.

## 1. Inspection (reused, not redesigned)
- **Error classes** (`common/errors.py`) already had `RateLimited(retry_after_sec)`,
  `QuotaExhausted(resets_at)` and `RateLimiterUnavailable` (infrastructure budget).
- **The classifier** (`common/http_errors.py`) already maps provider 429s to `<P>_RATE_LIMITED` /
  `GEMINI_QUOTA_EXHAUSTED` with a capped Retry-After, and has `PACIFIC` and `next_pacific_midnight()`.
- **The retry policy** (ADR-11) already turns these into outbox retries:
  `delay = max(step, retry_after)`, and the infrastructure budget for `RateLimiterUnavailable`.
- **The worker** already owns a Redis client (heartbeats).
- **The image** has timezone data (`America/Los_Angeles` verified in `notes-agent-backend:dev`).
- **`.env`** has empty `GEMINI_*_LIMIT` values; `.env.example` had an unused
  `GEMINI_LIMIT_SAFETY_FACTOR` and `LLM_MAX_WAIT_SEC`.

## 2. Files changed and reasons
| File | Change |
|---|---|
| `backend/notesagent/common/ratelimit.py` (new) | `RateLimiter`: atomic Lua scripts on Redis time. Gemini uses sliding-window RPM and input TPM plus RPD per Pacific day; Notion uses spacing per connection; YouTube has cooldown only; a shared cooldown (`report`); `settle` for the token correction; fail-closed `RateLimiterUnavailable` |
| `backend/notesagent/common/config.py` | `gemini_rpm_limit` 4, `gemini_tpm_limit` 200000, `gemini_rpd_limit` 16, `notion_rps_limit` 2.5, `rate_limit_max_wait_sec` 60, `rate_limit_default_cooldown_sec` 10 (all validated) |
| `backend/notesagent/worker/stages.py` | `StageContext.limiter` |
| `backend/notesagent/worker/runner.py` | creates the limiter on the existing Redis client; passes it to stages; in `_handle_stage_failure` calls `limiter.report(error)` before the existing retry plan |
| `docker-compose.yml` | worker: the six limiter variables with `:-` defaults (so the empty values in `.env` fall back) |
| `.env.example` | effective Gemini limits (4 / 200000 / 16), `RATE_LIMIT_MAX_WAIT_SEC`, `RATE_LIMIT_DEFAULT_COOLDOWN_SEC`; removed the unused `GEMINI_LIMIT_SAFETY_FACTOR` (it would apply the 0.8 margin twice) and `LLM_MAX_WAIT_SEC` (replaced) |
| `docs/decisions.md` | new ADR-13; ADR-06 limiter line points to it |
| `backend/tests/test_ratelimit.py` (new) | 25 tests (see §3) |
| `backend/tests/support/limiter_client.py` (new) | independent limiter process for the multi-process tests |
| `backend/tests/support/fixture_stages.py` | `FIXTURE_LIMIT_STAGE` (permit before a simulated call, `call.*` marker); `rate_limited:<sec>:<PROVIDER>` |

The user's `.env` was not edited. No new dependency or service was added.

## 3. Tests and results
Test image rebuilt: `docker compose --profile test build tests`.

`MSYS_NO_PATHCONV=1 docker compose --profile test run --rm --no-deps tests pytest -p no:warnings -q -s tests/test_ratelimit.py`
→ **25 passed in 32.36 s** (01:14:10–01:14:46). Printed evidence:
- `RPM: 12 grants over 4048 ms, max 4 per 2 s window` (3 processes × 4 requests, limit 4)
- `TPM: max 900 tokens per 2 s window (limit 1000)` (3 processes × 3 × 300 tokens)
- `Notion: 12 calls, minimum gap 404 ms, span 4566 ms` (3 processes × 4, 2.5/s)

| Acceptance criterion | Test(s) |
|---|---|
| 1 Gemini RPM shared across processes | `test_gemini_rpm_is_shared_by_independent_processes` |
| 2 input TPM across workers | `test_gemini_input_tpm_is_shared_by_independent_processes`, `test_settle_corrects_the_token_estimate`, `test_request_larger_than_tpm_is_fatal` |
| 3 RPD + Pacific reset | `test_gemini_rpd_is_shared_by_independent_processes` (exactly 5 of 9, then `GEMINI_DAILY_LIMIT_REACHED`), `test_rpd_resets_at_midnight_pacific`, `test_pacific_day_and_reset_are_dst_correct` (×5 incl. DST start/end) |
| 4 Notion shared | `test_notion_rate_is_shared_by_independent_processes`, `test_notion_connections_are_limited_separately` |
| 5 429 cooldown shared | `test_provider_429_in_one_worker_cools_down_the_others` (real worker + independent process), `test_youtube_has_no_invented_quota_only_the_shared_cooldown` |
| 6 Retry-After honoured | same worker test (cooldown TTL ≤ 3 s, retry delay ≥ 3 s), `test_retry_after_sets_the_cooldown_length` (Retry-After, default cooldown, quota reset, never shortened, local errors not reported) |
| 7 Redis down fails closed | `test_redis_down_raises_rate_limiter_unavailable_and_never_calls_the_provider`, `test_worker_with_redis_down_takes_the_infrastructure_retry_path` (infra_attempts 1, attempts 0, no `call.*` marker) |
| 8 concurrency cannot bypass | `test_concurrent_requests_cannot_bypass_the_limits` (40 concurrent → exactly 4; 12 × 300 tokens → exactly 3; 20 Notion → exactly 1) |
| 9 existing behaviour intact | full suite (§4) |
| configuration | `test_default_limits_match_the_agreed_values`, `test_invalid_limits_are_rejected` (×4), `test_unique_redis_keys_per_pacific_day` |

## 4. Full suite
`MSYS_NO_PATHCONV=1 docker compose --profile test run --rm --no-deps tests pytest -p no:warnings -q -rfE --tb=short`
(01:15:06–01:26:08) → **653 passed in 658.35 s**, 0 failed, 0 errors. Previous baseline: 628; the 25
new tests account for the difference. Existing retry, outbox, worker, dead-letter and API tests pass
unchanged. Afterwards RabbitMQ/PostgreSQL/Redis were healthy with 0 restarts, and no new diagnostics
files were written. The dev stack was **not** redeployed in this task.

## 5. Unresolved issues / notes
1. **No real provider calls exist yet.** The limiter is wired into the worker and `StageContext` and is
   proven with fixture stages and independent processes. Phase B stages must call
   `ctx.limiter.acquire(...)` before every YouTube/Gemini/Notion request and raise classified errors
   (ADR-10) so that 429s reach `report()`.
2. A Gemini request is counted toward RPD when the permit is granted, even if the call then fails
   (conservative). The input-token estimate must come from the Phase B stage; `settle()` corrects it.
3. Waiting inside `acquire` holds the worker's job slot for up to `RATE_LIMIT_MAX_WAIT_SEC` (60 s);
   longer waits are re-queued and count as a provider attempt (`MAX_ATTEMPTS`).
4. **Notion:**
   - The cooldown is global for the provider: conservative, because the workspace limit is unknown.
   - The spacing is per connection key, which Phase B must derive from a hash of the token.
5. Gemini limits are per Google project; any other application using the same project shares the
   quota without this limiter knowing.
6. The dev stack still runs the image from before this task. Redeploying needs a separate
   instruction.
7. **Git index changed during this session by someone other than me:** 65 paths are now staged (36
   added, 24 modified, 2 deleted, …), while HEAD is still `49eed18`. Files created or edited after
   that staging are unstaged or partially staged:
   - `MM` `backend/tests/support/fixture_stages.py`, `MM` `docs/decisions.md`, `AM` this log;
   - `??` `backend/tests/support/limiter_client.py`, `??` `backend/tests/test_ratelimit.py`.

   The index was **not** modified by me.

## 6. Final Git status
HEAD = `main` = `phase1-baseline` = `49eed18` (nothing committed). The index is as described in §5
item 7, and the working tree contains all of this task's changes. No staging, commit, push or identity
change was made by the assistant.
