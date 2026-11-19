# Session log — Project 2 specification compliance audit (read-only)

- **Date:** 2026-11-19
- **Authoritative source:** `PROJECT_SPEC.pdf` (repository root), extracted with `pdftotext -layout`.
- **Branch:** `phase-2`; HEAD = `main` = `phase1-baseline` = `49eed18`. All Phase 2 work is uncommitted.
- **Nature:** audit only. The only file written is this log. No code, tests, migrations, configuration,
  Docker files, documentation or earlier session logs were changed; nothing was staged or committed.

Evidence labels: **[SRC]** source inspection · **[TEST]** automated test · **[RUN]** runtime observation ·
**[NV]** not independently verified.

## 1. Executive verdict
**Serious gaps: the backend reliability foundation is strong, but the product does not exist yet.**

**Built and verified:**
- gateway with the 3 REST endpoints;
- durable broker;
- ≥2 workers;
- job state machine;
- crash recovery;
- classified retries with bounded backoff;
- worker-side DEAD_LETTERED + DLQ;
- `/healthz`.

**Not built:**
- every pipeline stage: transcript, duration check, LLM, captions, frames, Notion;
- the browser extension (only `manifest.json` with a key);
- the global rate limiter;
- the README.

Today every submitted job ends `FAILED / PIPELINE_STAGE_UNAVAILABLE`, by design. Roughly half of the
graded criteria (§9: client experience, latency, constraints A and B, no LLM 429s) depend entirely on
unbuilt components.

**One specification deviation needs a decision:** the spec's suggested DLQ trigger (private/unavailable
video) is classified as fatal (→ FAILED, never DLQ) — see §4 D-1.

**One area of over-engineering:** the approved Part 4 design (audit consumer + parking queue +
migration 004) is far heavier than the spec requires — see §5.

## 2. Requirement-by-requirement compliance

| # | Requirement (spec §) | Status | Evidence | Missing work / concerns |
|---|---|---|---|---|
| 1a | API Gateway, separate process, does no pipeline work (§1.1) | IMPLEMENTED AND VERIFIED | [SRC] `gateway/routes.py`, `submission.py` (no pipeline code); [TEST] `test_api.py`; [RUN] `docker compose ps`: gateway healthy | — |
| 1b | Durable broker, at-least-once, redelivery on crash (§1.1) | IMPLEMENTED AND VERIFIED | [SRC] `common/broker.py` (quorum, persistent, confirms, manual ack); [TEST] `test_worker.py` SIGKILL, `test_results_and_dlq.py`, `test_retries.py` crash windows; [RUN] 10-run crash series 160/160 (earlier log) | — |
| 1c | ≥2 independent workers (§1.1, §8) | IMPLEMENTED AND VERIFIED | [SRC] compose `worker` `replicas: ${WORKER_REPLICAS:-2}`; [RUN] 2 worker containers healthy, `/healthz` `live_workers: 2` | — |
| 1d | Browser extension as a separate component (§1.1) | NOT IMPLEMENTED | [SRC] `extension/manifest.json` only (name, version, description, key) | everything in row 2 |
| 2a | Content script: "Take Notes" button in player, video id, survives SPA navigation (§2) | NOT IMPLEMENTED | [SRC] no content script | must be built |
| 2b | Background script: one-time Notion OAuth, stores token, submits jobs, relays status (§2) | NOT IMPLEMENTED | [SRC] none. Gateway accepts `notion_token` per request [TEST] `test_unit_validation.py`, `test_api.py`; OAuth design in ADR-07 [NV] | OAuth flow (needs the Notion integration, which may only be created with the user's approval), token storage, submission, polling |
| 2c | Popup: human-readable progress Queued→…→Done, "View in Notion", clear failures/warnings, no raw JSON (§2) | NOT IMPLEMENTED | [SRC] none | must be built |
| 3a | Timestamped transcript (§3.1) | NOT IMPLEMENTED | [SRC] `worker/stages.py`: `build_stage_registry()` returns `{}` | Phase 3 |
| 3b | True video duration learned; Constraint A enforced (§3.1, §7A) | NOT IMPLEMENTED | [SRC] `MAX_VIDEO_DURATION_SEC=1200` appears only in `.env.example`; not read by `config.py` or any code. `test_api.py:160` only forces a `VIDEO_TOO_LONG` error into the DB to test the response shape | duration lookup + deterministic rejection |
| 3c | LLM summary bullets + important timestamps + a caption per timestamp; chunk and merge long transcripts in order (§3.2) | NOT IMPLEMENTED | [SRC] no Gemini client; `requirements.in` has no LLM SDK | Phase 3 |
| 3d | Real frame capture at each timestamp (§3.3, §7B) | NOT IMPLEMENTED | [SRC] no ffmpeg/yt-dlp; `backend/Dockerfile` installs no ffmpeg | Phase 4 |
| 3e | Notion page: headings, bullets, images with captions (§3.4) | NOT IMPLEMENTED | [SRC] no Notion client | Phase 5 |
| 4a | Lifecycle states and transitions (§4) | IMPLEMENTED AND VERIFIED | [SRC] `common/states.py`, 001 trigger (allowed transitions, terminal immutability); [TEST] `test_unit_states.py`, `test_migrations.py` | — |
| 4b | `POST /jobs`: validates URL, 202 + `Location`, body `{job_id, status, video_id}`, no pipeline work, 4xx `{"error":{code,message}}` (§5) | IMPLEMENTED AND VERIFIED | [SRC] `routes.py:58-68`, `validation.py`, `submission.py:70`; [TEST] `test_api.py`, `test_unit_validation.py`, `test_idempotency.py` | Additive `Idempotency-Key` support (not required). Token is not stored by the gateway (encrypted per-job mailbox, ADR-03) |
| 4c | `notion_parent_id` optional; default destination stated in README (§5) | PARTIALLY IMPLEMENTED | [SRC] optional field, UUID-validated; `NOTION_DEFAULT_PARENT_ID` only in `.env.example` | README + use in the publishing stage |
| 4d | `GET /jobs/{id}`: read-only, required fields, 404 unknown (§5) | IMPLEMENTED AND VERIFIED | [SRC] `routes.py:29-44,71-73` (SELECT only); [TEST] `test_api.py` (incl. polling does not mutate) | `video_title` / `duration_sec` stay null until a transcript stage sets them. Additive fields `video_id`, `retry`, `created_at`, `updated_at` |
| 4e | `GET /jobs/{id}/results`: 409 before terminal; absolute Notion URL on success (§5) | IMPLEMENTED BUT NOT FULLY VERIFIED | [SRC] `routes.py:76-89`; result validation `runner.validate_publish_result`, migration 002; [TEST] `test_api.py`, `test_results_and_dlq.py` | A *working* Notion URL cannot be verified until Phase 5 |
| 5a | HTTP 429 from all three APIs → re-queued with exponential backoff; user sees waiting, not error (§6) | PARTIALLY IMPLEMENTED | [SRC] `http_errors.py` (429 → RateLimited for YouTube/Gemini/Notion), `retry_policy.py` (10, 30, 90 s, last repeats), `retry` view in API; [TEST] `test_unit_http_classifier.py`, `test_retries.py` (fixture stages raising `RateLimited`) | No real API calls exist, so no real 429 is ever caught; stages must raise classified errors |
| 5b | Worker crash: unacked message reassigned; no job lost; no Notion duplicate; ack after durable stage; idempotent stages (§6) | PARTIALLY IMPLEMENTED | [SRC] ack after commit, advisory lock, CAS, checkpoints, message gating; [TEST] SIGKILL tests in `test_worker.py`, `test_retries.py`, `test_concurrency.py`; [RUN] 160/160 | Verified only with fixture stages. Stage idempotency (especially Notion "no duplicate page") is Phase 5 work. Crash **loops** reaching the delivery limit leave the job non-terminal (Part 4) |
| 5c | Graceful degradation: partial/all frame failures still publish, COMPLETED_WITH_WARNINGS (§6) | PARTIALLY IMPLEMENTED | [SRC] `runner.py:354-364` (frames_published < frames_requested → WITH_WARNINGS; warnings carried); [TEST] `test_api.py` / `test_results_and_dlq.py` result validation | Frame stage and text-only publishing do not exist |
| 5d | Bounded retries → DLQ, bounded/configurable/demonstrable (§6) | IMPLEMENTED BUT NOT FULLY VERIFIED | [SRC] `MAX_ATTEMPTS`, `BACKOFF_SCHEDULE_SEC`, demo profile documented in `.env.example`; `jobs_repo.dead_letter` + outbox copy to `jobs.dlq`; [TEST] `test_retries.py::test_backoff_schedule_last_step_repeats_then_dead_letter`, `test_results_and_dlq.py` | **Deviation D-1** (unavailable video → FAILED, not DLQ). Broker-initiated dead letters do not update PostgreSQL (Part 4 gap) |
| 6a | Constraint A: ≤20 min supported, longer rejected deterministically (§7) | NOT IMPLEMENTED | see 3b | — |
| 6b | Constraint B: only real frames, each with an LLM caption; failed frames omitted, never substituted (§7) | NOT IMPLEMENTED | [SRC] `validate_publish_result` checks counts only | Phases 3–5 |
| 7a | Unpacked extension loadable without a build step (§8) | NOT IMPLEMENTED | manifest only | — |
| 7b | One compose file: gateway, broker, ≥2 workers; scaling/stop demonstrable (§8) | IMPLEMENTED AND VERIFIED | [SRC] `docker-compose.yml`; [RUN] stack up 11 h, all healthy | Image lacks ffmpeg (Phase 4) |
| 7c | README: base URL, default Notion destination, all env vars incl. LLM key/model, rate limit, OAuth credentials, broker URL, retry params (§8) | NOT IMPLEMENTED | [SRC] no README anywhere | must be written. `.env.example` lists the variables, but many are placeholders not yet read by code (GEMINI_*, NOTION_*, MAX_FRAMES, MAX_VIDEO_DURATION_SEC, CHAOS_ENABLED) |
| 7d | Health check: gateway, broker, state store (§8) | IMPLEMENTED AND VERIFIED | [SRC] `gateway/health.py`; [TEST] `test_health.py`; [RUN] `curl /healthz` → `{"status":"ok", postgres ok, rabbitmq ok (2 consumers), redis ok (2 live workers)}` | — |
| 8 | Respect the rate limits of every external API; queue drains without an LLM 429 (§1, §9) | NOT IMPLEMENTED | [SRC] no limiter; Redis used only for heartbeats/health | Global limiter across workers (how: §10 design choice) |

§10 design choices (not graded as requirements): RabbitMQ (choice), PostgreSQL state model, one worker
doing all stages, Gemini free tier (ADR-06), extension tooling — all legitimate choices.

## 3. Implemented architecture and verified behaviour
- **Services** (compose): postgres 16, rabbitmq 4.4.1, redis 7, migrate (one-shot), gateway,
  outbox-publisher, worker ×2, tests (profile).
- **Submission:** validate → encrypt the Notion token into a per-job RabbitMQ credential mailbox → one
  PostgreSQL transaction (job + event + outbox row) → 202. The outbox publisher delivers with confirms.
- **Worker:** advisory lock per job, CAS state transitions, stage checkpoints, ack after commit,
  terminal-message drop, credential cleanup.
- **Retries (Part 3):** classified errors, attempts budget, delay queues, message-identity gating,
  DEAD_LETTERED + DLQ copy through the outbox.
- **Verified by tests:** the full suite (see §8) and earlier reliability runs (160/160 crash executions).
- **Not exercised end-to-end:** any real video, LLM, frame or Notion behaviour — the worker has no
  stages, so only fixture stages (tests) ever complete a job.

## 4. Deviations and correctness risks
- **D-1 (deviation, needs a decision):** spec §6 names "submitting a private/unavailable video" as the
  natural way to trigger the DLQ. ADR-10 D3 / `http_errors.py:296` classify YouTube 404/410 (unavailable)
  as **fatal → FAILED after 1 attempt**, so that demo produces FAILED, not DEAD_LETTERED. Both readings
  are defensible (FAILED = "non-retryable error" in §4), but the grader's likely demo fails. Options:
  (a) classify unavailable/private as retryable so it reaches DEAD_LETTERED after `MAX_ATTEMPTS`
  (demo profile `2,4,8` / 3 attempts ≈ 6 s); (b) keep FAILED and document another DLQ trigger in the
  README. Recommendation: (a).
- **D-2 (gap):** a job whose message reaches the broker delivery limit (crash loop) stays non-terminal
  in PostgreSQL and polls as "running" forever (Part 4 problem statement).
- **D-3 (risk):** "no job duplicated on Notion" needs page-level idempotency in Phase 5 (the
  `notion_write_outcome_unknown` reconciliation already recorded as a requirement).
- **D-4 (risk):** the Docker image has no ffmpeg / video download tool; adding them affects image size
  and needs the approved dependency process.
- **D-5 (risk):** `.env.example` advertises variables nothing reads yet; a README written from it would
  currently be inaccurate.
- **D-6 (risk):** the dev stack runs Phase 1 images; Phase 2 is not deployed and `jobs.work` (Phase 1
  queue) is still live in vhost `/` [RUN].
- **D-7 (process risk):** all Phase 2 work (≈40 files) is uncommitted on `phase-2`.

## 5. Complexity assessment

| Mechanism | Category | Assessment |
|---|---|---|
| Durable queue, manual ack after commit, ≥2 workers | Required (§1.1, §6) | keep |
| Job state machine + DB trigger | Reasonable (§4, §10 choice) | keep |
| Advisory lock + CAS + checkpoints | Reasonable — needed for "safe to repeat" and no duplicates (§6) | keep |
| Transactional outbox | Reasonable — prevents "accepted but never enqueued" (no job silently lost); a simpler publish-after-commit leaves a loss window | keep |
| Retry classification + backoff + delay queues | Required in substance (§6: 429 backoff, bounded retries, DLQ) | keep; the classifier breadth (45 unit tests) is more than strictly needed but already built |
| Message-identity gating (retry.event_id) | Reasonable — keeps the attempt count exact under duplicates | keep |
| Separate infra budget (`infra_attempts`) | Supports the limiter's fail-closed path | keep, small |
| Encrypted per-job credential mailbox (ADR-03) | Reasonable but heavier than needed: the spec only says the gateway must not *store* the token. Chosen to keep the token out of PostgreSQL (user decision) | keep (approved); do not extend |
| Idempotency keys (ADR-04) | Not required; useful against double clicks | built and tested; keep, do not extend |
| Topology v2 + drain-safe work-queue migration with write-block policy, management-API client (`ops/`, 22 tests) | **Adds complexity without helping a graded requirement.** It exists only because the dev broker already had a Phase 1 queue. A grader starts from an empty broker where `jobs.work` never exists | keep the built code, but the simplest way to close D10 is a fresh dev broker volume (equivalent outcome, no live messages to protect — [RUN] 0 messages) — needs your decision |
| Redis global rate limiter (Part 5, not built) | Required in substance: §9 "queue should drain without ever triggering a 429 from the LLM provider" with ≥2 workers; §10 leaves *how* to us | build, minimal: Gemini RPM (+ RPD) and Notion RPS token buckets; per-request TPM estimation optional |
| Part 4 approved design: audit consumer, `jobs.dlq.audit`, parking queue, migration 004, 10 decisions | **Over-engineered for the spec.** The spec DLQ requirement is met by Part 3 for budget exhaustion. The remaining gap (D-2) can be closed by a much smaller mechanism: the worker reads `x-delivery-count`; on the last allowed delivery (count ≥ `QUEUE_DELIVERY_LIMIT`) it dead-letters the job through the existing Part 3 `dead_letter` transaction (state + event + DLQ copy) before running the stage. No new queues, consumer or migration; malformed messages have no job to update anyway | recommend replacing Part 4 with this "poison guard" (needs your decision; the probe findings remain valid documentation) |
| `dead_letter_audit` table (migration 003) | Unused unless Part 4 is built as designed | harmless; leave |
| Part 6 DLQ inspection CLI | Optional (spec: "manual inspection" — RabbitMQ management UI suffices) | defer/drop |
| Test diagnostics, reliability runners | Test-only | fine |

## 6. Exact remaining mandatory work
1. **Reliability close-out:** close D-2 (poison guard or Part 4); decide D-1; global rate limiter (Gemini
   + Notion; YouTube via backoff only); deploy Phase 2 to dev and remove `jobs.work`; commit Phase 2.
2. **Pipeline:**
   - Transcript + duration with Constraint A (rejection is deterministic; reject at transcript time or
     at submission);
   - Gemini synthesis: bullets, timestamps, a caption per timestamp, chunk + ordered merge;
   - Frame extraction (video download + ffmpeg seek, real frames only, omit failures, warnings);
   - Notion publishing: image upload, page with headings/bullets/captioned images, default parent,
     idempotent against redelivery, absolute URL;
   - every external call raises classified errors.
3. **Extension:** MV3 content script (button, SPA navigation), background (Notion OAuth via the gateway
   redirect, token storage, submit, poll), popup (human-readable stages, View in Notion, failure/warning
   display); CORS / allowed origin.
4. **Delivery:** README (base URL, default destination, every env var, retry/limit parameters, demo
   profile, how to load the extension), end-to-end demo of the four §6 failure modes, ≤20-minute latency
   check, burst test without LLM 429.

## 7. Recommended roadmap (4 phases)

**Phase A — Backend reliability close-out**
- Tasks: poison guard (or Part 4 if you keep it); D-1 decision; Redis limiter (Gemini RPM/RPD, Notion
  RPS) with fail-closed infra retry; commit Phase 2; dev deployment + `jobs.work` removal.
- Depends on: Parts 1–3.
- Acceptance:
  - crash loop → DEAD_LETTERED;
  - 3 worker processes never exceed the configured limit in any window;
  - Redis down → infra retry, no provider call;
  - full suite green.
- Deferred: DLQ CLI, alerts, `/healthz` DLQ depth, parking queue.

**Phase B — Video-processing pipeline**
- Tasks: the four stages with checkpoints and classified errors; Constraint A; Constraint B (real frames
  + captions); graceful degradation; Notion idempotency (reconcile when the write outcome is unknown);
  ffmpeg in the image (dependency approval).
- Depends on: Phase A limiter; Gemini key and Notion integration (user-created).
- Acceptance:
  - real ≤20-minute video → COMPLETED with captioned real frames;
  - >20 minutes → deterministic rejection;
  - frame failures → COMPLETED_WITH_WARNINGS / text-only page;
  - SIGKILL during frames → one Notion page;
  - forced 429 per API → waiting, then success;
  - unavailable video → behaviour per D-1.
- Deferred: transcript fallbacks beyond one method, model tuning.

**Phase C — Extension and end-to-end integration**
- Tasks: content/background/popup, OAuth, CORS.
- Depends on: Phase B, a registered OAuth redirect URI (user approval).
- Acceptance:
  - click Take Notes on several videos in a row → independent jobs;
  - popup shows the stage sequence, retry waiting, warnings, failures and a working Notion link;
  - navigation between videos keeps the button.
- Deferred: styling beyond clarity.

**Phase D — Grading readiness and submission**
- Tasks: README, demo profile, scripted demo of the four §6 failure modes, latency run near 20 minutes,
  burst run without LLM 429, final full suite, final commit/tag.
- Acceptance: a stranger brings the stack up from the README alone; all demos pass.

Combining A–D further is not recommended: B needs A's limiter, C needs B's real results, and D needs C.

## 8. Test commands and results
| Command | Result |
|---|---|
| `pdftotext -layout PROJECT_SPEC.pdf -` | full spec text extracted (§1–§10) |
| `docker compose ps` | gateway, outbox-publisher, worker ×2, postgres, rabbitmq, redis: all running and healthy (Phase 1 images) |
| `curl -s http://localhost:8000/healthz` | `{"status":"ok", gateway ok, postgres ok (outbox_pending 0), rabbitmq ok (work_queue_messages 0, consumers 2), redis ok (live_workers 2)}` [RUN] |
| `MSYS_NO_PATHCONV=1 docker compose --profile test run --rm --no-deps tests pytest -p no:warnings -q -rfE` (isolated test DB, vhost `notes_test`, Redis db 15) | **555 passed in 540.49 s**, 0 failed, 0 errors; started 22:08:35 and ended 22:17:38 by the host clock. RabbitMQ / PostgreSQL / Redis healthy with 0 restarts afterwards; no new diagnostics files (still 5) |

What the suite proves: the gateway API contract, validation, idempotency, migrations, state machine,
outbox, credential mailbox, worker crash recovery, retries/backoff/DLQ, topology and the queue migration
— **all with fixture stages**. It does not prove any transcript, LLM, frame, Notion or extension
behaviour, because none exists.

## 9. Git status
- **Before:** HEAD = `main` = `phase1-baseline` = `49eed18`; 0 staged; 41 status entries (uncommitted
  Phase 2 work).
- **After:** identical (41 entries, 0 staged, HEAD `49eed18`) plus this new untracked log inside the
  already-untracked `docs/session_logs/`.

## 10. Unresolved questions / not verified
1. D-1: should an unavailable/private video dead-letter (spec demo) or fail immediately (current ADR-10 D3)?
2. Replace the approved Part 4 design with the smaller poison guard (§5)?
3. Close D10 by a fresh dev broker volume instead of running the drain-safe migration?
4. [NV] Notion OAuth feasibility for a Chrome extension (redirect via the gateway, ADR-07) — not built,
   not tested; requires creating the Notion integration (your approval).
5. [NV] Gemini free-tier limits are sufficient for a ~20-minute video within an acceptable latency.
6. [NV] Video download + ffmpeg seeking in the container (tooling, image size, YouTube access from Docker).
7. [NV] End-to-end latency and burst behaviour (§9) — impossible to measure until Phases B–C exist.
8. The host clock showed 22:08 for this run, while earlier logs today recorded ~17:13. This was noted
   only; it does not affect the results.
