# Project status and handover — Distributed Multimodal Video Note-Taking Agent

**This file is the single source of truth for project status.**
- Last updated: **2026-11-21 17:43 IST** (first Flash-Lite end-to-end job failed at Notion publishing).
- Every session that changes implementation, tests, deployment or blockers updates this file at its end
  (rule recorded in `CLAUDE.md`).
- Detailed evidence lives in `docs/session_logs/` (one log per session).
- Design decisions live in `docs/decisions.md` (ADR-01 … ADR-21).
- API contract: `docs/api.md`. Specification: `PROJECT_SPEC.pdf` (authoritative).
- A root-level `handover.md` also exists from a same-day session. It is **not maintained**; this file
  supersedes it.

Status labels used below:
- **✅ Verified (real)**: observed against the real external service or the real running stack.
- **🧪 Tested (fakes)**: automated tests with fake providers only.
- **🟡 Implemented, untested** in that form.
- **🔴 Blocked / failing**.
- **⬜ Not implemented**.
- **Unverified**: could not be checked.

---

## 1. Objective and mandatory requirements (from `PROJECT_SPEC.pdf`)

Turn a YouTube video (≤ 20 min) into structured study notes, a summary plus **real video frames** of
important visuals each with an LLM caption, published to **Notion**. Everything is driven from a
**Chrome extension**; a queue-backed worker pool respects every external API's rate limits.

| # | Requirement |
|---|---|
| R1 | Four independent components: extension; API gateway (no pipeline work); durable broker (at-least-once, redelivery on crash); **≥ 2** workers |
| R2 | Extension: "Take Notes" button in the YouTube player (survives SPA navigation); background with one-time Notion OAuth, token storage, submission, status relay; popup with Queued → Transcribing → Summarizing → Extracting Frames → Publishing → Done, "View in Notion", clear failures and warnings, never raw JSON |
| R3 | Pipeline: timestamped transcript + true duration → LLM summary + important timestamps + one caption each (chunk/merge for long transcripts) → frames at those timestamps → Notion page with headings, bullets, captioned images |
| R4 | Lifecycle states QUEUED, TRANSCRIBING, SUMMARIZING, EXTRACTING_FRAMES, PUBLISHING, COMPLETED, COMPLETED_WITH_WARNINGS, FAILED, DEAD_LETTERED |
| R5 | REST: `POST /jobs` (202 + Location), `GET /jobs/{id}` (404 unknown), `GET /jobs/{id}/results` (409 before terminal) |
| R6 | Failure handling, demonstrable live: 429 from all three APIs → backoff with visible waiting; worker kill → no job lost, **no duplicate Notion page**; graceful degradation (missing frames → COMPLETED_WITH_WARNINGS / text-only); DLQ after a bounded, configurable number of attempts |
| R7 | **A:** ≤ 20 min supported, longer rejected deterministically. **B:** every image is a real frame at the flagged timestamp, never generated, each with an LLM caption |
| R8 | Unpacked extension (no build step); one compose file (scale/kill workers); **README** with base URL, default Notion destination and every required env var; health check |
| R9 | Evaluation: client experience; latency near the 20-min cap; burst submissions **without an LLM 429**; fault tolerance; constraints A/B |

## 2. Architecture

```
Chrome extension (MV3, unpacked, no build)
   content.js: Take Notes button ─► background.js (service worker)
   lib/auth.js (OAuth, sealed token) · lib/jobs.js (submit + poll) · popup reads chrome.storage
        │ HTTP (host_permissions http://localhost:8000/*, no CORS)
        ▼
Gateway (FastAPI)  POST /jobs · GET /jobs/{id}[/results] · /healthz · /livez · /oauth/notion/{authorize,callback}
        │ one PostgreSQL tx: job + outbox row;  Notion token → Fernet → per-job RabbitMQ queue cred.<job_id>
        ▼
PostgreSQL 16 (jobs, events, checkpoints, outbox, idempotency, dead_letter_audit; migrations 001–004)
Outbox publisher ──confirmed publish──► RabbitMQ 4.4.1 (quorum)
   jobs ─► jobs.work.v2 (delivery-limit 10, DLX jobs.dlx) · jobs.retry ─► jobs.retry.{5,10,30,90}s (TTL → back)
   jobs.dlx (fanout) ─► jobs.dlq (inspection) + jobs.dlq.audit ─► jobs.dlq.audit.parked
Workers ×2 (one image, all four stages): advisory lock + CAS + checkpoints; retries only via outbox
   TRANSCRIBING (yt-dlp metadata + youtube-transcript-api) → SUMMARIZING (Gemini, google-genai 3.6.1)
   → EXTRACTING_FRAMES (yt-dlp video-only download + FFmpeg) → PUBLISHING (Notion REST via httpx)
Redis 7: shared rate limiter (Lua; Gemini RPM/TPM/RPD per Pacific day, Notion per connection,
   YouTube cooldown), worker heartbeats, single-use OAuth state (hashed)
```

Key decisions (full text in `docs/decisions.md`):

| ADR | Decision |
|---|---|
| 02/03 | Transactional outbox; the Notion token is never in PostgreSQL (encrypted per-job broker mailbox) |
| 05/09/11 | Manual ack after durable commit; quorum queues; retries via outbox + delay queues with uncounted hops; `MAX_ATTEMPTS` = total executions |
| 06/13 | Production Gemini model is now `gemini-3.5-flash-lite` (switched 2026-11-21; originally `gemini-3.8-flash`); Redis limiter RPM 4 / TPM 200k / RPD 16 remains unchanged and was originally calibrated on 3.8 |
| 10/12 | Error classification (FATAL vs retry budgets); broker dead-letter auditing with a parking queue |
| 14–17 | One ADR per stage. Constraint A in TRANSCRIBING; grounded timestamps; real frames only; Notion page reconciled before every write (no duplicates) |
| 18/19/20 | Gateway-only OAuth with **sealed token** (`nva1.`) in the extension; background-only sign-in; Take Notes + polling + popup |
| **21** | **Gemini 503/504:** keep sanitized provider status (`http_status`, `provider_code`), backoff `60,180,600` (Retry-After honoured, capped 1 h), request timeout 300 s (also Google's server deadline), clear popup text |

## 3. Feature status (verified against code, tests, logs and the running stack)

| Area | Status | Evidence |
|---|---|---|
| Gateway API (R5), validation, idempotency keys, `/healthz` | ✅ Verified (real) + 🧪 | Real stack; backend suite |
| Outbox, broker topology, ≥ 2 workers, crash recovery (R1, R6 crash) | ✅ real stack; crash tests with fixture stages | SIGKILL crash set 160/160 (2026-11-19). **Never** a real mid-job kill during real stages |
| Retries, DLQ, broker dead-letter audit (R6 DLQ) | ✅ Verified (real) + 🧪 | 3 real jobs DEAD_LETTERED with DLQ copies (`jobs.dlq` = 3 messages) |
| Shared rate limiter (R9) | 🧪 + ✅ counting verified | The real RPD counter matched the real requests exactly (→ 16/16 on 2026-11-20). Burst behaviour against real Gemini: **Unverified** |
| TRANSCRIBING + constraint A (R3, R7-A) | ✅ Verified (real) | 3 real videos in real jobs (jNQXAC9IVRw 6, D26sUZ6DHNQ 334 ×2, _S-cBzAg0GE 213 segments); > 20 min rejection checked with real metadata (lowered limit) |
| SUMMARIZING (Gemini) (R3) | ✅ **Verified in one real short-video job** | `gemini-3.5-flash-lite` completed one structured request (one chunk, one API request): non-empty summary, 3 key points, 3 timestamped sections, and 4 visual moments. The job proceeded to frame extraction; multi-chunk/merge and completed-job behavior remain **Unverified**. Earlier `gemini-3.8-flash` failures remain unexplained. See §5 |
| ADR-21 Gemini 503/504 handling | ✅ **Deployed**; 🧪 tested; behaviour checked **offline in the deployed worker** | 2026-11-21: delays 60/180/600/600, Retry-After cap 3600, `X-Server-Timeout: 300` via the real SDK with a mock transport. **Not yet exercised by a real Gemini failure** |
| EXTRACTING_FRAMES (R3, R7-B) | ✅ Verified in one real job + 🧪 | One job downloaded the source and produced 4/4 JPEG frames; byte length, JPEG signature and SHA-256 verified from the durable checkpoint. All frame timestamps matched grounded visual moments. No Notion page was published. |
| PUBLISHING to Notion (R3, R6 no duplicates) | 🔴 **One real attempt failed** + 🧪 | The first Flash-Lite end-to-end job failed in `PUBLISHING` with `NOTION_OBJECT_NOT_FOUND` ("The Notion page was not found or not shared with the connection."); no page URL/result was recorded. Fake Notion API tests incl. SIGKILL mid-append. Real page contents remain **Unverified**. |
| Graceful degradation (R6) | 🧪 Tested (fakes) only | — |
| Notion OAuth, gateway (ADR-18) | ✅ Verified (real) | Gateway log "notion authorization completed" after the user's real sign-in (2026-11-20) |
| Extension sign-in, Take Notes, submission, polling, popup (R2) | ✅ partly real + 🧪 | Real Chrome: sign-in done, 3 real jobs submitted via Take Notes (`vna:` idempotency keys), popup showed "Failed after retries". 94 Node tests |
| Popup wording for Gemini 503/504 (ADR-21) | 🧪 | Extension tests. **Needs an extension reload in Chrome**; not yet seen in Chrome |
| README (R8) | ⬜ **Not implemented** | Only `extension/README.md` exists |
| Live demo of R6 (429 from all three APIs, worker kill, DLQ demo profile) | ⬜ Not prepared | Mechanisms exist and are tested with fakes |
| Latency near the 20-min cap (R9) | ⬜ Not measured | — |

## 4. Tests (exact latest results)

| Suite | Result | When |
|---|---|---|
| Backend full suite (Docker, real PostgreSQL / RabbitMQ / Redis, fake providers) | **902 passed, 0 failed** (775 s; 888 + 14 contract tests) | 2026-11-21 13:42–13:55 IST (production code unchanged = deployed `13a9334`) |
| Gemini SDK contract tests (`test_unit_gemini_sdk_contract.py`: real SDK request/response path, mock transport; request shape; 503/504/400/401/403/429/quota; structured-JSON success through the real stage) | **14 passed** | 2026-11-21 (503 investigation) |
| Focused Gemini summarization/unit tests (summarize, failures, SDK contract) | **71 passed, 0 failed** (1.96 s; offline/mocked) | 2026-11-21 (model switch) |
| Extension `cd extension && npm test` (Node, fake chrome + local fake gateway) | **94 passed**, 0 failed | 2026-11-21 (re-run in the deployment session) |
| Extension ↔ real gateway (fake Notion) cross-check | 10/10 PASS | 2026-11-20 (Phase C Part 3) |
| Deployed-worker configuration check (no network) | delays [60, 180, 600, 600]; 5th → DEAD_LETTERED with `last_provider_code` UNAVAILABLE; Retry-After 999999 → 3600; YouTube 503 → 10 s; SDK `X-Server-Timeout` 300 | 2026-11-21 deployment |

Commands:
- backend: `docker compose --profile test build tests`, then
  `MSYS_NO_PATHCONV=1 docker compose --profile test run --rm --no-deps tests pytest -q`;
- focused Gemini: same test container with
  `pytest -q tests/test_unit_summarize.py tests/test_unit_gemini_failures.py tests/test_unit_gemini_sdk_contract.py`.
- extension: `cd extension && npm test`.

Tests never call real Gemini, YouTube or Notion.

## 5. Gemini model switch: Flash-Lite reached frame extraction in a real job; Notion publishing failed

- **Production switch (2026-11-21 16:59 IST):**
  - `docker-compose.yml` now pins the worker `GEMINI_MODEL` to `gemini-3.5-flash-lite`, overriding any
    existing `.env` value. The application default and `.env.example` match; the actual `.env` was not
    edited.
  - The worker image was rebuilt from the current working tree, and only the two workers were
    recreated. Both report `gemini-3.5-flash-lite` from loaded application settings and are healthy.
    Gateway, outbox publisher, PostgreSQL, RabbitMQ and Redis were not targeted for recreation and
    remain healthy.
  - Focused offline unit/SDK-contract tests: **71 passed**. No live Gemini request was made for this
    configuration change.
  - Rate limits remain unchanged at RPM 4 / TPM 200000 / RPD 16. Their safety margin was originally
    set for `gemini-3.8-flash`; compatibility with Flash-Lite has not been revalidated.
- The prior one-chunk fixture test was followed by the real job below. Multi-chunk/merge behavior and
  an end-to-end job completing successfully remain **Unverified**.
- **First real Flash-Lite end-to-end test (2026-11-21 17:37 IST; exactly one job):**
  - Submitted through the normal Chrome extension Take Notes button for the public YouTube video
    *What is Microlearning? [2-Minute Explainer]* (`KlbsuPAibfY`, 153 seconds). The UI showed
    captions during playback. Exactly one matching job record exists; no second job or manual retry
    was made.
  - Job ID: `574eb00f-f7df-40a7-9bb4-1b235458d330`. Gateway health was 200 before the run; both
    workers were healthy and configured for `gemini-3.5-flash-lite`. Post-run gateway health remained
    200 with two live workers/consumers.
  - Stage progression: `QUEUED` → `TRANSCRIBING` → `SUMMARIZING` → `EXTRACTING_FRAMES` →
    `PUBLISHING` → `FAILED`.
  - **TRANSCRIBING passed:** 39 segments; true duration 153 seconds.
  - **SUMMARIZING passed:** model `gemini-3.5-flash-lite`, one chunk and one Gemini request; non-empty
    summary, 3 key points, 3 sections, and 4 visual moments. Section starts (0.089, 24.06, 38.06 s)
    and visual timestamps (2.21, 26.21, 40.21, 110.81 s) matched transcript segment starts.
  - **EXTRACTING_FRAMES passed:** 4 requested, 4 extracted, 0 failed. All four stored frame payloads
    had JPEG signatures and passed stored byte-length and SHA-256 checks; frame timestamps matched
    visual moments. The data came from the production FFmpeg frame stage.
  - **PUBLISHING failed:** `NOTION_OBJECT_NOT_FOUND`, sanitized message: "The Notion page was not
    found or not shared with the connection." The job had no per-job Notion parent ID and no result
    or page URL. The audit did not preserve the raw HTTP status; the error classification is the
    application's Notion-object-not-found code. No page contents can be verified.
  - **Conclusion:** real transcription, one structured Flash-Lite request, and actual frame
    extraction succeeded; the end-to-end job did not complete because publishing could not resolve
    or access the configured Notion destination. The evidence does not establish whether the page is
    missing or merely unshared.
  - Evidence and sanitized outputs:
    `docs/session_logs/first_real_e2e_test_flash_lite.md`.

- **Old key/project:**
  - real jobs for **D26sUZ6DHNQ** (×2) and **_S-cBzAg0GE** (×1) all DEAD_LETTERED at SUMMARIZING;
  - Google answered **503 `UNAVAILABLE`** (and 504 at the then-120 s deadline);
  - the controlled probe (2026-11-20 20:25 UTC, 6-segment input that had worked before) also got 503
    `UNAVAILABLE`.
- **New key, in a DIFFERENT Google Cloud project** (confirmed by the user; `.env` changed 2026-11-21 03:31
  IST, so the running workers already use it):
  - the obsolete old-project counter `ratelimit:gemini:rpd:2026-11-20` (16) was deleted with a single
    targeted `DEL` (nothing else touched; thresholds unchanged);
  - then **exactly one probe** (2026-11-20 23:20:47 UTC, production `summarize()` + shared limiter, 300 s
    timeout, 6-segment input) **reached Google and got HTTP 503 `UNAVAILABLE` in 1.68 s**. No retry.
    Limiter RPD for the new project: 1.
  - Evidence: `docs/session_logs/gemini_new_key_reset_and_probe.md`.
- **Root-cause investigation (2026-11-21, 0 API calls;
  `docs/session_logs/gemini_503_root_cause_investigation.md`):**
  - **no application-side bug was found and no fix applied;**
  - **the decisive evidence:** the successful request (Phase B Part 2) and the first T1 failure are
    **identical** (same code, SDK 3.6.1, `httpx2` backend, `v1 generateContent` endpoint, key, model,
    input, headers and body), yet one got 200 and the other 503;
  - checked and excluded:
    - environment propagation and proxies;
    - the HTTP backend (adding `httpx` in Part 4 did not change it);
    - request serialization (the SDK's snake_case `thinking_level` is identical in the success);
    - error classification and retries;
  - `v1` is the documented SDK default, and the features we use are listed for `v1`. Google documents 503
    as "temporarily overloaded or down".
  - **Remaining uncertainty:** why `gemini-3.8-flash` returned errors and whether Flash-Lite handles
    longer, multi-chunk transcripts and merge requests. The one-chunk structured fixture test passed,
    but does not resolve the earlier failures or validate a full video job.
- ADR-21 (deployed) makes such episodes visible (`last_provider_code`), longer-lived (about 24 min of
  backoff) and clearer in the popup. It **cannot** make Gemini answer.
- **Earlier alternative investigated:** `gemini-4-hydrogen` ("Gemini 4 Hydrogen", GA 2026-11-12, free tier,
  the officially recommended upgrade from 3.8-flash). See
  `docs/session_logs/gemini_4_hydrogen_feasibility.md`.
  - **Fits without code changes:** the SDK has no model allowlist; thinking `low` is valid; we send no
    `temperature`, `top_p`, `top_k` or caching. AI Studio shows 5 / 250k / 20, so with the ADR-13 × 0.8
    margin the limiter stays 4 / 200000 / 16 (unchanged).
  - **Unverified:** whether the `v1 generateContent` path (our SDK default) with `responseJsonSchema`
    serves it (the official examples use the Interactions API), and access for our project.
  - **Probed once (2026-11-20 23:30 UTC, approved; model overridden in the throwaway container only):
    reached Google → HTTP 503 `UNAVAILABLE` in 3.87 s**. Not 404 or 400, so the model ID and request
    were accepted far enough to get `UNAVAILABLE`. No notes were produced; production stayed on
    `gemini-3.8-flash` at that time (`docs/session_logs/gemini_4_hydrogen_probe.md`).
- **Prior failures:** 19 diagnostic requests after 2026-11-20 ~19:35 IST failed: 16 HTTP 503,
  2 HTTP 504, and one minimal plain-text `gemini-3.8-flash` request with no HTTP response within
  300 seconds. They span two projects and two models.
  - The `gemini-3.5-flash-lite` plain-text success and structured fixture test below show that not
    every tested model/request currently fails. Full-job and multi-chunk/merge behavior remain
    unverified.
- **Minimal plain-text probe (2026-11-21 09:45 UTC, approved, exactly 1 request;
  `docs/session_logs/gemini_minimal_plaintext_probe.md`):**
  - **Setup:** SDK 3.6.1 direct, `v1`, `gemini-3.8-flash`, `"Reply with exactly: Hello"`, no
    schema/system/thinking config, no `X-Server-Timeout`, SDK retries off. The limiter granted it (RPD
    0 → 1); the key was verified to be the new `.env` key (booleans only).
  - **Result: no HTTP response within 300 s** (local guard cancelled it): no status, no text.
  - This minimal request failed specifically for `gemini-3.8-flash`; the same prompt later returned
    HTTP 200 on `gemini-3.5-flash-lite`. This shows the result differs by tested model/request, but
    does not establish the underlying cause or validate the production structured request.
  - **Timeout detail (verified against the installed SDK source):** this probe did not set
    `HttpOptions.timeout`. The SDK passed `timeout=None` to HTTPX2 for the request, disabling its
    connect/read/write/pool timeouts; the 300-second `asyncio.wait_for` wrapper was the only bound.
  - **Undetermined:** the direct SDK call's silence is still unexplained. Separate DNS/TCP/TLS checks
    (including an SDK-context TLS handshake) do not show whether that earlier HTTP request reached
    Google's API frontend or why no response arrived.
- **Worker network diagnostic (2026-11-21 15:48 IST; no Gemini API request or quota):** both healthy
  running workers (`notes-agent-worker-1`, `notes-agent-worker-2`) resolved
  `generativelanguage.googleapis.com`, connected to port 443, and completed a TLS 1.3 handshake with
  normal certificate verification. See
  `docs/session_logs/gemini_network_connectivity_diagnostic.md` for the IPs, timings and commands.
  This confirms basic hostname/network/TLS connectivity from both containers, but **does not prove**
  Gemini API generation is healthy or explain the earlier SDK timeout.
- **SDK/HTTP transport investigation (2026-11-21 15:52 IST; zero Gemini API/HTTP requests):** both
  workers have CPython 3.12.16, `google-genai` 3.6.1 and use the SDK's default `httpx2` 2.16.2
  backend (not installed `httpx` 0.28.1). Proxy variables and CA overrides are unset; both workers
  have the same resolver, trust-store implementation and transport versions. A TLS-only handshake
  using the SDK's own verified TLS context also succeeded in each worker.
  - Production sets `HttpOptions.timeout=300000` ms; the SDK maps this to 300-second HTTPX2
    connect/read/write/pool phase timeouts and adds `X-Server-Timeout: 300`. This is not a separate
    total wall-clock deadline.
  - The minimal probe omitted the SDK timeout, so its 300 seconds came only from the outer wrapper.
    This explains the local wait duration, **not** why the HTTP request got no response.
  - Existing records show the earlier 200 and first 503 used the same SDK/backend and 120-second
    server timeout; later fast 503s used 300 seconds. This does not isolate the cause of the 503s.
    Historical 504s around 119 seconds align with the earlier 120-second server deadline.
  - No definite production configuration/implementation bug was found. The API-response cause remains
    unresolved. See `docs/session_logs/gemini_sdk_transport_investigation.md`.
- **Offline request-path investigation (2026-11-21 16:02 IST; `httpx2.MockTransport`, no external HTTP):**
  - The production `SUMMARIZING` stage generated `POST https://generativelanguage.googleapis.com/v1/models/gemini-3.8-flash:generateContent`.
    Captured body: `contents`, `systemInstruction`, `generationConfig`; JSON response schema (727
    characters), `maxOutputTokens=8192`, `thinking_level=LOW`; no tools or sampling parameters.
  - The mock saw `X-Server-Timeout: 300` and HTTPX2 connect/read/write/pool timeouts of 300 seconds.
    A synthetic 503 and 504 each caused exactly one mock request; retry attempt count was 1.
  - Expected status/provider codes were preserved in classified errors. A mock `ReadTimeout` was
    classified as retryable `GEMINI_UNREACHABLE`. The earlier success and first 503 used the same
    endpoint/body/model with a 120-second deadline; current production uses 300 seconds. That deadline
    difference may explain the old 504 timing, not the fast 503s.
  - No request-construction defect was found. This validates local SDK request construction only; it
    does not establish that a live Gemini endpoint accepts or answers the request.
  - Evidence and remaining questions: `docs/session_logs/gemini_offline_http_request_investigation.md`.
- **One controlled live Gemini diagnostic (2026-11-21 16:33 IST; one request only):**
  - The shared Redis limiter allowed one request from `notes-agent-worker-1`. The official
    `google-genai` 3.6.1 SDK used the existing configured key, `gemini-3.8-flash`, API v1, one
    attempt, and a temporary 45-second timeout for the plain-text prompt `Reply with exactly: Hello`.
  - An HTTP response arrived in 4.248 seconds: **503 `UNAVAILABLE`** (not a timeout). The sanitized
    provider message reported high model demand. No request ID was available in the checked safe
    response headers.
  - This confirms the observed request received an HTTP 503; it does not establish the underlying
    cause or definitively assign fault. No further live request was made.
  - Evidence and unresolved questions:
    `docs/session_logs/gemini_controlled_live_diagnostic_2026-11-21.md`.
- **Controlled Flash-Lite diagnostic (2026-11-21 16:43 IST; one request only):**
  - The shared limiter allowed one `gemini-3.5-flash-lite` request using the existing credentials,
    API v1, `google-genai` 3.6.1, one attempt, and a 45-second timeout. The prompt was
    `Reply with exactly: Hello`.
  - The SDK returned the expected `Hello` in 1.419 seconds. Its installed HTTPX2 response handling
    returns normally only for HTTP 200; no timeout or provider error occurred. No safe request ID was
    available.
  - This was only a plain-text model check. At that time, the production structured summarization
    request was unverified on Flash-Lite and production still used `gemini-3.8-flash`; it was
    subsequently switched as recorded at the top of this section.
  - Evidence and unresolved questions:
    `docs/session_logs/gemini_35_flash_lite_diagnostic.md`.
- **Flash-Lite structured summarization test (2026-11-21 16:48 IST; exactly one request):**
  - The three-segment `SEGMENTS` fixture from `test_unit_gemini_sdk_contract.py` produced one
    transcript chunk and no merge request; this count was reported before the call.
  - The shared limiter granted one permit. The existing production `make_summarize_stage()` and
    `GeminiClient` sent one structured request with the unchanged JSON schema/settings. It returned
    HTTP 200 in 1.381 seconds; production Pydantic parsing and normalization passed. Two sections
    and two visual moments mapped to fixture timestamps; no sections or moments were dropped.
  - This is evidence for one short structured request only, not for multi-chunk merging, a full job,
    or long transcripts. At the time of this test, production still used `gemini-3.8-flash`; no
    migration was made until the later switch recorded at the top of this section.
  - Evidence and remaining limitations:
    `docs/session_logs/gemini_35_flash_lite_structured_summarization_test.md`.
- **Next validation:** separately test Flash-Lite on a controlled multi-chunk transcript (chunk
  requests plus one merge), then run one short real end-to-end job. The model switch itself made no
  API requests.

## 6. Deployment status

- **Worker-only deployment (2026-11-21 16:59 IST):** worker ×2 runs image
  `notes-agent-backend:dev`, image ID `6aad6362b38a58dff512154d7a1807fe5551bf73521d473384ef29e89d69776f`,
  built from the current working tree. This image includes the updated application model default; no
  commit was created.
- Gateway and outbox publisher remain on their prior image ID
  `2a01b00b9603b8a201726a0a26eb25e45b7db6e1efb7c82f43087fb96eaa5536`. The command recreated only
  the worker service with `--no-deps`; PostgreSQL, RabbitMQ and Redis were not targeted. All seven
  application/infrastructure containers checked after deployment were running and healthy.
- **Effective worker configuration:** `GEMINI_MODEL=gemini-3.5-flash-lite`,
  `GEMINI_REQUEST_TIMEOUT_SEC=300`, `GEMINI_UNAVAILABLE_BACKOFF_SEC=60,180,600`,
  `BACKOFF_SCHEDULE_SEC=10,30,90`, `MAX_ATTEMPTS=5`. Both workers' loaded model setting was checked
  without printing secrets.
- Migrations 001–004 and retry queues `jobs.retry.{5,10,30,90}s` were not changed.
- **Rollback targets:** `notes-agent-backend:00ef0f4` (`70dce0098cd2`, previous deployment). The
  pre-Phase-C image `8cb661395cde` no longer exists.
- **Extension:** source unchanged by the deployment. The ADR-21 popup wording requires reloading the
  unpacked `extension/` folder in `chrome://extensions` (no build step).

## 7. Configuration (names only; never commit `.env`)

- **Required (Compose fails without them):** `DATABASE_URL`, `RABBITMQ_URL`, `REDIS_URL`,
  `JOB_PAYLOAD_ENCRYPTION_KEY`, `POSTGRES_{DB,USER,PASSWORD}`, `RABBITMQ_DEFAULT_{USER,PASS}`.
- **Gemini (worker):**
  - `GEMINI_API_KEY` [secret], `GEMINI_MODEL` (`gemini-3.5-flash-lite`, pinned in worker Compose),
    `GEMINI_THINKING_LEVEL` (low);
  - `GEMINI_RPM_LIMIT` / `GEMINI_TPM_LIMIT` / `GEMINI_RPD_LIMIT` (4 / 200000 / 16);
  - `GEMINI_REQUEST_TIMEOUT_SEC` (300), `GEMINI_UNAVAILABLE_BACKOFF_SEC` (60,180,600);
  - `TRANSCRIPT_CHUNK_TOKENS`, `MAX_FRAMES`.
- **Notion:** gateway gets `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET` [secret], `NOTION_REDIRECT_URI` =
  `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion`, and `OAUTH_STATE_TTL_SEC`; the
  worker gets `NOTION_DEFAULT_PARENT_ID` (ID or page URL); both get `NOTION_VERSION` and
  `NOTION_RPS_LIMIT`.
- **Retries:** `MAX_ATTEMPTS` (5), `BACKOFF_SCHEDULE_SEC` (10,30,90; one delay queue per value),
  `INFRA_MAX_ATTEMPTS`, `QUEUE_DELIVERY_LIMIT`.
- **The user's `.env`:** not edited during the model switch. The worker's literal Compose model setting
  overrides any `GEMINI_MODEL` value in `.env`; other settings continue to use their existing values.
- **Extension:** ID `kkkeigfecmfhmainhbkcpdhjnloifbdf` (manifest `key`); gateway `http://localhost:8000`
  (`extension/config.js` + manifest host permission); permissions `identity`, `storage`.

## 8. Known limitations and open issues

1. **End-to-end / Notion destination** (§5): one Flash-Lite job successfully transcribed, summarized,
   and extracted four frames, then failed at `PUBLISHING` with `NOTION_OBJECT_NOT_FOUND`. No real
   end-to-end COMPLETED job or published page exists to verify. Whether the configured Notion page is
   missing or not shared with the connection remains unresolved. Multi-chunk/merge behavior and the
   "no duplicate page" guarantee against real Notion are **Unverified**.
   The unchanged Gemini limiter safety margin was originally based on `gemini-3.8-flash` and has not
   been revalidated for Flash-Lite.
2. **Earlier end-to-end attempts were blocked by Gemini errors; the first Flash-Lite job reached Notion publishing but failed there.**
   - 23:14 UTC: a first probe attempt was refused locally by our limiter (old-project counter 16/16; 0
     requests to Google).
   - After the user switched to a key in a **different project** and the obsolete counter was deleted,
     the single probe at 23:20 UTC **reached Google: 503 `UNAVAILABLE`**.
   - The separate first Flash-Lite job is recorded in §5; no further job or retry was made after its
     Notion publishing failure.
3. **README missing (R8).**
4. **Quota:** a failing Gemini job can use 4–5 of the existing 16 daily requests; with ADR-21 it takes
   about 24 min to dead-letter. Model-specific Flash-Lite quota compatibility has not been revalidated.
5. **Real Chrome:** YouTube DOM changes could hide the button. The service-worker lifetime during long
   polling is Unverified (resumes on the next event).
6. **Broker:** the DLQ and parking queue have no retention limit; inspection is manual. Single-node
   RabbitMQ.
7. **Historical:** two unexplained intermittent test failures (2026-11-19), not reproduced since.
8. **Local `.env`:** contains unused keys (`BACKOFF_JITTER`, `GEMINI_LIMIT_SAFETY_FACTOR`,
   `LLM_MAX_WAIT_SEC`, …). They are harmless and were not edited (secrets file).

## 9. Remaining mandatory work (priority order)

1. **User:** reload the extension in Chrome (ADR-21 is deployed).
2. **Resolve the Notion destination first:** verify that the configured default parent refers to an
   existing page shared with the integration. Do not retry or submit another job until the user has
   reviewed this result and authorized the next attempt. After resolution, validate Flash-Lite on a
   controlled multi-chunk case (including one merge), then plan a short end-to-end job to verify a
   completed Notion page, frames and no duplicate page.
3. **README** (R8): base URL, default Notion destination (`NOTION_DEFAULT_PARENT_ID`), every env var
   from §7, how to load the extension, the demo commands.
4. **Live demos (R6/R9):**
   - 429 handling for all three providers (decide how);
   - kill a worker mid-job;
   - DLQ with the demo profile (`BACKOFF_SCHEDULE_SEC=2,4,8`, `MAX_ATTEMPTS=3`);
   - `--scale worker=N`;
   - a burst of Take Notes clicks without an LLM 429;
   - latency near the 20-min cap.
5. Optional: Notion-side token revocation, a popup "clear finished jobs" action.

## 10. Session history (most recent first)

| Date | Session log | Outcome |
|---|---|---|
| 2026-11-21 | `first_real_e2e_test_flash_lite.md` | One 2:33 video job: 39 transcript segments, one structured Flash-Lite request, 4/4 validated frames; failed at PUBLISHING with `NOTION_OBJECT_NOT_FOUND`; no page result and no retry |
| 2026-11-21 | `gemini_35_flash_lite_production_switch.md` | Pinned Flash-Lite in worker Compose and app defaults; rebuilt/recreated only two workers; both healthy and verified loading the model; 71 focused offline tests passed; no live Gemini request |
| 2026-11-21 | `gemini_35_flash_lite_structured_summarization_test.md` | One-chunk production stage test on the 3-segment contract fixture: limiter allowed one request; HTTP 200 in 1.381 s; schema/normalization and grounded section/moment timestamps passed; multi-chunk behavior unverified; no migration |
| 2026-11-21 | `gemini_35_flash_lite_diagnostic.md` | Shared limiter granted one plain-text request; `gemini-3.5-flash-lite` returned expected `Hello` (HTTP 200) in 1.419 s; structured production request remains unverified; no migration |
| 2026-11-21 | `gemini_controlled_live_diagnostic_2026-11-21.md` | Shared limiter granted one request; live plain-text v1 request received HTTP 503 `UNAVAILABLE` in 4.248 s (not a timeout); no request ID available; no further calls |
| 2026-11-21 | `gemini_offline_http_request_investigation.md` | Production stage captured through mock transport; v1 endpoint/body and 300-second timeout verified; 503/504 and timeout error handling preserved/classified; no request-construction bug found |
| 2026-11-21 | `gemini_sdk_transport_investigation.md` | Both workers match on SDK/transport, proxy, CA and DNS settings; production uses a 300-second SDK phase timeout; minimal probe's 300-second limit was outer-only; API-response cause remains unknown |
| 2026-11-21 | `gemini_network_connectivity_diagnostic.md` | Both workers: DNS, TCP/443 and verified TLS 1.3 succeeded; no Gemini API request; prior API timeout remains unexplained |
| 2026-11-21 | `gemini_minimal_plaintext_probe.md` | One minimal plain-text request (SDK direct, v1, gemini-3.8-flash, no config): no SDK timeout was set; outer 300-second guard cancelled it with no HTTP response; later transport checks passed, cause remains unknown |
| 2026-11-21 | `gemini_503_root_cause_investigation.md` | 503 root-cause investigation, 0 API calls: no application bug found (an identical request went 200 → 503); 14 SDK contract tests added; one minimal plain-text request recommended to close the remaining uncertainty |
| 2026-11-21 | `gemini_4_hydrogen_probe.md` | One approved probe with `gemini-4-hydrogen` (container-only override): reached Google, **503 UNAVAILABLE** (3.87 s); production unchanged |
| 2026-11-21 | `gemini_4_hydrogen_feasibility.md` | Docs/SDK feasibility for `gemini-4-hydrogen` (no API request): config-only switch looks possible; v1 generateContent + JSON schema unverified; limiter stays 4/200000/16 with margin |
| 2026-11-21 | `gemini_new_key_reset_and_probe.md` | User confirmed the new key is in a different project; deleted only `ratelimit:gemini:rpd:2026-11-20`; one probe reached Google: **503 UNAVAILABLE** (1.68 s); no job |
| 2026-11-21 | `gemini_new_key_limiter_scope_check.md` | New Gemini key detected (already in the running workers; 0 requests made). Limiter counters are project-level, the new key's project is unknown, so nothing was reset and no probe was run; awaiting user confirmation |
| 2026-11-21 | `e2e_probe_blocked_by_local_limiter.md` | The one authorised probe was refused LOCALLY by our RateLimiter (`GEMINI_DAILY_LIMIT_REACHED`, 16/16, 0 requests to Google); no job; no bypass |
| 2026-11-21 | `e2e_attempt_blocked_by_quota.md` | Inspection OK (deployed = `13a9334`); real test not started (limiter at 16/16); no external calls |
| 2026-11-21 | `deployment_adr21.md` | ADR-21 (uncommitted working tree) deployed and verified; this file restored as the source of truth; `CLAUDE.md` created |
| 2026-11-21 | `adr21_gemini_failure_handling.md` | ADR-21 implemented + tested (888 / 94) |
| 2026-11-20 | `gemini_controlled_tests_T1_T2.md`, `gemini_integration_root_cause_diagnosis.md`, `first_real_job_failure_diagnosis.md` | Gemini 503 `UNAVAILABLE` diagnosed (external) |
| 2026-11-20 | `deployment_phaseC_00ef0f4.md` | `00ef0f4` deployed and verified |
| 2026-11-20 | `phaseC_part{1,2,3}_*.md` | OAuth (gateway + extension), Take Notes / polling / popup |
| 2026-11-19/20 | `phaseB_part{1..4}_*.md`, `phaseA_*`, `phase2_*` | Pipeline stages, limiter, retries, DLQ audit |
