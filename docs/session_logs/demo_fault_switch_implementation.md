# Demo fault switch (ADR-22) — implementation

Date: 2026-11-22 IST. It implements the approved design (`demo_fault_switch_design.md`) plus the
user-requested `frames_all` scenario.

Not done (by instruction):
- no real job, no external API call;
- no `.env` change;
- no deployment (the running workers still use image `6aad6362b38a`, without ADR-22);
- no Git operations;
- root `handover.md` untouched.

## 1. Pre-coding check (design vs. code)

- **YouTube hook:** `transcribe.call()` takes `limiter.acquire(YOUTUBE)` → `to_thread(fn)` → `except` →
  `classify(exc)`. ✔ (the hook is placed inside the `try`.)
- **Gemini hook:** `summarize.ask()`: permit → `try: gemini.generate()` / `except: classify_gemini_error`.
  ✔ `usage["requests"]` only counts after a real reply, so an injected 429 is not counted as a request.
- **Frame hook:** `frames._extract_into` loop: `try: extract_frame()` / `except FrameError → failed`. ✔
- **Exception constructors:**
  - `TransportError(msg)` and `ClientError(code, response_json)` construct as the classifiers expect;
  - the message "demo fault: simulated network failure" matches none of `_YTDLP_PATTERNS`, so it
    classifies as `YOUTUBE_UNREACHABLE`, not unavailable (D-1 unaffected).
- `StageContext` takes a new optional field; `ctx.job` is the full row; Redis is not published; the
  gateway schema uses `extra="forbid"`. ✔
- **One refinement inside the design:** "once" for `gemini_429_once` uses a Redis `SET NX` marker
  (`demo:fault:job:<id>:gemini_429_done`) instead of `attempts == 0`. It is robust if an earlier stage
  retried, and across crashes. A Redis error → no fault.
- **No conflict was found; no broader redesign.**

## 2. Files changed

| File | Change |
|---|---|
| `backend/notesagent/worker/demo_faults.py` (new) | `Scenario` (`dlq`, `gemini_429_once`, `frames_partial`, `frames_all`); key helpers; `frame_fails()`; real-type exception factories (`yt_dlp TransportError`, `google.genai ClientError(429, RetryInfo 20s)`); `DemoFaults` with `bind()` (`GET` job key, else `GETDEL` video key → `SET` job key with a 2 h TTL), `scenario()`, `before_youtube_metadata()`, `before_gemini_request()` (`SET NX` marker), `frame_injected()`; every Redis call wrapped fail-safe; `demo fault bound to job` / `demo fault injected` warnings |
| `backend/notesagent/ops/demo_faults.py` (new) | the CLI `arm <scenario> --video <id>` (refuses unless `DEMO_FAULTS_ENABLED`; validates the ID; `SET … EX 900`), `disarm`, `status`; exit codes 0/2/3; never prints the Redis URL |
| `backend/notesagent/worker/stages.py` | `StageContext.demo_faults: Any = None` |
| `backend/notesagent/worker/runner.py` | creates `DemoFaults(self.redis)` only if `settings.demo_faults_enabled` (logs a warning at startup); passes it in `StageContext` |
| `backend/notesagent/worker/transcribe.py` | `call(..., demo_hook=None)` awaits the hook inside the existing `try`; at stage start `bind()` + the hook for the metadata request only |
| `backend/notesagent/worker/summarize.py` | `before_gemini_request(ctx)` inside the existing `try`, after the limiter permit, before `generate()` |
| `backend/notesagent/worker/frames.py` | the scenario is read once per stage; in the loop `raise FrameError(FRAME_FAILURE_REASON)` inside the existing `try` for the selected indices |
| `backend/notesagent/common/config.py` | `demo_faults_enabled: bool = False` |
| `docker-compose.yml` | worker env `DEMO_FAULTS_ENABLED: ${DEMO_FAULTS_ENABLED:-false}` (the rendered config resolves `false`; not passed to the gateway) |
| `.env.example` | unused `CHAOS_ENABLED` → `DEMO_FAULTS_ENABLED=false` + explanation |
| `backend/tests/test_unit_demo_faults.py` (new) | 13 unit tests |
| `backend/tests/test_demo_faults.py` (new) | 8 integration tests (real worker / outbox / broker / DB / Redis / FFmpeg; fake YouTube, Gemini, Notion) |
| `docs/decisions.md` | ADR-22 |
| `docs/FAILURE_DEMONSTRATION_RUNBOOK.md` | live procedures 2-L / 3-L / 5-L with the switch, the deployment prerequisite, readiness table, §6 status, demo order |
| `README.md` | "Failure demonstrations" subsection; `DEMO_FAULTS_ENABLED` row in the variable table |
| `docs/PROJECT_STATUS_AND_HANDOVER.md` | status, tests, remaining work, history |

Production error classification, retries, cooldown, DLQ, warnings and publishing code paths are **not
modified**; the hooks only raise the provider-shaped exception where the external call would be made.

## 3. Tests and commands

| Command | Result |
|---|---|
| `docker compose --profile test build tests` | built |
| `pytest tests/test_unit_demo_faults.py tests/test_demo_faults.py` | **21 passed, 0 failed** (44.95 s), on the first run |
| full backend suite `pytest -p no:warnings -q -rfE --tb=short` (03:17:06 → 03:31:54) | **923 passed, 0 failed** (871.05 s) = previous 902 + 21 new |
| `cd extension && npm test` | **94 passed, 0 failed** (no extension change needed) |
| `docker compose config --format json` (names / non-secret values) | worker `DEMO_FAULTS_ENABLED=false`; gateway has no such variable |

**What the tests prove:**
- **Unit:**
  - disabled by default (`Settings`, `StageContext` default `None`);
  - the injected exceptions classify exactly as real ones: `YOUTUBE_UNREACHABLE` (retryable) and
    `GEMINI_RATE_LIMITED` with Retry-After 20;
  - frame selection (partial: 1, 3, …; all; none for other scenarios);
  - one-shot binding on the real Redis (another video → none; the first job binds; a second job of the
    same video → none; the bound job keeps it);
  - **10 concurrent claims → exactly 1**; unknown values are ignored;
  - `dlq` on every attempt; the 429 exactly once regardless of the attempt counter; unbound jobs get
    nothing;
  - Redis errors (including on the `SET NX` marker) → no fault;
  - the CLI refuses when disabled **without touching Redis**, validates its input, and arm / status /
    disarm work (TTL ≤ 900; the status output contains no `redis://`).
- **Integration** (real runner, fake providers):
  - `dlq` → `DEAD_LETTERED` after exactly 3 attempts, `last_error_code YOUTUBE_UNREACHABLE`, 2 retry
    events, **0 fake-YouTube calls**, a DLQ copy in `jobs.dlq`, 3 injection logs, injected text not in
    the API;
  - `gemini_429_once` → visibly waiting (status SUMMARIZING, `retry.reason GEMINI_RATE_LIMITED`, delay
    ≥ 20, shared cooldown key), then `COMPLETED` with `attempts` 1; the retry event has `http_status` 429
    / `provider_code` `RESOURCE_EXHAUSTED`; **exactly 1 fake-Gemini call**; limiter RPD = 2 (both
    attempts passed the limiter); 1 injection log;
  - `frames_partial` → `COMPLETED_WITH_WARNINGS`, frames 2 requested / 1 published, 1 warning
    `DEMO_INJECTED_FAILURE`, exactly one fake Notion page with 1 image and headings;
  - `frames_all` → `COMPLETED_WITH_WARNINGS`, 0 published, 2 warnings, one **text-only** page (0 images,
    headings present);
  - an armed video with the **switch off** → normal `COMPLETED`, YouTube called, 0 injections, the
    arming key still present (never read);
  - a **second job for the same video** → `COMPLETED` while the first was `DEAD_LETTERED`; 1 injection
    in total;
  - **D-1:** switch on, private video → `FAILED YOUTUBE_VIDEO_UNAVAILABLE`, `attempts` 0, no injection.

## 4. Design decisions recorded

- Injection **replaces** the external call at the boundary (no external API is overloaded or made
  unavailable); downstream code is untouched.
- `gemini_429_once` takes the normal limiter permit, like a real 429, so a demo job uses 2 RPD and makes
  1 real Gemini request.
- Binding happens at the start of TRANSCRIBING (the first stage of every job); later stages and retries
  read the job key.
- `frames_partial` never fails index 0; with a single visual moment it injects nothing (the runbook
  advises a video with ≥ 2 moments).
- Live 429 demo: Gemini only. YouTube and Notion 429 remain covered by the automated tests (5-A), as
  designed.

## 5. Unresolved / next

- **Not deployed:** the running workers (`6aad6362b38a`) do not contain ADR-22. Deploying means
  `docker compose build migrate` + recreating **only** the workers (with the switch enabled only during
  demos). This needs the user's approval.
- **Not rehearsed live:** 2-L (DLQ, 0 external calls), 3-L (`frames_partial`, `frames_all`: 1 real job
  each), 5-L (`gemini_429_once`: 1 real job).
- Uncommitted: all of the above plus earlier docs (README, runbook, session logs).
