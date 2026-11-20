# Project status and handover — Distributed Multimodal Video Note-Taking Agent

- **Prepared:** 2026-11-20, for an independent review of the current project state. **Updated:**
  2026-11-20 after Phase A Part 2 (shared rate limiter) after Phase B Part 1 (TRANSCRIBING stage) and its deployment verification, after Phase B Part 2
  (SUMMARIZING stage), after Phase B Part 3 (EXTRACTING_FRAMES stage), and after Phase B Part 4 (PUBLISHING stage).
- **Sources:** `PROJECT_SPEC.pdf` (authoritative), `docs/decisions.md` (ADRs), `docs/api.md`, the
  session logs in `docs/session_logs/`, and direct inspection of the repository, Git and the running
  Docker stack.
- **Evidence labels:**
  - **[TEST]** automated test evidence (with date and result);
  - **[RUN]** observed on the running stack;
  - **[SRC]** source inspection only;
  - **[PLAN]** planned or designed, not implemented;
  - **[NV]** not verified.

---

## 1. Objective and the professor's mandatory requirements

Build a distributed, asynchronous system that turns a YouTube video into structured study notes —
summary text plus **real video frames** of important diagrams or slides — and publishes them to a
**Notion** page. Everything is driven from a **browser extension**. A queue-backed worker pool must
respect the rate limits of every external API it calls.

Mandatory requirements, with spec section numbers:

| # | Requirement |
|---|---|
| R1 | Four independently running components (§1.1): browser extension; API gateway (no pipeline work); durable message broker (at-least-once, redelivery on crash); worker pool with **≥ 2** independent processes |
| R2 | Extension (§2): content script with a "Take Notes" button in the YouTube player that survives SPA navigation; background script with **one-time Notion OAuth**, token storage, job submission and status relay; popup with human-readable progress Queued → Transcribing → Summarizing → Extracting Frames → Publishing → Done, a "View in Notion" link and clear failure/warning display (never raw JSON) |
| R3 | Pipeline (§3): timestamped transcript (and true duration); LLM summary bullets + important timestamps + **one LLM caption per timestamp**, with chunking/merging in order for long transcripts; frame capture at each timestamp from the video stream; Notion page with headings, bullets and captioned images |
| R4 | Job lifecycle (§4): QUEUED, TRANSCRIBING, SUMMARIZING, EXTRACTING_FRAMES, PUBLISHING, COMPLETED, COMPLETED_WITH_WARNINGS, FAILED, DEAD_LETTERED, with the specified transitions |
| R5 | REST API (§5): `POST /jobs` (validate, **202** + `Location`, `{job_id, status, video_id}`, 4xx `{"error":{code,message}}`, token supplied per request, default Notion destination documented); `GET /jobs/{id}` (read-only, 404 for unknown); `GET /jobs/{id}/results` (**409** before terminal, absolute Notion URL on success) |
| R6 | Failure handling (§6), demonstrable live: HTTP **429 from all three external APIs** → re-queued with exponential backoff and visible waiting; **worker crash** (e.g. SIGKILL) → message reassigned, no job lost, **no duplicate Notion page**, ack only after durable stage completion, idempotent stages; **graceful degradation** (some or all frames fail → still publish, COMPLETED_WITH_WARNINGS / text-only page); **DLQ** after a bounded, configurable, demonstrable number of attempts |
| R7 | Hard constraints (§7): **A** — videos ≤ 20 minutes supported, longer ones rejected deterministically; **B** — every image is a real frame from the video at the flagged timestamp, never AI-generated or substituted, and each has an LLM caption |
| R8 | Deliverables (§8): unpacked extension loadable in Chrome (no build or one command); one compose file with gateway, broker and ≥ 2 workers (scaling/killing demonstrable); **README** with base URL, default Notion destination and every required environment variable (LLM key and model, rate limit, Notion OAuth credentials, broker URL, retry/backoff); **health check** for gateway, broker and state store |
| R9 | Evaluation (§9): client experience; latency near the 20-minute cap; burst submissions drain **without ever triggering an LLM 429**; fault tolerance after a worker kill; constraints A/B judged by an LLM |

§10 leaves these to the team: broker technology, state model, how the global LLM limit is
coordinated, worker topology, LLM provider/model, transcript method, frame tooling, and extension
tooling.

---

## 2. Architecture and technologies

```
Chrome extension (planned) ──HTTP──► Gateway (FastAPI) ──one PostgreSQL tx: job + outbox row──► PostgreSQL 16
                                          │ encrypted Notion token → per-job RabbitMQ queue cred.<job_id>
Outbox publisher (separate container) ──confirmed publish──► RabbitMQ 4.4.1
    exchange jobs ──job.work──► jobs.work.v2 (quorum, delivery-limit 10, DLX jobs.dlx, at-least-once)
    exchange jobs.retry ──<N>s──► jobs.retry.<N>s (TTL delay queues → back to jobs/job.work)
    exchange jobs.dlx (fanout) ──► jobs.dlq (inspection only)
                               └─► jobs.dlq.audit ──(at its limit, at-least-once)──► jobs.dlq.audit.parked
Workers ×2 (same image): consume jobs.work.v2 (prefetch 1, manual ack) + dead-letter auditor on jobs.dlq.audit
    advisory lock per job · CAS state transitions · stage checkpoints · retries via the outbox
Redis 7: worker heartbeats / health (planned: global rate-limit buckets)
```

- **Language and libraries:** Python 3.12. FastAPI/uvicorn, pydantic(-settings), psycopg 3 (async
  pool), aio-pika 10.2.3, redis-py, cryptography (Fernet), python-json-logger. Tests use pytest 9.
- **Infrastructure:** PostgreSQL 16, RabbitMQ `4.4.1-management` (quorum queues; memory override
  512 MiB → watermark 307 MiB), Redis 7. Everything runs in Docker Compose (project `notes-agent`).
- **Planned and not yet installed [PLAN]:**
  - `google-genai` (Gemini);
  - `youtube-transcript-api`;
  - `yt-dlp`;
  - `ffmpeg` (worker image only);
  - `httpx` for Notion;
  - an MV3 plain-JS extension.

---

## 3. Completed phases and tasks

### Committed
| Commit | Content |
|---|---|
| `927ec18` Phase 0 | Project setup: repository, `.gitignore`/`.gitattributes`, `.env.example`, extension key pair (public key in `extension/manifest.json`, private key git-ignored in `.secrets/`), ADRs |
| `49eed18` Phase 1 (65 files) | Gateway (3 endpoints, validation, idempotency keys, `/healthz`), PostgreSQL schema + state-machine trigger (001) and result/error integrity constraints (002), transactional outbox + publisher, encrypted per-job credential mailbox, worker (lock, CAS, checkpoints, crash recovery), Docker Compose, test suite. Tag `phase1-baseline` → `49eed18` |

### Implemented on branch `phase-2` — all uncommitted (see §6)
| Part | Content | Evidence |
|---|---|---|
| Phase 2 Part 1 | Migration 003 (outbox destinations, `jobs.infra_attempts`, `dead_letter_audit` table); topology v2 (`jobs.work.v2`, delay queues, DLX); drain-safe legacy-queue migration (`notesagent/ops/work_queue_migration.py`, `scripts/migrate_work_queue.py`, write-block policy because quorum queues reject `if-empty` deletes) | [TEST] full suite; [RUN] used successfully on the dev stack 2026-11-19 |
| Part 2 | Error classification (ADR-10): `FatalStageError`, `TransientError`, `RateLimited`, `QuotaExhausted`, `RateLimiterUnavailable`; pure HTTP classifier for YouTube/Gemini/Notion; Notion write-outcome detection; API-key redaction in logs | [TEST] 45 classifier + 9 error unit tests |
| Part 3 | Classified retries via the outbox (ADR-11): `MAX_ATTEMPTS` = total executions (5), backoff 10/30/90 s with the last step repeating, Retry-After honoured, uncounted delay hops, new event id per retry (message identity gates stale copies), DEAD_LETTERED with `RETRIES_EXHAUSTED` + DLQ copy, `retry` view in the API, delay-without-counting when PostgreSQL is unavailable | [TEST] 15 integration (incl. 5 SIGKILL crash windows) + 13 unit; crash set 10 consecutive runs 160/160 |
| Phase A Part 1 (= Phase 2 Part 4) | Broker dead-letter auditing (ADR-12): `jobs.dlq.audit` + parking queue (at-least-once), auditor in every worker, origin marker `x-event-type` on every published message, migration 004 (outcomes `stale`, `inconsistent`), audit-limit validation for the carried-over delivery count | [TEST] 43 unit + 17 integration + 12 migration + 1 topology; based on two isolated RabbitMQ probes |
| Deployment | Dev stack moved to Phase 2; migrations 003/004 applied; legacy `jobs.work` removed | [RUN] 2026-11-19, see §4 |
| **Phase C Part 3** | **Take Notes button, submission, polling, progress popup** (ADR-20): content script on `https://www.youtube.com/*` (one button in the player toolbar on `/watch` pages; SPA navigation via `yt-navigate-finish` + `popstate` + 1 s watchdog; per-video state); background `lib/jobs.js` (Notion-connected check, duplicate prevention for active jobs and concurrent clicks, `Idempotency-Key` reused after uncertain failures; one sequential polling loop over `GET /jobs/{id}`: 3 s, 10 s while retrying, backoff ≤ 30 s on errors, stops at terminal states; jobs persisted in `chrome.storage.local` and resumed on every worker start); popup stage sequence, warnings, failures, "View in Notion" (validated Notion URL). No new permissions, no backend change | [TEST] `npm test`: **89 passed** (50 new; 5 consecutive runs 89/89); real gateway + fake Notion cross-check **10/10 PASS** (incl. idempotent replay and real `GET /jobs/{id}` parsing), 0 secrets in logs. Content script tested with a fake DOM (jsdom install was not permitted); not yet checked on real YouTube; no real job submitted; not deployed |
| **Phase C Part 2** | **Extension Notion sign-in** (ADR-19, `extension/`): MV3 module service worker (`lib/auth.js`, `lib/gateway.js`) runs authorize → `launchWebAuthFlow` → callback, checks the redirect URI and state locally, stores only the sealed token + workspace metadata in `chrome.storage.local`; popup with Connect/Disconnect and readable errors; `job:submit` message sends the sealed token to `POST /jobs` (`INVALID_NOTION_TOKEN` → reconnect). Manifest: `identity`, `storage`, host `http://localhost:8000/*`; no CORS | [TEST] `npm test` in `extension/`: **39 passed** (fake chrome + local fake gateway, real fetch); cross-check of the extension code against the **real gateway** + fake Notion token endpoint in a throwaway test container: **8/8 PASS**, 0 secrets in logs. Not loaded in Chrome by me; **no real Notion sign-in**; not deployed |
| **Phase C Part 1** | **Notion OAuth in the gateway** (ADR-18, `gateway/oauth.py`): `POST /oauth/notion/authorize` (Notion URL + single-use state; only its SHA-256 in Redis, `SET NX EX` 600 s) and `POST /oauth/notion/callback` (the extension forwards `code`/`state` from the `chromiumapp.org` redirect; state consumed with `GETDEL`; server-side exchange at `/v1/oauth/token` with HTTP Basic client ID/secret + the exact registered redirect URI, a Notion limiter permit, 15 s timeout). Returns only a **sealed token** (`nva1.` Fernet, HKDF key from `JOB_PAYLOAD_ENCRYPTION_KEY`); `POST /jobs` unseals it into the credential mailbox. Denied / missing / invalid state / 4xx / 401 / 429 / 5xx / network each mapped to a fixed error code; no token, code or secret in responses or logs. No refresh (not documented as required). Also: `NOTION_DEFAULT_PARENT_ID` accepts a page URL; `notion.com` result URLs accepted | [TEST] unit + integration against a fake Notion token endpoint (never the real API); full suite **861 passed** (2026-11-20 23:02); real settings check (booleans only): gateway OAuth configured, redirect URI = extension URI. **No real Notion authorization performed. Not deployed** |
| **Phase B Part 4** | **PUBLISHING stage** (ADR-17, `worker/publish.py`, `worker/notion.py`): `httpx` Notion client (Bearer token from the credential mailbox, `Notion-Version` 2026-03-11, no own retries, a `Provider.NOTION` permit before every request, method-aware error classification). Deterministic page plan: marker (job id) first, summary, key points, timestamped sections with video links, key frames uploaded via the File Upload API with LLM captions (time + caption + reason), text-only when no frames. Destination: job `notion_parent_id` or `NOTION_DEFAULT_PARENT_ID` (else FAILED `NOTION_PARENT_MISSING` before any request). **No duplicate pages:** every attempt reconciles first (child page with the same title whose first block carries the job marker) and writes only the missing suffix of the plan; covers redelivery, 429 retries and uncertain 5xx writes. Result `{notion_page_url, frames_requested, frames_published}`, with frames_published counted from Notion; permanent upload failure → note + `FRAME_UPLOAD_FAILED` warning | [TEST] 19 unit (in-process fake Notion) + 9 integration (real worker + fake Notion server process, including SIGKILL mid-append and after all writes); full suite **825 passed** (2026-11-20 22:37). **No real Notion verification** (no OAuth token exists yet). **Not deployed** |
| **Phase B Part 3** | **EXTRACTING_FRAMES stage** (ADR-16, `worker/frames.py`, `YouTubeClient.download_video`): one `Provider.YOUTUBE` permit + one `yt-dlp` download of a video-only stream (≤ 720p, H.264 preferred, no internal retries, size guard) into a private temp dir → per Gemini timestamp an exact FFmpeg frame (JPEG ≤ 1280 px, never upscaled), validated with `ffprobe` (decodable MJPEG, non-empty, ≤ 5 MiB). Timestamps checked against the real duration. **Only FFmpeg-decoded source frames are stored** (base64 in the checkpoint, sha256), each with its timestamp, Gemini caption/reason and `caption_basis=transcript`. **Graceful degradation:** failed timestamps → warnings `FRAME_EXTRACTION_FAILED` (spec shape) → COMPLETED_WITH_WARNINGS; source not obtainable for non-retryable reasons → text-only. 429 / network / 5xx → existing retries; unavailable → FAILED (ADR-14). Temp dirs removed in every case; leftovers of dead/zombie workers purged. **Docker:** Debian `ffmpeg` 7.1.5 in the base image (≈ 430 MB); **no JS runtime** (verified not needed today) | [TEST] 42 unit (real FFmpeg on synthetic videos) + 7 integration (real worker); full suite **797 passed** (2026-11-20 21:19). [RUN] real `yt-dlp` + FFmpeg on one public video outside the suite: 1 permit, 2 real frames, 1 out-of-range warning, temp dir empty. **Not deployed** (dev image rebuilt, 961 MB) |
| **Phase B Part 2** | **SUMMARIZING stage** (ADR-15, `worker/summarize.py`, `worker/gemini.py`): Gemini `gemini-3.8-flash` (official `google-genai==3.6.1`, SDK retries off, JSON-schema output, thinking `low`) over the TRANSCRIBING checkpoint → validated checkpoint with `summary`, `key_points`, ordered `sections` (`start_sec`), `important_timestamps` (`timestamp_sec`, `caption`, `reason`, `transcript_excerpt`; ≤ `MAX_FRAMES` 8, de-duplicated). **Grounding:** Gemini answers with global transcript segment numbers; the timestamps are those segments' real starts (invented numbers dropped). **Chunking:** in order by `TRANSCRIPT_CHUNK_TOKENS` (12,000, capped by TPM), + 1 merge request when chunked. A Gemini permit before every request + `settle()`. Errors → existing retries (429/RetryInfo + shared cooldown, daily quota → Pacific reset, 5xx/network transient, invalid output bounded → DEAD_LETTERED; 4xx/blocked/missing key → FAILED) | [TEST] 33 unit + 11 integration (real worker, fake Gemini raising real SDK errors); full suite **748 passed** (2026-11-20 20:11). [RUN] config: key loaded and masked (hash-compared, never printed); **one real Gemini request** via the production stage on the stored 'Me at the zoo' transcript: OK, 32.7 s, 417/243 tokens, 1 grounded timestamp. **Not deployed** (dev image rebuilt, containers not recreated) |
| **Phase B Part 1** | **TRANSCRIBING stage** (ADR-14, `worker/transcribe.py`, `worker/youtube.py`): `yt-dlp` metadata (title + true duration) → **constraint A** (> `MAX_VIDEO_DURATION_SEC` 1200 → FAILED `VIDEO_TOO_LONG` before any transcript request; exactly 1200 accepted; invalid/missing duration → FAILED `VIDEO_DURATION_UNKNOWN`) → `youtube-transcript-api` list/choose/fetch → checkpoint with ordered `{start, duration, end, text}` segments + `video_title`/`duration_sec` on the job. A `Provider.YOUTUBE` limiter permit before each of the 3 requests. Library errors → ADR-10 classes (unavailable/private/404/410/no transcript → FAILED; 429/blocking → shared cooldown + outbox retry; network/5xx → bounded retry). Dependencies `yt-dlp==2026.10.26`, `youtube-transcript-api==1.2.5` | [TEST] 40 unit + 11 integration with the real worker and a fake YouTube client; full suite **704 passed** (2026-11-20). [RUN] production stage code + real client against one public video outside the suite: title, 19 s, 6 timestamped segments, 3 permits. **Deployed to the dev stack 2026-11-20 02:06 and verified with a real job** |
| Phase A Part 2 | **Shared external-API rate limiter** (ADR-13, `common/ratelimit.py`): atomic Redis Lua checks on Redis time. Gemini: RPM 4 and input TPM 200,000 (sliding 60 s windows), RPD 16 per Pacific day. Notion: 2.5/s per connection. YouTube: no quota, cooldown only. Shared Retry-After cooldown recorded by the worker's existing failure path; waits ≤ 60 s in-process, otherwise the existing outbox retry; Redis down → `RateLimiterUnavailable` (infra budget), no provider call. Wired into `StageContext.limiter`, settings and the Compose worker | [TEST] 25 tests incl. 3-process RPM/TPM/RPD/Notion, shared cooldown from a real worker, Redis-down worker path; full suite 653 passed (2026-11-20). **Not yet deployed to the dev stack; no real provider calls exist yet** |

Each part has detailed session logs in `docs/session_logs/` (11 files).

---

## 4. Current development and Docker status

[RUN] **Redeployed and verified 2026-11-20 02:06** (Phase B Part 1; see
`docs/session_logs/phaseB_part1_deployment_verification.md`):

| Service | State | Image |
|---|---|---|
| gateway | healthy | `notes-agent-backend:dev` (ID **`8cb661395cde`**: rate limiter + TRANSCRIBING stage) |
| outbox-publisher | healthy | same |
| worker ×2 | healthy | same; `stages=['TRANSCRIBING']`, `MAX_VIDEO_DURATION_SEC=1200`, `TRANSCRIPT_LANGUAGES=en` |
| postgres / rabbitmq / redis | healthy | `postgres:16`, `rabbitmq:4.4.1-management`, `redis:7-alpine` (not recreated in either deployment; restarts 0; volumes preserved) |

- **Database:** migrations `001`–`004` applied. Two job rows exist:
  - the first deployment's smoke test, `FAILED / PIPELINE_STAGE_UNAVAILABLE` at TRANSCRIBING;
  - the Phase B verification job for the real public video `jNQXAC9IVRw`: title "Me at the zoo",
    19 s, a TRANSCRIBING checkpoint with 6 timestamped segments, then SUMMARIZING → `FAILED /
    PIPELINE_STAGE_UNAVAILABLE` (expected).
- **RabbitMQ vhost `/`:**
  - `jobs.work.v2`: 2 consumers.
  - `jobs.dlq.audit`: 2 consumers. Arguments: limit 21, at-least-once, reject-publish, DLX `""` →
    `jobs.dlq.audit.parked`.
  - `jobs.dlq.audit.parked`: limit `-1`.
  - `jobs.dlq`.
  - Delay queues `jobs.retry.{5,10,30,90}s`.
  - The legacy `jobs.work` queue was **removed** with `scripts/migrate_work_queue.py`; no policies are
    left.
- **`GET /healthz`:** HTTP 200 (gateway, PostgreSQL, RabbitMQ, Redis ok; 2 live workers).
- **Functionally (deployed image):**
  - A real job is transcribed: title, true duration, timestamped transcript, constraint A.
  - It then fails honestly at SUMMARIZING (`PIPELINE_STAGE_UNAVAILABLE`), which is not implemented yet.
  - The shared rate limiter is active in both workers.
- **Verification evidence:**
  - Over-20-min rejection checked with real metadata and a lowered limit in a throwaway container
    (`VIDEO_TOO_LONG` after the metadata request only).
  - Logs: no tracebacks or retries; 0 secret-value hits.
  - 124 targeted tests passed after the deployment.

---

## 5. Remaining mandatory requirements (from the compliance audit, re-checked)

| Requirement | Status |
|---|---|
| R1 gateway / broker / ≥ 2 workers | **Done** [TEST][RUN] |
| R1/R2 browser extension (content script, background, OAuth, popup) | **Implemented** (Phase C Parts 2–3, ADR-19/20) [TEST]: content-script button with SPA navigation, OAuth + token storage, submission, status polling, progress popup with View in Notion / warnings / failures. **Not yet verified in real Chrome on YouTube**; no real sign-in or job yet |
| R3 transcript + duration | **Done** (Phase B Part 1, ADR-14) [TEST]; deployed and verified with a real job on the dev stack [RUN] |
| R3 LLM synthesis (bullets, timestamps, captions, chunking) | **Done** (Phase B Part 2, ADR-15) [TEST]; one real Gemini request verified [RUN]; not yet deployed |
| R3 frame extraction | **Done** (Phase B Part 3, ADR-16) [TEST]; one real video verified outside the suite [RUN]; not yet deployed |
| R3 Notion publishing (images, captions, default parent, idempotent against redelivery) | **Implemented** (Phase B Part 4, ADR-17) [TEST] against a fake Notion API; **not verified against real Notion** (needs the OAuth token, Phase C); not deployed |
| R4 lifecycle / state machine | **Done** [TEST] |
| R5 API contract | **Done** [TEST]; a *working* Notion URL can only be verified once publishing exists |
| R6 429 handling | **Mechanism done** with fixture stages [TEST]; no real API calls exist yet |
| R6 crash recovery | **Done for the infrastructure** [TEST][RUN]; "no duplicate Notion page" depends on idempotent publishing (not built) |
| R6 graceful degradation | Result logic present (`frames_published < frames_requested` → WITH_WARNINGS) [SRC][TEST]; the frame stage and text-only page are **not built** |
| R6 DLQ | **Done** (worker exhaustion + broker dead letters) [TEST]; see decision D-1 in §10 |
| R7-A ≤ 20 min / reject longer | **Done** in the TRANSCRIBING stage (FAILED `VIDEO_TOO_LONG` for > 1200 s before transcript extraction; 1200 accepted) [TEST] |
| R7-B real frames + captions | **NOT IMPLEMENTED** |
| R8 README | **NOT IMPLEMENTED** (no README in the repository) |
| R8 compose / health check | **Done** [RUN] |
| R9 global LLM rate limit (no LLM 429 in bursts) | **Limiter implemented and tested** [TEST] (ADR-13). It only takes effect once Phase B stages call `ctx.limiter.acquire()` before each Gemini/Notion/YouTube request; the burst behaviour against the real Gemini API is unverified |

---

## 6. Git state

- **Current (2026-11-20, after Phase B Part 1):**
  - Branch `phase-2`, **HEAD `1aa6863`** ("Complete Phase 2 backend reliability and rate limiting",
    committed by the user; it contains all work up to Phase A Part 2).
  - `main` = `phase1-baseline` = `49eed18`.
  - 0 staged.
  - **Update after Phase B Part 2:** also uncommitted:
    - `backend/notesagent/worker/{gemini,summarize}.py`,
      `backend/tests/support/fake_gemini.py`, `backend/tests/test_{summarize_stage,unit_summarize}.py`;
    - `docs/session_logs/phaseB_part1_deployment_verification.md` and
      `docs/session_logs/phaseB_part2_gemini_summarization.md`;
    - further edits to `config.py`, `stages.py`, `requirements.*`, `docker-compose.yml`,
      `.env.example`, `docs/decisions.md`, `tests/support/{fake_youtube,run_youtube_worker}.py`,
      `tests/test_{worker,transcribe_stage}.py`.

    `git status`: 24 entries, 0 staged.
  - Uncommitted Phase B Part 1 changes:
    - **modified:** `.env.example`, `backend/notesagent/common/config.py`,
      `backend/notesagent/worker/stages.py`, `backend/requirements.in`, `backend/requirements.txt`,
      `backend/tests/test_worker.py`, `docker-compose.yml`, `docs/api.md`, `docs/decisions.md`,
      this report;
    - **untracked:** `backend/notesagent/worker/{transcribe,youtube}.py`,
      `backend/tests/support/{fake_youtube,run_youtube_worker}.py`,
      `backend/tests/test_{transcribe_stage,unit_transcribe}.py`,
      `docs/session_logs/phaseB_part1_transcript_stage.md`.

  The entries below are historical.
- **Branch:** `phase-2`. **HEAD (before the user's commit):** `49eed183fc8b9a040c170aed5d691ef6e0bbd33a`
  (= `main` = tag `phase1-baseline`). History: `927ec18` Phase 0, `49eed18` Phase 1.
- **Update 2026-11-20 (after Phase A Part 2):** still no new commit (HEAD `49eed18`), but the index was
  changed outside the assistant during that session: **65 paths staged**, while HEAD is unchanged.
  - Files edited after that staging are partially staged: `MM backend/tests/support/fixture_stages.py`,
    `MM docs/decisions.md`, `AM docs/session_logs/phaseA_part2_shared_rate_limiter.md`.
  - Files created after it are untracked: `backend/tests/support/limiter_client.py`,
    `backend/tests/test_ratelimit.py`.
  - Also new in Phase A Part 2: `backend/notesagent/common/ratelimit.py` and edits to
    `backend/notesagent/common/config.py`, `backend/notesagent/worker/{runner,stages}.py`,
    `docker-compose.yml` and `.env.example`.
  - Re-run `git status` before committing so the commit contains the latest versions.

  The file lists below describe the state before that task.
- **Staged (before Phase A Part 2):** none. **All Phase 2 / Phase A work is uncommitted.** The user
  handles all commits; no AI co-author trailers.
- **Modified tracked files (25):**
  - `.env.example`, `.gitignore`, `docker-compose.yml`;
  - `backend/infra/rabbitmq/rabbitmq.conf`;
  - `backend/notesagent/common/{broker,config,errors,jobs_repo,logging}.py`;
  - `backend/notesagent/gateway/routes.py`, `backend/notesagent/outbox/publisher.py`,
    `backend/notesagent/worker/runner.py`;
  - `backend/tests/conftest.py`;
  - `backend/tests/support/{fixture_stages,helpers,rabbit}.py`;
  - `backend/tests/test_{api,concurrency,mailbox_races,migrations,outbox,results_and_dlq,worker}.py`;
  - `docs/api.md`, `docs/decisions.md`.
- **Deleted (2, intentionally replaced in Part 3):** `backend/tests/support/run_worker_crash_after_retry_publish.py`,
  `backend/tests/test_retry_duplicate_window.py`.
- **Untracked (35 files, including this report):**
  - migrations `003`, `004`;
  - `common/http_errors.py`;
  - `ops/{__init__,rabbitmq_mgmt,work_queue_migration}.py`;
  - `worker/{dead_letters,dlq_audit,retry_policy}.py`;
  - test support `{diagnostics,legacy,run_worker_hooks}.py`;
  - tests `test_{dead_letter_audit,migration_003,migration_004,retries,topology_v2,unit_dead_letters,unit_errors,unit_http_classifier,unit_retry_policy,work_queue_migration}.py`;
  - `scripts/migrate_work_queue.py`;
  - 11 session logs;
  - this report.
- **Git-ignored and never to be committed:** `.env` (secrets), `.secrets/` (extension private key),
  `backend/test-artifacts/` (diagnostics and reliability evidence).

---

## 7. Architecture decisions (`docs/decisions.md`)

| ADR | Decision |
|---|---|
| 01 | Four components + PostgreSQL, Redis and the outbox publisher in Compose; one worker image scaled with `--scale`; ffmpeg/yt-dlp only inside the worker image |
| 02 | Transactional outbox: job + outbox row in one transaction; the publisher uses confirms and `SKIP LOCKED`; duplicates possible, consumers idempotent |
| 03 | The Notion token is never stored in PostgreSQL. It is Fernet-encrypted into a per-job durable RabbitMQ queue `cred.<job_id>` (TTL 24 h), read non-destructively, and deleted at terminal state. **Explicitly: the encrypted token is temporarily persisted by the broker** |
| 04 | Optional `Idempotency-Key` on `POST /jobs` |
| 05 | Manual ack after the durable commit; advisory lock + CAS; bounded retries; quorum `x-delivery-limit` as a backstop; broker pinned to 4.4.1 |
| 06 | Gemini Developer API, **free tier only**, model `gemini-3.8-flash`, no paid fallback; global Redis limiter planned (RPM, input TPM, RPD reset at midnight Pacific; shared cooldown) |
| 07 | Notion public integration with OAuth; code exchange/refresh **on the gateway only**; redirect candidate `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion` (**unverified**); default parent `NOTION_DEFAULT_PARENT_ID`; resumable publishing without duplicate pages (design) |
| 08 | Stable extension ID `kkkeigfecmfhmainhbkcpdhjnloifbdf` from the manifest key |
| 09 | Topology v2 and the drain-safe migration via a write-block policy (Option B) |
| 10 | Error classification (FAILED = cannot succeed; retryable classes with budgets); YouTube 404/410 = fatal (decision D3) |
| 11 | Retries through the outbox; message-identity gating; only `nack(requeue=True)` is uncounted (`reject(requeue=True)` is counted — measured) |
| 12 | Broker dead-letter auditing with the parking queue; newest `x-death`; origin marker; dedup key; `stale` / `inconsistent` outcomes |
| 17 | PUBLISHING: httpx Notion client with a permit per request; deterministic page plan with a job marker; reconcile before every write (no duplicate pages, resume the plan suffix); File Upload API for real frames with LLM captions; text-only fallback; frames_published counted from Notion |
| 16 | EXTRACTING_FRAMES: one yt-dlp video-only download (permit) + exact FFmpeg frames validated by ffprobe; only real source frames stored (base64 + sha256) with transcript-based captions; failed timestamps → warnings, unobtainable source → text-only; FFmpeg in the image, no JS runtime |
| 15 | SUMMARIZING: Gemini via `google-genai` (SDK retries off), numbered transcript lines → segment numbers → real segment start times (grounded); pydantic-validated notes; ordered token-budget chunking + merge request; permit + settle per request; classified errors into the existing retries; no partial-chunk persistence |
| 14 | TRANSCRIBING: yt-dlp metadata (true duration, constraint A before any transcript request) + youtube-transcript-api; limiter permit per request; library errors → ADR-10 classes; **D-1 settled: unavailable/private/deleted → FAILED** (DLQ demo uses retryable failures); yt-dlp JS-runtime deprecation noted |
| 20 | Extension Take Notes: content script button synced with YouTube SPA navigation; background submission with duplicate prevention + Idempotency-Key reuse after uncertain failures; one sequential polling loop on `GET /jobs/{id}` with backoff, persisted jobs resumed on every worker start (no `alarms`); popup stage sequence, warnings, failures, validated "View in Notion" link |
| 19 | Extension sign-in in the background service worker only; local redirect-URI + state checks; sealed token in `chrome.storage.local`, last error in `storage.session`; permissions `identity`, `storage`, host `http://localhost:8000/*`; no CORS |
| 18 | Notion OAuth: Mode A redirect to the extension, which forwards code + state; gateway-only exchange with the client secret; single-use hashed state in Redis; sealed token returned to the extension and unsealed into the mailbox on `POST /jobs`; no refresh; fixed error codes |
| 13 | Shared external-API rate limiter: atomic Redis Lua on Redis time. Gemini RPM/input-TPM sliding windows + RPD per Pacific day (values used as given, already × 0.8); Notion spacing per connection; YouTube cooldown only. Shared Retry-After cooldown reported by the worker; waits ≤ `RATE_LIMIT_MAX_WAIT_SEC`, else the existing outbox retry; Redis down → `RateLimiterUnavailable` |

---

## 8. External APIs, models, credentials and configuration (no secret values)

| Service | Method / model | Credential | Status |
|---|---|---|---|
| YouTube transcript + duration | `youtube-transcript-api==1.2.5` (no key; unofficial web-client interface) + `yt-dlp==2026.10.26` metadata — ADR-14; settings `MAX_VIDEO_DURATION_SEC` (1200), `TRANSCRIPT_LANGUAGES` (en), `YOUTUBE_REQUEST_TIMEOUT_SEC` (30) | none | **Implemented** (Phase B Part 1); not deployed |
| Frames | `yt-dlp` video-only download (≤ 720p) + `ffmpeg -ss T -frames:v 1` (ADR-16); settings `FRAME_SOURCE_MAX_HEIGHT` (720), `FRAME_MAX_WIDTH` (1280) | none | **Implemented** (Phase B Part 3); FFmpeg 7.1.5 in the image; not deployed |
| Gemini | `gemini-3.8-flash`, thinking level `low`, free tier | `GEMINI_API_KEY` (from Google AI Studio, project **without billing**) | **Set by the user in `.env`; loaded and verified** (hash-compared, masked; one real request OK on 2026-11-20). Limits per ADR-13: 4 RPM / 200k TPM / 16 RPD. SUMMARIZING implemented (ADR-15) |
| Notion | REST API (httpx), File Upload API, OAuth (ADR-18) | `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`, `NOTION_REDIRECT_URI` (gateway only), `NOTION_DEFAULT_PARENT_ID` (worker) | **Set by the user in `.env`** (2026-11-20); presence verified without printing values; redirect URI = `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion`; default parent is a page URL (parsed). No real authorization yet |

**Documented external limits** (read 2026-11-19, see `docs/session_logs/external_services_credentials_inspection.md`):
- **Gemini:** RPM / TPM / RPD **per project** (not per key); the RPD reset is at midnight Pacific.
  **Free-tier numbers are not published** — they are shown only at `aistudio.google.com/rate-limit`.
- **Notion:**
  - 180 requests/minute per connection (avg 3/s, bursts allowed), plus an unpublished per-workspace
    limit; 429 with `Retry-After`.
  - ≤ 100 array elements per request, ≤ 500 KB payload, ≤ 2000 characters per rich-text item.
  - **Free workspace: 5 MiB per uploaded file**; uploads must be attached within 1 h.
  - Refresh tokens exist.
- **YouTube transcript library:** no documented limits; YouTube blocks cloud IPs (and possibly
  heavy home IPs); age-restricted videos are inaccessible. The official Data API `captions.download`
  needs OAuth + edit permission (not usable).

**Configuration wiring:**
- The `GEMINI_*`, `NOTION_*`, `MAX_VIDEO_DURATION_SEC`, `MAX_FRAMES`, `TRANSCRIPT_CHUNK_TOKENS`,
  `PUBLIC_BASE_URL` and `ALLOWED_EXTENSION_ORIGIN` variables exist in `.env.example` / `.env`, but
  **no code reads them and Compose does not pass them**.
- Retry and broker settings that are wired: `MAX_ATTEMPTS` (5), `INFRA_MAX_ATTEMPTS` (20),
  `BACKOFF_SCHEDULE_SEC` (10,30,90), `LOCK_RETRY_DELAY_SEC` (5), `INFRA_RETRY_DELAY_SEC` (30),
  `QUEUE_DELIVERY_LIMIT` (10), and the derived audit limit (21).
- Rate-limiter settings wired since Phase A Part 2 (settings + Compose worker; defaults also apply when
  `.env` values are empty): `GEMINI_RPM_LIMIT` (4), `GEMINI_TPM_LIMIT` (200000), `GEMINI_RPD_LIMIT` (16),
  `NOTION_RPS_LIMIT` (2.5), `RATE_LIMIT_MAX_WAIT_SEC` (60), `RATE_LIMIT_DEFAULT_COOLDOWN_SEC` (10).
  `GEMINI_LIMIT_SAFETY_FACTOR` and `LLM_MAX_WAIT_SEC` were removed from `.env.example`: the given limits
  already include the 0.8 margin. The user's `.env` still contains them; they are ignored.

---

## 9. Test suites and latest verified results

All tests run in Docker against real PostgreSQL/RabbitMQ/Redis using isolated test resources
(database `notes_test`, vhost `notes_test`, Redis db 15):
`MSYS_NO_PATHCONV=1 docker compose --profile test run --rm --no-deps tests pytest`.

| Run | Date / time (host clock) | Result |
|---|---|---|
| **Full suite (latest, after Phase C Part 1)** | 2026-11-20 22:50–23:02 | **861 passed**, 0 failed, 0 errors (741.57 s), incl. the new `test_unit_oauth.py` + `test_oauth.py` cases (36 new) |
| Full suite (after Phase B Part 4) | 2026-11-20 22:25–22:37 | 825 passed, 0 failed, 0 errors (717.59 s), incl. 19 unit (`test_unit_publish.py`) + 9 integration (`test_publish_stage.py`) publishing tests |
| Full suite (after Phase B Part 3) | 2026-11-20 21:08–21:19 | 797 passed, 0 failed, 0 errors (657.90 s), incl. 42 unit (`test_unit_frames.py`, real FFmpeg) + 7 integration (`test_frames_stage.py`) frame tests |
| Full suite (after Phase B Part 2) | 2026-11-20 20:00–20:11 | 748 passed, 0 failed, 0 errors (640.58 s), incl. 33 unit (`test_unit_summarize.py`) + 11 integration (`test_summarize_stage.py`) summarization tests. (An earlier run that day had 4 failures in `test_ratelimit.py` from a config validator added in Phase B Part 2; fixed, see that session log) |
| Full suite (after Phase B Part 1) | 2026-11-20 01:49–02:00 | 704 passed, 0 failed, 0 errors (627.78 s), incl. 40 unit (`test_unit_transcribe.py`) + 11 integration (`test_transcribe_stage.py`) transcript tests |
| Full suite (after Phase A Part 2) | 2026-11-20 01:15–01:26 | 653 passed, 0 failed, 0 errors (658.35 s), including the 25 new rate-limiter tests (`test_ratelimit.py`: 25 passed in 32.36 s) |
| Full suite (before Phase A Part 2) | 2026-11-19 22:46–22:56 | 628 passed, 0 failed, 0 errors (617.65 s) |
| Targeted integration set after deployment (dead-letter audit, topology, migration 004, health, API, worker, outbox, work-queue migration) | 2026-11-19 23:28–23:34 | **105 passed** |
| Crash set (16 crash/SIGKILL tests) × 10 consecutive runs | 2026-11-19 | **160/160 passed** |
| Previous full-suite baseline before Phase A Part 1 | 2026-11-19 | 555 passed |

Test files (counts of test functions; parametrised cases expand further):
- `api` 16, `concurrency` 2, `health` 6, `idempotency` 6, `mailbox` 3, `mailbox_races` 5;
- `migrations` 17, `migration_003` 10, `migration_004` 4 (12 cases), `outbox` 4;
- `results_and_dlq` 7, `retries` 15, `topology_v2` 7, `work_queue_migration` 22, `worker` 4,
  `dead_letter_audit` 16 (17 cases);
- unit: `crypto_logging` 9, `errors` 9, `http_classifier` 45, `retry_policy` 13, `states` 6,
  `validation` 5, `dead_letters` (43 cases).

**What the tests do NOT cover:** any real YouTube, Gemini, frame or Notion behaviour, and the
extension. All stage behaviour is exercised with fixture stages.

---

## 10. Known bugs, limitations and unresolved decisions

### Unresolved decisions (need the user)
- **D-1 (spec deviation risk):** the spec suggests a *private/unavailable video* as the natural DLQ
  trigger, but ADR-10 D3 classifies YouTube 404/410 as **fatal → FAILED after one attempt**, never DLQ.
  Options:
  - (a) make it retryable so it reaches DEAD_LETTERED (≈ 6 s with demo profile `2,4,8` / 3 attempts);
  - (b) keep FAILED and document another DLQ trigger.

  **Settled 2026-11-20 by the user's Phase B Part 1 instruction: FAILED** (option b; ADR-14). The DLQ
  demo must use a retryable failure (e.g. persistent network/5xx errors with the demo profile).
- **Notion redirect URI:** the `chromiumapp.org` URI is registered and configured (ADR-18), but no real
  authorization has run yet (approval required, Phase C Part 2).
- **Transcript method:** no ADR yet.

### Limitations and risks (no known failing test)
1. **Historical intermittent failures, cause unknown:**
   - crash-test "repetition 2" (2 failures + 1 setup/teardown error, 502 s, output lost) never
     reproduced afterwards (160/160);
   - one Phase 1 mailbox-test failure, also unexplained.

   Both are accepted as residual uncertainty.
2. During a long PostgreSQL outage, retries are delayed without being counted and without an upper
   bound (approved P3-D4).
3. Broker dead letters with inconsistent or missing metadata, `stale` outcomes, and parked messages
   leave the job unchanged. They need manual inspection; there is no inspection CLI or replay (out of
   scope, Q5).
4. `jobs.dlq` and the parking queue have no retention limit. `jobs.dlq` has the default quorum delivery
   limit of 20 and no DLX, so inspection must use get + `nack`, never repeated crashes.
5. A graceful worker restart while it holds an audit message probably counts toward the audit queue's
   limit [NV]. This is covered by the 10-crash margin and the parking queue.
6. aio-pika/aiormq always assign a `message_id`; ID-less messages come only from foreign publishers.
7. The YouTube library is unofficial and can be blocked; age-restricted videos are unsupported.
8. Gemini free-tier limits are unknown until the user reads them in AI Studio. A 429 cannot be made
   impossible, only handled.
9. The dev `.env` lacks the Phase 2 variables (Compose defaults apply) and still contains an unused
   `BACKOFF_JITTER` line. It was not edited because it holds secrets.
10. The old Phase 1 image (`b49bf637…`) is dangling.
11. Minor: the gateway imports `worker.retry_policy.public_retry_view` (layering; kept deliberately).
12. Single-node RabbitMQ; behaviour across broker restarts or leader changes was not tested.

---

## 11. Recommended next task

**Update after Phase C Part 3 (Take Notes, polling, progress popup, ADR-20):** the extension is
functionally complete for the spec's §2. Next:
1. **With the user's approval:** deploy the current image (gateway with OAuth + all four stages; the dev
   stack still runs the Phase B Part 1 image). Then load the extension, do the first real Notion
   sign-in, and run the first real end-to-end job on a short public video. Check: button placement on
   real YouTube, the stage sequence in the popup, the Notion page (summary, timestamps, frames with
   captions), and the View in Notion link.
2. **Phase D:** the README (base URL, default Notion destination, every required env var, how to load
   the extension), and demos of the §6 failure modes, worker scaling and crash recovery, and burst
   submission without an LLM 429.

**Earlier update after Phase C Part 2 (extension sign-in, ADR-19):** the extension side of the sign-in is
built and tested (39 Node tests + 8/8 cross-check against the real gateway with a fake Notion). Next:
1. **With the user's approval:** deploy the current image (gateway with OAuth, all four stages), load
   the unpacked extension, and do the **first real Notion sign-in**.
2. **Phase C Part 3:** the content-script "Take Notes" button (SPA navigation) → `job:submit`; status
   polling in the background; the progress popup (Queued → … → Done, "View in Notion", warnings and
   failures). Then the first real end-to-end job.

**Earlier update after Phase C Part 1 (gateway Notion OAuth, ADR-18):** implemented and tested against a fake
Notion token endpoint; not deployed; no real authorization. The next task is **Phase C Part 2**:
- the extension's `launchWebAuthFlow`: call `/oauth/notion/authorize`, forward `code`/`state` (or `error`)
  from the redirect URL to `/oauth/notion/callback`, and store the sealed token in `chrome.storage.local`;
- `host_permissions` for the gateway origin (CORS is not wired; it is not needed from the service worker);
- then, with the user's approval, deploy the current image and run the first real authorization and the
  first real end-to-end publish.

The text below predates Phase C.

**Phase B Part 4 (PUBLISHING) is implemented** — see §3 and ADR-17. Evidence: 28 new tests and the full
suite of 825 passed, all against a fake Notion API. **The whole backend pipeline now exists**
(TRANSCRIBING → SUMMARIZING → EXTRACTING_FRAMES → PUBLISHING), but it has **not** been run against the real
Notion API, because no OAuth token exists yet. Phase B Parts 1–4 are uncommitted (HEAD `1aa6863`).

1. **The user reviews and commits** Phase B Parts 1–4, the extension probe and the inspection logs (41
   status entries, nothing staged).
2. **The user completes the redirect-URI check** (`extension/README.md`): load the unpacked extension,
   read `getRedirectURL("notion")`, register it in the "Video Notes Agent" integration, and fill
   `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`, `NOTION_REDIRECT_URI`, `NOTION_DEFAULT_PARENT_ID` in the
   local `.env` (never in chat).
3. **Next mandatory development task — Phase C, part 1: Notion OAuth** (gateway code exchange/refresh
   with the client secret server-side, CORS for `ALLOWED_EXTENSION_ORIGIN`, extension
   `launchWebAuthFlow` + token storage). This yields the first real token, which allows the **first
   real end-to-end publish** (verify the page, images, captions and no duplicates on the real API).
   After that: the rest of Phase C (content-script button, popup progress) and Phase D (README, demos,
   deployment).
