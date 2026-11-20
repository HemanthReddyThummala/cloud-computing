# Gemini integration — root-cause diagnosis of the repeated SUMMARIZING failures (read-only)

Date: 2026-11-21 IST (≈ 2026-11-20 20:1x UTC).

**Read-only.**
- No job submitted; no Gemini, Notion or YouTube call.
- No code, test, `.env`, Compose, database or Redis change; no restart or rebuild; no Git operation.
- Secrets: the API key was only checked for presence, length and format flags inside the worker
  (never printed). Logs were filtered to non-secret fields and scanned for key-like strings (0 hits).
  Transcript text was not printed (sizes only).

## 1. Execution path (file → function)

1. `worker/runner.py` → the stage registry from `worker/stages.py:build_stage_registry()` →
   `JobStatus.SUMMARIZING: make_summarize_stage(settings, gemini=None)`.
2. `worker/summarize.py:make_summarize_stage.summarize(ctx)`:
   - reads the TRANSCRIBING checkpoint segments;
   - `chunk_segments(segments, chunk_budget(settings))`: budget `min(12000, 200000−2000)` → for this
     video **1 chunk** (334 segments);
   - prompt = header + `transcript_line(i, seg)` = `"[#i mm:ss] text"` per segment;
   - `ask(ctx, SYSTEM, prompt, CHUNK_SCHEMA, ChunkNotes, usage)`.
3. `summarize.py:ask()`:
   - lazily creates `GeminiClient(settings)`, failing with `GEMINI_NOT_CONFIGURED` if there is no key;
   - `ctx.limiter.acquire(Provider.GEMINI, tokens=estimate)` (Redis Lua: RPM / TPM / RPD, ADR-13);
   - `gemini.generate(...)`; any exception → `classify_gemini_error(exc)` (`raise … from None`);
   - after success: `limiter.settle(permit, prompt_tokens)`, blocked → FATAL, no text / schema mismatch
     → `GEMINI_INVALID_OUTPUT`, then `ChunkNotes.model_validate_json`.
4. `worker/gemini.py:GeminiClient._sdk()`:
   - `genai.Client(api_key=…, http_options=HttpOptions(timeout=120000 ms,
     retry_options=HttpRetryOptions(attempts=1)))`, i.e. **no SDK retries**.
5. `gemini.py:GeminiClient.generate()`:
   - `client.aio.models.generate_content(model="gemini-3.8-flash", contents=prompt, config=…)` with
     `system_instruction=SYSTEM`, `response_mime_type="application/json"`,
     `response_json_schema=CHUNK_SCHEMA`, `max_output_tokens=8192`, `thinking_level="LOW"`;
   - **no** safety settings (API defaults), **no** tools (the "AFC enabled, max remote calls 10" log
     line is an SDK default and is inert without tools).
6. SDK (`google-genai 3.6.1`, transport `httpx2 2.16.2`), from `_api_client.py`:
   - Developer-API base URL `https://generativelanguage.googleapis.com/`;
   - API version = `GOOGLE_GENAI_API_VERSION` or **`GOOGLE_AI_API_DEFAULT_VERSION = 'v1'`** (env var not
     set) → `POST /v1/models/gemini-3.8-flash:generateContent`;
   - headers `x-goog-api-key` and **`X-Server-Timeout: ceil(timeout) = 120`**
     (`populate_server_timeout_header`).
7. Errors: `gemini.py:classify_gemini_error()` → `common/http_errors.py:classify_http()` →
   `_gemini()`:
   - 429 → `RateLimited` / `QuotaExhausted`;
   - **500/503/504 → `TransientError("GEMINI_HTTP_<status>")`** with
     `detail={"http_status", "provider_code": <Google error.status>}`;
   - 400/401/403/402/404 → fatal.
8. Retry: `TransientError` → provider budget, `BACKOFF_SCHEDULE_SEC=10,30,90` (last repeats),
   `MAX_ATTEMPTS=5` → `jobs_repo.schedule_retry()` (outbox → `jobs.retry.<n>s`) … →
   `jobs_repo.dead_letter()` (`RETRIES_EXHAUSTED`).

Effective worker settings (non-secret, read in the running worker): model `gemini-3.8-flash`, thinking
`low`, `max_output_tokens` 8192, `gemini_request_timeout_sec` 120, chunk tokens 12000, RPM/TPM/RPD
4 / 200000 / 16, `MAX_ATTEMPTS` 5. Key: present, length 53, no whitespace or quotes, not `AIza…`
(already noted in Phase B Part 2, when this format **worked**).

## 2. Evidence — both jobs

| Job | Window (UTC 2026-11-20) | Gemini requests (time → status, duration of the request) |
|---|---|---|
| `acffab62-5b35-4826-bd70-1bb43e7e51db` | 18:24:32 → 18:30:33 DEAD_LETTERED | 18:24:43 **503** (7.8 s) · 18:24:55 **503** (2.0 s) · 18:27:24 **504** (119.1 s) · 18:28:57 **503** (3.1 s) · 18:30:33 **503** (5.4 s) |
| `f87c737a-3877-4164-a3e3-ad451d0a91f9` | 20:03:58 → 20:11:12 DEAD_LETTERED | 20:04:18 **503** (17.2 s) · 20:06:27 **504** (118.6 s) · 20:07:00 **503** (3.2 s) · 20:09:39 **503** (68.6 s) · 20:11:12 **503** (2.8 s) |

- **Same pattern in both jobs:** 8 × 503 + 2 × 504.
- **Every request reached Google and got a real HTTP response.** The httpx2 log line shows each status
  line, and there was no `GEMINI_UNREACHABLE`.
- **Exactly one request per attempt (10 total).** No hidden extra requests, no SDK retries.
- **No internal exception misclassified as 503.** The only ERROR lines are the expected "retry budget
  exhausted"; no tracebacks.
- **TRANSCRIBING** succeeded in both jobs (334 segments each, identical checkpoint size). No SUMMARIZING
  checkpoint, so frames and publishing never started.
- **Retry timing exactly as configured:** 10 / 30 / 90 / 90 s, 5 executions, then DEAD_LETTERED, with
  worker alternation and no lost or duplicated retries.
- **Limiter correct:** Redis `ratelimit:gemini:rpd:2026-11-20` = **11** = 1 (Phase B Part 2 check) + 5
  + 5. The TPM window has expired; no cooldown keys (none are set for 5xx).
- **Request size** (computed locally from the stored checkpoint with the production functions):
  - prompt 16,880 characters, system 1,089, schema 727 → **≈ 4.7k input tokens**;
  - plain ASCII, no control characters, longest line 53 characters, 1 chunk;
  - the limiter estimate is 6.2k (≤ TPM) → the payload is **not** abnormal.
- **Earlier success (Phase B Part 2 log):** the same SDK 3.6.1, `v1` endpoint, model, schema, thinking
  level and production code path succeeded once on a 6-segment transcript: **417 prompt / 243 output
  tokens in 32.7 s**.

## 3. AI Studio test vs application request

| Aspect | AI Studio manual test | Application |
|---|---|---|
| Model | Gemini 3.8 Flash | `gemini-3.8-flash` (same; a wrong ID would give 404, not 503) |
| Endpoint | AI Studio UI backend (not visible) | `generativelanguage.googleapis.com/v1/…:generateContent` (SDK default) |
| Credentials / project | UI login | API key (53 characters). **Same project: not proven.** Supporting evidence: the dashboard's **6/20 RPD** at screenshot time equals exactly our counted requests for that Pacific day before job 2 (1 + 5), which suggests the key belongs to the shown project and that failed 503 requests are counted |
| Input | "simple prompt" (small) | ≈ 4.7k tokens of numbered transcript + system instruction |
| Output | free text, short | **constrained JSON** (`response_json_schema`), summary + key points + sections + up to 8 visual moments; `max_output_tokens` 8192 |
| Thinking | UI default | `LOW` |
| Deadline | UI default | **`X-Server-Timeout: 120`** (from our client timeout) |
| Retries | — | none in the SDK; the outbox retries the full heavy request 4× within about 6 min |

## 4. Findings, ranked

### Confirmed (by code and evidence)

| # | Finding | Impact | Confidence |
|---|---|---|---|
| C1 | **The 504s are caused by our 120 s deadline.** The SDK sends `X-Server-Timeout: 120`, and both 504s arrived at 118.6 to 119.1 s. Google ended generation at our deadline: the model needed more than 120 s for this request. `GEMINI_REQUEST_TIMEOUT_SEC` exists in `Settings` (≤ 600) but is **not passed through Compose**, so the default 120 applies | 2 of 10 failures; also shows that full generation for this request is slow | High |
| C2 | **The Google error status is dropped.** `_gemini()` stores `provider_code=error.status` in `TransientError.detail`, but `schedule_retry()` and the runner's log keep only `reason_code`. It is therefore impossible to tell an overload `503 UNAVAILABLE` ("model is overloaded") from other 503 causes. This is the **missing evidence** | Diagnosis blocked; no effect on behaviour | High |
| C3 | **The retry policy cannot ride out a provider overload and multiplies quota use.** About 6 min of total backoff, and every retry resends the full ≈ 4.7k-token request. Each failed job costs 5 of 16 RPD (11 used now) | Turns a temporary 503 episode into DEAD_LETTERED; quota drain | High |
| C4 | Popup shows the raw `GEMINI_HTTP_503` (no `_HTTP_5xx` mapping in `popup-view.js:reasonText`) | Cosmetic | High |

**Not defects (verified):**
- correct model ID;
- `v1` is the SDK's own default and the identical configuration worked;
- the payload is valid (an invalid field or schema would give 400);
- the key format worked before;
- no misclassification;
- no extra requests;
- the limiter, counter and retry mechanics are correct;
- no connection-pool or transport errors;
- the tested production path is identical to the worker path (only the input size differs).

### Hypotheses (not provable without new evidence)

| # | Hypothesis | Supporting evidence | Against / unknown | Likelihood |
|---|---|---|---|---|
| H1 | **Capacity/overload on Google's side for this model on the free tier, hitting long, heavy generations.** 503 UNAVAILABLE is Google's overload or "try again later" status | Both jobs, 1.5 h apart, failed identically; 503s after varying times (2 s to 69 s) look like load shedding; the tiny earlier request already took 32.7 s (≈ 7 tokens/s), so this request's output (likely 1.5k–3k tokens + thinking) may need several minutes; a tiny AI Studio prompt succeeds | The Google status and message were not recorded (C2) | **Highest** |
| H2 | **Request weight** (large constrained-JSON output + thinking) exceeds what the model can deliver within the deadline, causing 504; mid-generation aborts surface as 503 | 504 exactly at the deadline (C1); 503 after 17 s and 69 s | Some 503s took only 2–3 s, which looks like immediate rejection rather than a too-long generation | High (as a contributor) |
| H3 | **AI Studio vs API project/route difference** (UI capacity pool vs API key quota pool) | AI Studio UI traffic can be served differently from API-key traffic | RPD 6/20 matching our count suggests the same project; routing can still differ | Medium–low |
| H4 | `v1` vs `v1beta` routing for this model | — | The same `v1` path succeeded before; an unsupported feature would return 400, not 503 | Low |
| H5 | Transcript content or formatting | — | ASCII, small, well-formed; a content problem would be 400 or a block, not 503 | Very low |
| H6 | The key changed after the Phase B success (`.env` modified 2026-11-20 22:10 IST) and points to another project | — | An invalid or forbidden key gives 400/401/403, not 503; RPD match | Very low |

## 5. Conclusion

- **The requests are valid and reach Google. Google itself answered 503 (8×) and 504 (2×).** The code
  defects found do **not** create the 503s.
- **The 504s are directly caused by our 120 s server deadline (C1).**
- **The root cause of the 503s cannot be confirmed from existing evidence**, because the Google
  `error.status` is discarded (C2). The best-supported explanation is **H1 + H2**: the free-tier model
  is overloaded or slow, and our request is much heavier (≈ 4.7k input tokens + several thousand
  tokens of constrained JSON output with thinking) than a simple AI Studio prompt.
- **This is why a simple AI Studio prompt works while both jobs fail:**
  - the AI Studio test is small and short-lived;
  - our request needs minutes of generation at the observed speed, so it hits the 120 s deadline or is
    shed under load;
  - our retry window (about 6 min) is too short to outlast the episode.

## 6. Smallest recommended fix (pending approval; nothing implemented)

1. **Observability (code, tiny, safe):** keep the sanitized `detail` (`http_status`, `provider_code` =
   Google `error.status`, already filtered by `_safe_code`; never the message text) in the
   `retry_scheduled` / `dead_lettered` event and the worker warning log.
2. **Deadline (configuration + Compose):** pass `GEMINI_REQUEST_TIMEOUT_SEC` to the worker in
   `docker-compose.yml` and set about 300 s. The validator allows up to 600.
3. **Retry policy for Gemini 5xx only (code, small):** a longer backoff for `GEMINI_HTTP_503/504`
   (for example 60 / 180 / 600 s) instead of the generic 10 / 30 / 90, so an overload episode is waited
   out with fewer wasted RPD. This is optional; decide after the test below.
4. **Popup wording** (C4), optional.

Request-weight reductions (for example lower `max_output_tokens` or fewer points) should be decided only
if the test shows that latency is the cause.

## 7. Smallest additional test (needs approval; costs Gemini quota; 9 RPD left until 08:00 UTC 2026-11-21)

Run in a one-off container from the deployed image (`docker compose run --rm --no-deps -T --entrypoint
python worker -c …`). Nothing is written to the database; the limiter is used. Log only status, Google
`error.status`, latency and token counts — never the key or message bodies.

| Test | Purpose | RPD |
|---|---|---|
| T1: production `GeminiClient.generate` with the tiny 6-segment checkpoint (the Phase B input) | Is the API path through our key and project working **right now**? 503 → project or model availability via the API (H1/H3); OK → continue | 1 |
| T2: production SUMMARIZING request on the stored checkpoint of `f87c737a…`, with `gemini_request_timeout_sec=300` | Does the real request succeed with a longer deadline? Record latency and output/thinking tokens (H2), or the exact Google `error.status` on failure (H1) | 1 |

Decision:
- **T1 fails** → external availability (wait or check the Gemini status), not our code.
- **T1 OK, T2 OK** → apply fix 2 (+1), then retry the video via Take Notes.
- **T1 OK, T2 fails with UNAVAILABLE** → overload on heavy requests; apply 1 + 3, and consider a
  lighter request.
- **T2 504 even at 300 s** → reduce request weight.

## 8. Commands executed (all read-only)

- `psql` (via `docker exec notes-agent-postgres-1`):
  - `jobs` (all), `job_events` (both jobs), `job_checkpoints` (sizes and counts),
    `dead_letter_audit`;
  - the checkpoint JSON was piped once into the worker's Python to compute sizes with the production
    functions (nothing printed besides counts).
- `docker compose logs --since 2026-11-20T18:10:00Z worker`, filtered to non-secret fields; key-like
  scan: 0.
- `docker exec notes-agent-worker-1 python -c …`: package versions; `get_settings()` non-secret values
  and key presence/length/format flags.
- `docker exec … grep/sed` of `google/genai/_api_client.py`: default API version, `X-Server-Timeout`,
  retry options; env variable **names** containing GOOGLE/GENAI/GEMINI.
- `redis-cli SCAN/TYPE/GET/TTL` on `ratelimit:*`.
- Source read: `worker/gemini.py`, `worker/summarize.py`, `worker/stages.py`,
  `common/http_errors.py` (Gemini section, `_detail`, `_safe_code`), `common/jobs_repo.py`
  (`schedule_retry`), `docker-compose.yml` (no `GEMINI_REQUEST_TIMEOUT_SEC` / `GEMINI_MAX_OUTPUT_TOKENS`),
  `docs/session_logs/phaseB_part2_gemini_summarization.md`.
