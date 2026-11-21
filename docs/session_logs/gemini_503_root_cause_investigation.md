# Gemini 503 UNAVAILABLE — root-cause investigation (no real API calls)

Date: 2026-11-21 IST. **Real Gemini, YouTube and Notion requests made: 0.** No `.env`, production code,
configuration, Redis or container change.

## 1. Question

Since 2026-11-20 ~19:35 IST, **17 of 17** real Gemini requests got 503 `UNAVAILABLE` (15) or 504 (2):
- across 2 Google projects/keys and 2 models (`gemini-3.8-flash`, `gemini-4-hydrogen`);
- including tiny 6-segment probes.

One identical tiny request had succeeded on 2026-11-20 (Phase B Part 2). Is there an application-side
cause?

## 2. What was inspected

| Area | Where | Finding |
|---|---|---|
| Client init / API version | `worker/gemini.py::GeminiClient._sdk`; SDK `_api_client.py` | `genai.Client(api_key, HttpOptions(timeout=ms, retry_options=attempts=1))`. API version = SDK default **`v1`** (`GOOGLE_AI_API_DEFAULT_VERSION`); `GOOGLE_GENAI_API_VERSION` not set |
| HTTP backend | SDK `_get_http_client_backend` | default **`httpx2`** (`_DEFAULT_HTTP_CLIENT_BACKEND='httpx2'`), no `GOOGLE_GENAI_HTTP_CLIENT` set. Adding `httpx` 0.28.1 to the runtime in Phase B Part 4 (after the success) does **not** change the backend; failure logs show the `httpx2` logger |
| Endpoint / model | captured offline (mock transport) in the running worker | `POST https://generativelanguage.googleapis.com/v1/models/gemini-3.8-flash:generateContent` |
| Headers | same | `x-goog-api-key` (the key appears only here, never in the URL or body), `x-goog-api-client`/`user-agent: google-genai-sdk/3.6.1 gl-python/3.12.16`, `x-server-timeout: 300`, `content-type` |
| Payload | same | top level `contents`, `systemInstruction` (role `user`), `generationConfig` = `maxOutputTokens` 8192, `responseMimeType` `application/json`, `responseJsonSchema` (727 chars), `thinkingConfig: {"thinking_level": "LOW"}`. No tools, temperature, topP or topK |
| `thinking_level` snake_case | SDK `models.py` (`setv(..., ['thinkingConfig'], thinking_config)`); offline `v1` vs `v1beta` | the SDK serializes ThinkingConfig fields in snake_case on **both** versions. It is the SDK's fixed behaviour and was **identical in the successful request** |
| Proxy / TLS env | worker env (names only) | no `*_PROXY`, `GOOGLE_GENAI_*`, SSL or CA overrides |
| Key propagation | earlier session (hash equality, booleans only) | the worker key equals `.env`; the model and timeout env are as configured |
| Timeout | settings + SDK | 300 s → `X-Server-Timeout: 300` + client read timeout 300 (ADR-21). The 503s arrive after 1.7–69 s, so they are not timeouts |
| Error classification | `common/http_errors.py::_gemini`, `worker/gemini.py::classify_gemini_error` | 503/504/500 → `TransientError` (provider budget); 429 → `RateLimited` / `QuotaExhausted`; 400 → `GEMINI_BAD_REQUEST`; 401/403 → `GEMINI_AUTH`; 404 → `GEMINI_MODEL_NOT_FOUND`; transport → `GEMINI_UNREACHABLE` |
| Retry / backoff | `worker/retry_policy.py` (ADR-11/21) | Gemini 503/504: 60/180/600/600 s, Retry-After honoured; SDK retries off; one request per attempt (confirmed in logs) |
| Limiter | `common/ratelimit.py` | per-project model of RPM/TPM/RPD; counters match the real requests; the only local refusal so far was correctly labelled |
| Historical evidence | session logs + worker logs | every failure has an `httpx2` line `HTTP/1.1 503 Service Unavailable` (or 504): a **real HTTP response from Google**, with a Google JSON error body (`status: UNAVAILABLE`) |

## 3. Successful vs failed requests (controlled comparison)

| Attribute | Success (2026-11-20, Phase B Part 2) | T1 failure (2026-11-20 20:25 UTC) | New-key failures (23:20 / 23:30 UTC) |
|---|---|---|---|
| Code path | production `summarize()` → `GeminiClient` | same | same |
| SDK / backend / endpoint | 3.6.1 / httpx2 / `v1 generateContent` | same | same |
| Input | 6-segment "Me at the zoo" checkpoint | **same** | same |
| Body shape (schema, system instruction, `thinking_level` LOW, 8192) | as in §2 | **same** | same |
| `X-Server-Timeout` | 120 | 120 | 300 |
| Key / project | old | **old (same)** | new (different project) |
| Model | `gemini-3.8-flash` | same | `gemini-3.8-flash`, then `gemini-4-hydrogen` |
| Result | **200, notes, 32.7 s** | **503 UNAVAILABLE, 4.3 s** | 503 UNAVAILABLE, 1.7 s / 3.9 s |

**The success and the T1 failure are the same request** (same code, SDK, endpoint, key, model, input,
headers, body) a few hours apart. Only the time differs. The application cannot have produced a different
outcome for an identical request; the outcome changed on Google's side.

## 4. Official documentation (Google pages only)

- **API versions** (`aistudio.google.com/api-reference/api-versions`):
  - Structured Output, Thinking and System Instructions are supported on **both `v1` and `v1beta`**;
  - "The latest SDKs default to `v1`"; "Starting with the November 2026 major releases, the Google Gen AI
    SDKs call `v1` unless you set a version."
  - So our `v1` usage is the documented default, not an unsupported combination.
- **Errors** (`aistudio.google.com/docs/api-errors`, `/docs/troubleshooting`):
  - 503 `UNAVAILABLE`: "The service is temporarily overloaded or down." Action: "Wait and retry with
    exponential backoff".
  - 504 `DEADLINE_EXCEEDED`: "Remove or increase client deadline setting to use the server default".
  - 500: "Retry … If it persists, contact support".
- **Not found officially:**
  - a v1 generateContent reference page (the reference lists `/v1beta/models/{model}:generateContent`);
  - any statement on snake_case aliases;
  - readable incident data (the status page is JavaScript-rendered);
  - any statement on free-tier load shedding.

## 5. Possibilities evaluated

| Possibility | Verdict | Evidence |
|---|---|---|
| Wrong / outdated SDK or endpoint | **Not confirmed** | `v1` is the documented SDK default; the identical request succeeded |
| Unsupported model/API-version combination | **Not confirmed** | features are listed for `v1`; unsupported fields or models give 400/404, not 503; both models answered 503 |
| Request parameters / JSON schema | **Not confirmed** | identical shape succeeded; a parameter error is 400 `INVALID_ARGUMENT` |
| API key / env propagation | **Excluded** | hash equality; two keys and projects behave the same |
| Client init / serialization bug | **Excluded for our code**; SDK snake_case is constant | captured request (§2); the contract tests below pin it |
| Wrong retry / error classification | **Excluded** | 503 handling matches Google's guidance; contract tests for 503/504/400/401/403/429 pass |
| Provider-side unavailability | **Best-supported explanation** | identical request 200 → 503; cross-project and cross-model; Google's definition of 503 |
| Provider-side change rejecting our request shape with 503 | **Not excluded** | rare (it would normally be 400), but only a differently-shaped request can rule it out |

## 6. Conclusion

- **No application-side bug causing the 503s was found; no fix was applied.**
- The best-supported root cause is **Google-side unavailability of the Gemini API for these requests**:
  an identical request flipped from 200 to 503, and Google defines 503 as "temporarily overloaded or
  down".
- **Remaining uncertainty:** a Google-side change that answers *our particular request shape* (`v1`
  generateContent + JSON schema + thinking + system instruction) with 503, while AI Studio's route (the
  Interactions API) works.

**Smallest test that resolves it (not run; needs approval): one real request** through the same SDK, key,
model and `v1` endpoint with a **minimal plain-text prompt** (no schema, no system instruction, no
thinking config).
- **503** → the service is unavailable for API-key requests: provider-side, nothing to fix in code.
- **200** → a request feature is involved. Next, at most 3 single-feature requests (schema / thinking /
  system instruction), then fix that feature (e.g. send thinking as `thinkingLevel`, or pin `v1beta` after
  establishing compatibility).

## 7. Tests added (verification, not a fix)

`backend/tests/test_unit_gemini_sdk_contract.py` (14 cases) uses the **real google-genai SDK
request/response path** with a mock transport:
- **Request shape pinned:** endpoint `v1 …:generateContent`; the key only in `x-goog-api-key`;
  `x-server-timeout` 300; SDK header; body fields; snake_case `thinking_level` recorded; no sampling
  params.
- **503** (with and without Retry-After 21), **504**, **400** (body leaking the key), **401** (new;
  previously uncovered), **403**, **429 rate** (RetryInfo 12 s), **429 daily quota**. Each is checked
  for class, code, retry budget, `http_status`/`provider_code`, retry-after, exactly one HTTP request,
  and no key or message text in the error.
- A non-JSON 503 body is still a transient 503; a transport failure is `GEMINI_UNREACHABLE`, not 503.
- **Structured JSON success through the production SUMMARIZING stage:** parse, pydantic validation,
  grounding (the invented segment 99 dropped), sections and timestamps from real segment starts, usage
  321/45, limiter acquire then settle with the real prompt tokens.
- Invalid structured output (non-JSON, schema mismatch) → `GEMINI_INVALID_OUTPUT` (retryable, bounded).

## 8. Commands and results

| Command | Result |
|---|---|
| `git log`/`git diff` on the Gemini files and requirements; session-log greps | Gemini code unchanged since commit `5a63cdd`; only runtime dependency change after the success: `httpx`/`httpcore` (Phase B Part 4) |
| SDK source reads in `notes-agent-backend:13a9334` with `--network none` | backend selection, default API version, `X-Server-Timeout`, ThinkingConfig serialization |
| Offline request capture in `notes-agent-worker-1` (mock transport, fake key) | §2 |
| `docker compose --profile test build tests`; `pytest tests/test_unit_gemini_sdk_contract.py` | **14 passed** (1.79 s) |
| full backend suite | **902 passed, 0 failed** (775 s) |

Extension: no shared behaviour changed, so it was not re-run (last: 94/94).

## 9. Files changed

| File | Why |
|---|---|
| `backend/tests/test_unit_gemini_sdk_contract.py` (new) | contract/regression tests above |
| `docs/PROJECT_STATUS_AND_HANDOVER.md` | conclusion, tests, next action |
| this log | — |

No production code, `.env`, Compose, Redis or container change; production configuration is unchanged.
Root `handover.md` untouched.

## 10. Full-suite result

`docker compose --profile test run --rm --no-deps tests pytest -p no:warnings -q -rfE --tb=short` → **902 passed, 0 failed** in 775.14 s (2026-11-21 13:42–13:55 IST); previous 888 + 14 new.
