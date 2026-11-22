# Demo fault switch — investigation and design proposal (NOT implemented)

Date: 2026-11-22 IST. Purpose: make the remaining spec §6 demos (DLQ, graceful frame degradation, HTTP
429) live and deterministic.

**Design only.**
- No code, `.env`, Compose, Redis or Git change.
- No job; no external call.
- Root `handover.md` untouched.

## 1. Investigation (existing paths the faults must exercise)

| Path | Where | Behaviour to reuse |
|---|---|---|
| YouTube metadata request | `worker/transcribe.py::make_transcribe_stage.call()` | `await ctx.limiter.acquire(Provider.YOUTUBE)` → `asyncio.to_thread(fn)` → any exception → `classify_metadata_error(exc)` (`worker/youtube.py`). A `yt_dlp.networking.exceptions.TransportError` → **`TransientError("YOUTUBE_UNREACHABLE")`** (retryable, provider budget) |
| Retry / DLQ | `worker/runner.py::_handle_stage_failure` → `retry_policy.plan_failure` → `jobs_repo.schedule_retry` / `dead_letter` | `BACKOFF_SCHEDULE_SEC` (10,30,90; last repeats), `MAX_ATTEMPTS` (5) → `DEAD_LETTERED`, `RETRIES_EXHAUSTED`, `last_error_code`, a DLQ copy to `jobs.dlx` → `jobs.dlq` |
| Gemini request | `worker/summarize.py::ask()` | limiter permit → `gemini.generate()` inside `try` → `classify_gemini_error(exc)` → for an SDK `ClientError(429, RetryInfo)` → **`RateLimited("GEMINI_RATE_LIMITED", retry_after_sec)`**. Runner: `limiter.report()` → **shared cooldown** `ratelimit:cooldown:gemini`; the delay is `max(step, Retry-After)`; the `retry` object is shown by `GET /jobs/{id}` and the popup ("Waiting to retry: the AI model is limiting requests …") |
| Frame extraction | `worker/frames.py::_extract_into` loop | per timestamp `extract_frame(...)` → `except FrameError as exc: failed.append(reason)` → `_output` adds `FRAME_EXTRACTION_FAILED` warnings → runner → **`COMPLETED_WITH_WARNINGS`**; PUBLISHING adds only the successful frames; the popup shows "Done, with warnings" |
| Stage context | `worker/stages.py::StageContext`, created in `runner.py` (`job` = full row incl. `attempts`) | a place to pass an optional demo-fault handle |
| Attack surface | gateway `JobRequest` (`extra="forbid"`), Redis (no published port), no admin endpoint | job requests cannot carry extra fields; Redis is only reachable inside the Docker network |
| D-1 (ADR-14) | `classify_metadata_error` → `_unavailable()` → `FatalStageError YOUTUBE_VIDEO_UNAVAILABLE` | **unchanged**: private/unavailable videos stay `FAILED` |
| Existing placeholder | `CHAOS_ENABLED` in `.env.example` (and in the user's `.env`) | unused by code; not reused (left inert in `.env`) |

## 2. Proposed design

### 2.1 Scenarios (exactly three)

| Scenario | Hook (the only new branch) | Injected exception: the **same type a real failure produces** | Existing path then exercised |
|---|---|---|---|
| `dlq` (retryable failure until DLQ) | `transcribe.call()`, for the **first YouTube request (metadata)** of the bound job, on **every** attempt; after the limiter permit, **instead of** `yt-dlp` | `yt_dlp.networking.exceptions.TransportError("demo fault: simulated network failure")` | `classify_metadata_error` → `TransientError YOUTUBE_UNREACHABLE` → retries per `BACKOFF_SCHEDULE_SEC` → `DEAD_LETTERED` (`RETRIES_EXHAUSTED`, `last_error_code YOUTUBE_UNREACHABLE`) + DLQ copy. **Zero external calls** (YouTube is never contacted) |
| `gemini_429_once` | `summarize.ask()`, inside the existing `try`, **instead of** `gemini.generate()`, only while the job's `attempts == 0` | `google.genai.errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "demo fault", "details": [RetryInfo "20s"]}})` | `classify_gemini_error` → `RateLimited GEMINI_RATE_LIMITED` (Retry-After 20 s) → shared cooldown 20 s → re-queued with a 20 s delay (visibly waiting) → the 2nd attempt makes the real Gemini request → normal completion |
| `frames_partial` | `frames._extract_into` loop, **instead of** `extract_frame()` for every **second** timestamp (indices 1, 3, 5, …); index 0 always runs normally | `FrameError("DEMO_INJECTED_FAILURE")` | the existing `except FrameError` → `failed` → `FRAME_EXTRACTION_FAILED` warnings → `COMPLETED_WITH_WARNINGS`; the page is published with the remaining real frames. (With a single timestamp nothing fails, so the demo needs ≥ 2 visual moments; videos normally give 4–8) |

The faults raise **before** the external call they replace, so nothing is overloaded or made unavailable.
Everything after the raise is unmodified production code.

### 2.2 Activation: two independent gates plus a one-shot, single-job binding

1. **Master switch (process level):**
   - `DEMO_FAULTS_ENABLED` → `Settings.demo_faults_enabled: bool = False`;
   - passed **only to the worker** in Compose: `DEMO_FAULTS_ENABLED: ${DEMO_FAULTS_ENABLED:-false}`;
   - when false, the runner sets `StageContext.demo_faults = None`, every hook is
     `if ctx.demo_faults is not None and …`, and **no Redis key is read**;
   - normal operation (no variable set) is therefore inert by construction.
2. **Operator arming (per video, explicit, short-lived):**
   - a CLI run **inside a worker container**:
     `docker compose exec worker python -m notesagent.ops.demo_faults arm <dlq|gemini_429_once|frames_partial> --video <VIDEO_ID>`;
   - it **refuses** unless the master switch is on in that container;
   - it writes `demo:fault:video:<VIDEO_ID> = <scenario>` with a **15-minute TTL**; one scenario per
     video; re-arming overwrites;
   - also `status` (lists armed and bound keys) and `disarm --video <id>`.
3. **One-shot binding to exactly one job:**
   - at the start of TRANSCRIBING (the first stage of every job) the worker does an atomic
     **`GETDEL demo:fault:video:<video_id>`**;
   - if it finds a scenario, it sets `demo:fault:job:<job_id> = <scenario>` (TTL 2 h) and logs it;
   - later stages and retries of **that job only** read the job key;
   - a second job for the same video, or any other video, is never affected; an unused arming expires by
     itself.
4. **Not reachable from the outside:**
   - no gateway route or request field (the gateway schema forbids extra fields);
   - Redis is not published;
   - arming requires `docker compose exec` on the host;
   - the gateway never reads these keys.
5. **Fail-safe:** a Redis error while checking or binding → **no fault** (logged at debug; never fails the
   job).
6. **Visibility:**
   - every binding and every injection logs `WARNING "demo fault injected"` with `scenario`, `job_id`,
     `stage`, `attempt`;
   - the injected messages contain "demo fault" (never shown to users: our error messages are fixed
     strings).
7. **Bounded:**
   - `dlq` ends at `MAX_ATTEMPTS` (demo: `MAX_ATTEMPTS=3`, worker-only → about 40 s with the default
     10/30 s steps; no topology change);
   - `gemini_429_once` injects at most once per job (attempt 0);
   - `frames_partial` never fails index 0.

### 2.3 Files to change (implementation stage, after review)

| File | Change |
|---|---|
| `backend/notesagent/worker/demo_faults.py` (new, ~80 lines) | `Scenario` enum; `DemoFaults(redis)` with `bind(job)` (GETDEL video → SET job), `active(job_id, scenario)`, the three exception factories, logging; fail-safe on Redis errors |
| `backend/notesagent/ops/demo_faults.py` (new, ~60 lines) | the CLI `arm` / `disarm` / `status`; refuses when disabled; validates the scenario and video ID (`^[A-Za-z0-9_-]{11}$`) |
| `backend/notesagent/worker/stages.py` | `StageContext.demo_faults: Any = None` (optional field; default keeps every test and stage unchanged) |
| `backend/notesagent/worker/runner.py` | create `DemoFaults` only if `settings.demo_faults_enabled`; pass it to `StageContext`; log `demo faults ENABLED` at startup |
| `backend/notesagent/worker/transcribe.py` | bind at stage start; the `dlq` branch in `call()` for the metadata request |
| `backend/notesagent/worker/summarize.py` | the `gemini_429_once` branch in `ask()` |
| `backend/notesagent/worker/frames.py` | the `frames_partial` branch in the loop |
| `backend/notesagent/common/config.py` | `demo_faults_enabled: bool = False` |
| `docker-compose.yml` | worker env `DEMO_FAULTS_ENABLED: ${DEMO_FAULTS_ENABLED:-false}` |
| `.env.example` | replace the unused `CHAOS_ENABLED` with `DEMO_FAULTS_ENABLED=false` and a comment |
| `docs/decisions.md` | ADR-22 (the design above; D-1 preserved) |
| `docs/FAILURE_DEMONSTRATION_RUNBOOK.md`, `README.md` | live procedures for demos 2, 3, 5; one README paragraph |

### 2.4 Test plan

**Unit (`test_unit_demo_faults.py`):**
- disabled → `ctx.demo_faults is None`, hooks inert, **no Redis access** (spy);
- the CLI refuses to arm when disabled; it validates the scenario and video ID;
- arming sets the key with a TTL;
- binding is a single `GETDEL`: the first job binds, a second job for the same video does not;
- other videos are unaffected; the binding survives retries;
- `gemini_429_once` only at `attempts == 0`;
- `frames_partial` keeps index 0 and fails 1, 3, …;
- the injected exceptions are classified exactly as real ones (`YOUTUBE_UNREACHABLE`,
  `GEMINI_RATE_LIMITED` with Retry-After 20, `FrameError`);
- a Redis failure → no fault.

**Integration (real worker / broker / DB / Redis, fake providers):**
- `dlq` → `DEAD_LETTERED`, `last_error_code YOUTUBE_UNREACHABLE`, retries = `MAX_ATTEMPTS − 1`, a DLQ
  copy, the fake YouTube **never called**;
- `gemini_429_once` → a `retry_scheduled` event `GEMINI_RATE_LIMITED`, delay ≥ 20, a cooldown key, then
  `COMPLETED`, the fake Gemini called once;
- `frames_partial` → `COMPLETED_WITH_WARNINGS`, warnings with reason `DEMO_INJECTED_FAILURE` for indices
  1, 3, …, the fake Notion page has only the remaining frames;
- **an armed video with the switch off → completely normal job**;
- an unarmed job with the switch on → normal.

**Regression:** the full backend suite and the extension suite (no extension change is expected; the
existing popup texts already cover all three outcomes).

## 3. Live verification plan (after implementation; each live job needs approval)

| Demo | Enable | Real external calls | Evidence |
|---|---|---|---|
| DLQ | `DEMO_FAULTS_ENABLED=true MAX_ATTEMPTS=3 docker compose up -d --no-deps worker`; arm `dlq` | **none** | popup "Failed after retries" (cause: YouTube could not be reached); `jobs` `DEAD_LETTERED`, `attempts` 3, `last_error_code` `YOUTUBE_UNREACHABLE`; `job_events` 2 × `retry_scheduled` + `dead_lettered`; `jobs.dlq` +1; worker log `demo fault injected` ×3 |
| 429 | `DEMO_FAULTS_ENABLED=true docker compose up -d --no-deps worker`; arm `gemini_429_once` | 1 job (YouTube, 1 real Gemini request; the injected attempt also uses a limiter permit, as a real 429 would; frames; Notion) | popup "Waiting to retry: the AI model is limiting requests (attempt 1 of 5)" then Done; `GET /jobs/{id}` `retry.reason` `GEMINI_RATE_LIMITED` during the wait; Redis `ratelimit:cooldown:gemini` TTL ≈ 20 s; `job_events` `retry_scheduled` (delay 20); final `COMPLETED`; one Notion page |
| Frame degradation | same switch; arm `frames_partial` | 1 job | popup "Done, with warnings" listing the omitted frames; `jobs` `COMPLETED_WITH_WARNINGS`, warnings `FRAME_EXTRACTION_FAILED` reason `DEMO_INJECTED_FAILURE`; result `frames_published < frames_requested`; Notion page with the remaining frames + all text |

**Disable after the demos:** `docker compose up -d --no-deps worker` (the variables unset). Bound keys
expire by themselves.

## 4. Remaining decisions for the user

- Approve the design (and the scenario names / CLI form).
- Optional, not proposed: a `frames_all` scenario for the spec's "every frame fails → text-only page"
  sentence (currently covered by automated tests only).
