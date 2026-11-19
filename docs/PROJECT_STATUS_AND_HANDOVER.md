# Project status and handover — Distributed Multimodal Video Note-Taking Agent

- **Prepared:** 2026-11-20, for an independent review of the current project state. **Updated:**
  2026-11-20 after Phase A Part 2 (shared rate limiter).
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
| Phase A Part 2 | **Shared external-API rate limiter** (ADR-13, `common/ratelimit.py`): atomic Redis Lua checks on Redis time. Gemini: RPM 4 and input TPM 200,000 (sliding 60 s windows), RPD 16 per Pacific day. Notion: 2.5/s per connection. YouTube: no quota, cooldown only. Shared Retry-After cooldown recorded by the worker's existing failure path; waits ≤ 60 s in-process, otherwise the existing outbox retry; Redis down → `RateLimiterUnavailable` (infra budget), no provider call. Wired into `StageContext.limiter`, settings and the Compose worker | [TEST] 25 tests incl. 3-process RPM/TPM/RPD/Notion, shared cooldown from a real worker, Redis-down worker path; full suite 653 passed (2026-11-20). **Not yet deployed to the dev stack; no real provider calls exist yet** |

Each part has detailed session logs in `docs/session_logs/` (11 files).

---

## 4. Current development and Docker status

[RUN] inspected 2026-11-20:

| Service | State | Image |
|---|---|---|
| gateway | healthy | `notes-agent-backend:dev` (Phase 2, ID `63cf80e0e2cd`) |
| outbox-publisher | healthy | same |
| worker ×2 | healthy | same |
| postgres / rabbitmq / redis | healthy | `postgres:16`, `rabbitmq:4.4.1-management`, `redis:7-alpine` (not recreated during deployment; volumes preserved) |

- **Database:** migrations `001`–`004` applied. One job row exists: the deployment smoke test,
  `FAILED / PIPELINE_STAGE_UNAVAILABLE`.
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
- **Functionally:** the worker has **no pipeline stages**. Every real job ends
  `FAILED / PIPELINE_STAGE_UNAVAILABLE` by design (no fake stages).

---

## 5. Remaining mandatory requirements (from the compliance audit, re-checked)

| Requirement | Status |
|---|---|
| R1 gateway / broker / ≥ 2 workers | **Done** [TEST][RUN] |
| R1/R2 browser extension (content script, background, OAuth, popup) | **NOT IMPLEMENTED** — `extension/` contains only `manifest.json` (name, version, key) |
| R3 transcript + duration | **NOT IMPLEMENTED** (stage registry is empty) |
| R3 LLM synthesis (bullets, timestamps, captions, chunking) | **NOT IMPLEMENTED** |
| R3 frame extraction | **NOT IMPLEMENTED** (no ffmpeg/yt-dlp in the image) |
| R3 Notion publishing (images, captions, default parent, idempotent against redelivery) | **NOT IMPLEMENTED** |
| R4 lifecycle / state machine | **Done** [TEST] |
| R5 API contract | **Done** [TEST]; a *working* Notion URL can only be verified once publishing exists |
| R6 429 handling | **Mechanism done** with fixture stages [TEST]; no real API calls exist yet |
| R6 crash recovery | **Done for the infrastructure** [TEST][RUN]; "no duplicate Notion page" depends on idempotent publishing (not built) |
| R6 graceful degradation | Result logic present (`frames_published < frames_requested` → WITH_WARNINGS) [SRC][TEST]; the frame stage and text-only page are **not built** |
| R6 DLQ | **Done** (worker exhaustion + broker dead letters) [TEST]; see decision D-1 in §10 |
| R7-A ≤ 20 min / reject longer | **NOT IMPLEMENTED** (`MAX_VIDEO_DURATION_SEC=1200` exists in `.env` only; no code reads it) |
| R7-B real frames + captions | **NOT IMPLEMENTED** |
| R8 README | **NOT IMPLEMENTED** (no README in the repository) |
| R8 compose / health check | **Done** [RUN] |
| R9 global LLM rate limit (no LLM 429 in bursts) | **Limiter implemented and tested** [TEST] (ADR-13). It only takes effect once Phase B stages call `ctx.limiter.acquire()` before each Gemini/Notion/YouTube request; the burst behaviour against the real Gemini API is unverified |

---

## 6. Git state

- **Branch:** `phase-2`. **HEAD:** `49eed183fc8b9a040c170aed5d691ef6e0bbd33a` (= `main` =
  tag `phase1-baseline`). History: `927ec18` Phase 0, `49eed18` Phase 1.
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
| 13 | Shared external-API rate limiter: atomic Redis Lua on Redis time. Gemini RPM/input-TPM sliding windows + RPD per Pacific day (values used as given, already × 0.8); Notion spacing per connection; YouTube cooldown only. Shared Retry-After cooldown reported by the worker; waits ≤ `RATE_LIMIT_MAX_WAIT_SEC`, else the existing outbox retry; Redis down → `RateLimiterUnavailable` |

---

## 8. External APIs, models, credentials and configuration (no secret values)

| Service | Method / model | Credential | Status |
|---|---|---|---|
| YouTube transcript + duration | `youtube-transcript-api` (no key; unofficial web-client interface) + `yt-dlp` metadata — **approved in the Phase 0 plan, not recorded in an ADR** | none | [PLAN] not installed |
| Frames | `yt-dlp` stream URL + `ffmpeg -ss T -frames:v 1` | none | [PLAN] |
| Gemini | `gemini-3.8-flash`, thinking level `low`, free tier | `GEMINI_API_KEY` (from Google AI Studio, project **without billing**) | key **empty** in `.env`; limits unknown |
| Notion | REST API (httpx), File Upload API, OAuth | `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`, `NOTION_REDIRECT_URI`, `NOTION_DEFAULT_PARENT_ID` | all **empty** in `.env`; integration not yet created |

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
| **Full suite (latest)** | 2026-11-20 01:15–01:26 | **653 passed**, 0 failed, 0 errors (658.35 s), including the 25 new rate-limiter tests (`test_ratelimit.py`: 25 passed in 32.36 s) |
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

  Recommended: (a). Still open.
- **Notion redirect URI:** the `chromiumapp.org` form is not documented by Notion; it needs a real test
  (approval required).
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

**Phase A Part 2 (shared rate limiter) is done** — see §3 and ADR-13. Evidence: 25 dedicated tests and
the full suite of 653 passed.

1. **The user reviews and commits** the work on `phase-2`. The index was partly staged during Phase A
   Part 2; re-stage the files listed in §6 so the commit contains the latest versions.
2. **The user decides D-1** (unavailable video → DLQ or FAILED). This is needed before the transcript
   stage's error mapping is written.
3. **Optional, on instruction:** redeploy the dev stack, so the running workers include the limiter
   (same procedure as the Phase A deployment, no migration needed).
4. **Next mandatory development task — Phase B, part 1: transcript stage** (TRANSCRIBING):
   - `youtube-transcript-api` + `yt-dlp` metadata (title, exact duration) behind
     `ctx.limiter.acquire(Provider.YOUTUBE)`;
   - **constraint A** (> `MAX_VIDEO_DURATION_SEC` = 1200 → deterministic FAILED);
   - library exceptions mapped to classified errors (ADR-10), including the D-1 decision;
   - an idempotent checkpoint;
   - an ADR recording the transcript method.

   This adds new dependencies, which need approval. The Gemini, frame and Notion stages follow,
   each acquiring a limiter permit before every external call.

After that come Phase B (transcript, Gemini, frames, Notion, with constraints A and B), Phase C
(extension and OAuth) and Phase D (README, grading demos, latency/burst runs, final submission), as
set out in `docs/session_logs/project_spec_compliance_audit.md`.
