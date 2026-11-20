# ADR-21 — Gemini failure handling and configuration (+ root `handover.md`)

Date: 2026-11-21 IST (2026-11-20 21:xx–22:xx UTC).

Scope (four improvements):
1. preserve Google's sanitized error status;
2. longer backoff for Gemini 503/504;
3. wire the Gemini request timeout (300 s);
4. extension wording.

Plus: create the root `handover.md` as the single source of truth.

Not done (by instruction):
- no real Gemini, YouTube or Notion call;
- no job;
- no deployment or restart;
- no `.env` change;
- no model or key change;
- no Git operations.

## 1. Initial state

- Branch `phase-2`, HEAD `00ef0f4`. `git status`: only 4 untracked session logs from the previous
  diagnosis sessions (mine). **No user code changes** to protect.
- Running stack: image `70dce0098cd2` (= `00ef0f4`), all healthy.
- **Observed during this session (read-only):** a **third real job** (`82960179-…`, video
  `_S-cBzAg0GE`, submitted by the user at 21:48 UTC, after the T1 probe):
  - TRANSCRIBING ok (213 segments);
  - SUMMARIZING: **4 × Gemini 503** (delays 10, 30, 90, 90 s);
  - the 5th attempt was refused locally by our limiter, `GEMINI_DAILY_LIMIT_REACHED` (RPD 16/16), so
    the job went DEAD_LETTERED.
  - Gemini was therefore still unavailable at 21:49–21:52 UTC.
  - Redis RPD for 2026-11-20 = 16 (resets 08:00 UTC on 2026-11-21); `jobs.dlq` = 3 messages.

## 2. Files inspected

| File | Purpose |
|---|---|
| `worker/gemini.py` | `GeminiClient._sdk()` (HttpOptions timeout, SDK retries off), `classify_gemini_error` |
| `common/http_errors.py` | `classify_http`, `_gemini` (503/504 → TransientError), `_safe_code`, `parse_retry_after`, `_detail` |
| `common/errors.py` | `PipelineError.detail` whitelist (`http_status`, `provider_code`, `retry_after_sec` …) |
| `worker/retry_policy.py` | `plan_failure`, `backoff_step`, `choose_bucket`, `public_retry_view` |
| `worker/runner.py` | `_handle_stage_failure` (logs + schedule/dead-letter) and the delay-hop logic |
| `common/jobs_repo.py` | `schedule_retry` / `dead_letter` event details |
| `common/ratelimit.py` | `report()`: cooldown only for 429/quota, which stays unaffected |
| `common/config.py` | settings, `backoff_delays`, `delay_queue_seconds` (decides which delay queues exist) |
| `common/logging.py` | JSON logs; extras pass through; redaction on every line |
| `docker-compose.yml`, `.env.example`, `infra/rabbitmq/rabbitmq.conf` (`consumer_timeout` 1 h) | configuration |
| google-genai 3.6.1 `_api_client.py` (in the image) | `HttpOptions` fields (`async_client_args`), `X-Server-Timeout`, per-request timeout |
| `extension/lib/popup-view.js`, `lib/jobs.js` | how backend codes reach the popup |
| tests: `test_unit_retry_policy.py`, `test_unit_http_classifier.py`, `test_unit_summarize.py`, `test_summarize_stage.py`, `support/fake_gemini.py`, `conftest.py`, extension tests | existing coverage |

## 3. Changes and rationale

| # | File | Change | Why |
|---|---|---|---|
| 1 | `common/http_errors.py` | Gemini **503/504**: `detail.retry_after_sec` = Retry-After header, else `RetryInfo.retryDelay` (both parsed, validated and capped as before). 500 unchanged | Lets the policy honour a valid Retry-After. The provider code (Google `error.status`, `_safe_code`-filtered) was already in `detail` |
| 1 | `worker/retry_policy.py` | `provider_status(error)` → `{http_status, provider_code}` when present. The retry doc gains `http_status`, `provider_code`, `backoff`; the terminal error gains `last_http_status`, `last_provider_code` | Persist the sanitized status (finding C2) |
| 1 | `common/jobs_repo.py` | `retry_scheduled` event detail: `http_status`, `provider_code`, `backoff`; `dead_lettered` event detail: `last_http_status`, `last_provider_code` | Same |
| 1 | `worker/runner.py` | "stage failed permanently / retry scheduled / retry budget exhausted" logs carry `http_status`, `provider_code` (+ `backoff`) | Same, in logs; still redacted |
| 2 | `worker/retry_policy.py` | `GEMINI_HTTP_503` / `GEMINI_HTTP_504` (provider GEMINI only) use `GEMINI_UNAVAILABLE_BACKOFF_SEC`; delay = `max(step, valid retry_after)`. Invalid values (≤ 0, NaN, bool, string) are ignored. Same `MAX_ATTEMPTS` → dead-letter + DLQ unchanged | Finding C3. Other providers and errors are untouched |
| 2 | `common/config.py` | `gemini_unavailable_backoff_sec` (default `60,180,600`, same validator as `BACKOFF_SCHEDULE_SEC`), property `gemini_unavailable_backoff_delays`. **Not** added to `delay_queue_seconds` | Long delays are reached by ADR-11 hops through the existing `jobs.retry.*` queues, so deploying declares no new queues |
| 3 | `common/config.py` | `gemini_request_timeout_sec` default **120 → 300** (1–600) | Finding C1: the SDK sends it as `X-Server-Timeout` |
| 3 | `docker-compose.yml` | worker env + `GEMINI_REQUEST_TIMEOUT_SEC: ${…:-300}`, `GEMINI_UNAVAILABLE_BACKOFF_SEC: ${…:-60,180,600}` | Previously not passed: the `.env` value could not take effect |
| 3 | `.env.example` | both variables, documented | — |
| 4 | `extension/lib/popup-view.js` | `reasonText(code, providerCode)`: `*_HTTP_503` / `UNAVAILABLE` → "… is temporarily unavailable (overloaded)"; `*_HTTP_504` / `DEADLINE_EXCEEDED` → "… took too long to respond"; other `_HTTP_5xx` → "temporary server problem". Exhausted 5xx retries get "Please try again later." | Finding C4. Other codes are unchanged |
| 4 | `extension/lib/jobs.js` | `sanitizeStatus` keeps `error.last_provider_code` only if `^[A-Z][A-Z0-9_]{0,63}$` | Safe pass-through |
| docs | `docs/decisions.md` | **ADR-21** | Design change of ADR-10/11 for Gemini 5xx |
| docs | `docs/api.md` | optional `last_http_status` / `last_provider_code` in the terminal error; the Gemini 503/504 schedule | Contract update (additive) |

`GeminiClient` itself needed **no change**: it already passed `gemini_request_timeout_sec` into
`HttpOptions(timeout=ms)`. The gap was the default and the missing Compose wiring. The API's `retry`
view (`public_retry_view`) is deliberately unchanged.

## 4. Tests

### New and updated tests
- **`backend/tests/test_unit_gemini_failures.py`** (new, 24 cases):
  - 503/504 keep only `http_status` + sanitized `provider_code`; message text is never kept;
  - Retry-After precedence (header > RetryInfo), invalid values, cap 3600;
  - 500 has no Retry-After; an unsafe status falls back to the numeric code;
  - a real SDK `ServerError` with a Retry-After header;
  - the policy: 60 / 180 / 600 / 600 then DEAD_LETTERED with `last_http_status`; valid and invalid
    Retry-After; buckets stay within existing queues; YouTube/Notion/Gemini-500/network/429 keep the
    default schedule; fields omitted when unknown; public view unchanged; topology unchanged;
  - settings defaults and validation;
  - **timeout wiring through the real google-genai SDK** (mock `httpx2` transport injected via
    `async_client_args`): `X-Server-Timeout: 300` (and 46 for 45.2 s), client read timeout 300,
    exactly one request, path `…/models/gemini-3.8-flash:generateContent`;
  - a 503 through the real SDK classified with status + Retry-After 13.
- **`backend/tests/test_summarize_stage.py`** (real worker, real outbox/broker/Redis, fake Gemini):
  - 503 and 504 → retry event `http_status`/`provider_code` (`UNAVAILABLE` / `DEADLINE_EXCEEDED`),
    `backoff=gemini_unavailable`, delay 3 (the test schedule's first step), logs contain the provider
    code but not the upstream message, then COMPLETED;
  - 503 with `Retry-After: 7` → delay 7; the API retry view is unchanged; no shared cooldown;
    COMPLETED;
  - **persistent 503 with `MAX_ATTEMPTS=3`** → DEAD_LETTERED, delays [3, 6], terminal error with
    `last_http_status` 503 and `last_provider_code` UNAVAILABLE, a `dead_lettered` event with both, a
    **DLQ copy** in `jobs.dlq`, the API shows the fields, no upstream text anywhere.
- `support/fake_gemini.py`: scenarios `deadline_once`, `unavailable_always`,
  `unavailable_retry_after_once:<s>` (real SDK errors; Retry-After via a real `httpx2.Response`).
- `conftest.py`: `GEMINI_UNAVAILABLE_BACKOFF_SEC=3,6,12` (short and distinct from `2,4,8`).
- **Extension** (`tests/manifest_and_popup.test.js` +4, `tests/jobs.test.js` +1):
  - DEAD_LETTERED with GEMINI_HTTP_503 / UNAVAILABLE → exact readable text with "Please try again
    later.", no raw code or JSON, SUMMARIZING marked failed;
  - 504 / other 5xx / provider-code-only / quota / unknown cause;
  - waiting retry after a 503 is shown as still processing;
  - `reasonText` mapping;
  - `sanitizeStatus` keeps only a safe `last_provider_code`.

### Commands and results
| Command | Result |
|---|---|
| `cd extension && npm test` | **94 passed**, 0 failed (89 before + 5 new) |
| `docker compose --profile test build tests` (test image only; `:dev` untouched) | built |
| `… pytest tests/test_unit_gemini_failures.py tests/test_unit_retry_policy.py tests/test_unit_http_classifier.py tests/test_unit_summarize.py tests/test_summarize_stage.py` | **334 passed, 1 failed** (74 s). The failure was my own test expectation: an unsafe `status` falls back to Google's numeric `error.code` (`"503"`), which is the existing intended sanitization. The test was corrected; no product change |
| rebuild test image; `pytest tests/test_unit_gemini_failures.py` | **24 passed** |
| full backend suite (`… pytest -p no:warnings -q -rfE --tb=short`, after the corrected test image) | **888 passed, 0 failed** in 789.14 s (2026-11-21 03:45–03:58 IST); previous 861, so +27 new (24 unit + 3 integration) |

## 5. handover.md

- Created the **root `handover.md`** from verified evidence (code, tests, configuration, Docker and
  database state, session logs). It covers:
  - requirements and architecture;
  - a per-feature status (verified real / fakes only / blocked / not implemented);
  - exact test results, the Gemini blocker, deployment state and repo-vs-deployed differences;
  - configuration (names only), limitations, prioritised remaining work, session history.
- `docs/PROJECT_STATUS_AND_HANDOVER.md` was replaced by a short pointer: its contents were outdated and
  contradictory; its history is in Git.
- A memory note records the permanent rule: update `handover.md` at the end of each implementation
  session.

## 6. Unresolved / limitations

- **Gemini is still unavailable (external):** the third real job also got 503s. The daily budget is
  used up until 08:00 UTC on 2026-11-21. ADR-21 cannot fix provider availability.
- **Not deployed:** the running stack still has the old behaviour (120 s, 10/30/90, no provider status).
- ADR-21's longer schedule means a failing Gemini job now takes about 24 min to dead-letter (by
  design) instead of about 6 min. Each attempt still costs one RPD.
- The extension changes need an extension reload in Chrome.

## 7. Deployment required? Yes (needs approval)

1. `docker compose build migrate`
2. `docker tag notes-agent-backend:dev notes-agent-backend:<commit-or-adr21-tag>`
3. `docker compose up -d --no-build gateway outbox-publisher worker` (migrate re-runs and finds
   nothing; no topology change)
4. verify:
   - `/healthz`;
   - worker env names include `GEMINI_REQUEST_TIMEOUT_SEC` / `GEMINI_UNAVAILABLE_BACKOFF_SEC`;
   - logs are clean.
5. Reload the extension in Chrome.
