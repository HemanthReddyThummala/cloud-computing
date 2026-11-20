# Project status and handover — Distributed Multimodal Video Note-Taking Agent

**This file is the single source of truth for project status.**
- Last updated: **2026-11-21 04:3x IST** (ADR-21 deployment session).
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
| 06/13 | Gemini free tier `gemini-3.8-flash`; Redis limiter RPM 4 / TPM 200k / RPD 16 (= AI Studio limits × 0.8) |
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
| SUMMARIZING (Gemini) (R3) | 🔴 **Blocked (external)** | 1 real success (2026-11-20, 6 segments, 32.7 s). Since then **15 real requests all failed**: 503 `UNAVAILABLE` ×13, 504 ×2 (incl. a tiny controlled probe). See §5 |
| ADR-21 Gemini 503/504 handling | ✅ **Deployed**; 🧪 tested; behaviour checked **offline in the deployed worker** | 2026-11-21: delays 60/180/600/600, Retry-After cap 3600, `X-Server-Timeout: 300` via the real SDK with a mock transport. **Not yet exercised by a real Gemini failure** |
| EXTRACTING_FRAMES (R3, R7-B) | 🧪 + one real one-off | 42 unit tests with real FFmpeg on synthetic videos; one real yt-dlp + FFmpeg run outside a job (2026-11-20). **Never reached in a real job** |
| PUBLISHING to Notion (R3, R6 no duplicates) | 🧪 Tested (fakes) only | Fake Notion API tests incl. SIGKILL mid-append. **Never run against real Notion** |
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
| Backend full suite (Docker, real PostgreSQL / RabbitMQ / Redis, fake providers) | **888 passed, 0 failed** (789 s) | 2026-11-21 03:45–03:58 IST (ADR-21 code = the deployed code) |
| Extension `cd extension && npm test` (Node, fake chrome + local fake gateway) | **94 passed**, 0 failed | 2026-11-21 (re-run in the deployment session) |
| Extension ↔ real gateway (fake Notion) cross-check | 10/10 PASS | 2026-11-20 (Phase C Part 3) |
| Deployed-worker configuration check (no network) | delays [60, 180, 600, 600]; 5th → DEAD_LETTERED with `last_provider_code` UNAVAILABLE; Retry-After 999999 → 3600; YouTube 503 → 10 s; SDK `X-Server-Timeout` 300 | 2026-11-21 deployment |

Commands:
- backend: `docker compose --profile test build tests`, then
  `MSYS_NO_PATHCONV=1 docker compose --profile test run --rm --no-deps tests pytest -q`;
- extension: `cd extension && npm test`.

Tests never call real Gemini, YouTube or Notion.

## 5. Current blocker: Gemini unavailable for our API key/project

- Real jobs for **D26sUZ6DHNQ** (×2) and **_S-cBzAg0GE** (×1) all DEAD_LETTERED at SUMMARIZING.
- Google answered **503 `UNAVAILABLE`** (and 504 at the then-120 s deadline). The controlled probe
  (2026-11-20 20:25 UTC, 6-segment input that had worked before) also got 503 `UNAVAILABLE` in 4.3 s.
- **Not** our request, model ID, key or code: the requests reach Google and are valid. AI Studio's UI
  answering does not prove API availability.
- The daily budget for Pacific day 2026-11-20 was used up (16/16 in our limiter). It resets at **08:00 UTC
  (13:30 IST) on 2026-11-21**.
- ADR-21 (now deployed) makes such episodes visible (`last_provider_code`), longer-lived (about 24 min
  of backoff) and clearer in the popup. It **cannot** make Gemini answer.
- Next (user decision):
  1. after the reset, one probe request (the T1 script in
     `docs/session_logs/gemini_controlled_tests_T1_T2.md`) before any real job;
  2. if it is still unavailable, probe another free-tier Flash model available to the key and change
     `GEMINI_MODEL` (check its AI Studio limits first; ADR-13 limits are per model).

## 6. Deployment status

- **Running dev stack (deployed 2026-11-21 04:26 IST = 2026-11-20 22:56 UTC):** gateway, outbox-publisher
  and worker ×2 on image **`2a01b00b9603`** (`notes-agent-backend:dev` =
  **`notes-agent-backend:adr21-uncommitted-2026-11-21`**).
- **The deployed code is NOT in a commit.** It was built from the working tree on top of HEAD `00ef0f4`
  plus the uncommitted ADR-21 changes, by the user's explicit choice. All 48 backend files in the image
  are byte-identical to the working tree (sha256 check).
- **After committing,** the backend files in that commit should equal the deployed image; re-check with
  the same hash comparison if anything changes.
- **Not changed:** PostgreSQL / RabbitMQ / Redis (running since 2026-11-19, 0 restarts, volumes and data
  intact); migrations 001–004 (the migrate check applied nothing); delay queues `jobs.retry.{5,10,30,90}s`
  (no new queues).
- **Health:** `/healthz` 200, 2 live workers, 2 consumers; startup logs had 0 errors or warnings.
- **Effective worker config:** `GEMINI_REQUEST_TIMEOUT_SEC=300`, `GEMINI_UNAVAILABLE_BACKOFF_SEC=60,180,600`
  (Compose defaults; `.env` has neither), `BACKOFF_SCHEDULE_SEC=10,30,90`, `MAX_ATTEMPTS=5`, model
  `gemini-3.8-flash`.
- **Rollback targets:** `notes-agent-backend:00ef0f4` (`70dce0098cd2`, previous deployment). The
  pre-Phase-C image `8cb661395cde` no longer exists.
- **Extension:** source unchanged by the deployment. The ADR-21 popup wording requires reloading the
  unpacked `extension/` folder in `chrome://extensions` (no build step).

## 7. Configuration (names only; never commit `.env`)

- **Required (Compose fails without them):** `DATABASE_URL`, `RABBITMQ_URL`, `REDIS_URL`,
  `JOB_PAYLOAD_ENCRYPTION_KEY`, `POSTGRES_{DB,USER,PASSWORD}`, `RABBITMQ_DEFAULT_{USER,PASS}`.
- **Gemini (worker):**
  - `GEMINI_API_KEY` [secret], `GEMINI_MODEL` (gemini-3.8-flash), `GEMINI_THINKING_LEVEL` (low);
  - `GEMINI_RPM_LIMIT` / `GEMINI_TPM_LIMIT` / `GEMINI_RPD_LIMIT` (4 / 200000 / 16);
  - `GEMINI_REQUEST_TIMEOUT_SEC` (300), `GEMINI_UNAVAILABLE_BACKOFF_SEC` (60,180,600);
  - `TRANSCRIPT_CHUNK_TOKENS`, `MAX_FRAMES`.
- **Notion:** gateway gets `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET` [secret], `NOTION_REDIRECT_URI` =
  `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion`, and `OAUTH_STATE_TTL_SEC`; the
  worker gets `NOTION_DEFAULT_PARENT_ID` (ID or page URL); both get `NOTION_VERSION` and
  `NOTION_RPS_LIMIT`.
- **Retries:** `MAX_ATTEMPTS` (5), `BACKOFF_SCHEDULE_SEC` (10,30,90; one delay queue per value),
  `INFRA_MAX_ATTEMPTS`, `QUEUE_DELIVERY_LIMIT`.
- **The user's `.env`:** all required values set (checked by name and presence only); the two ADR-21
  variables are absent, so the defaults apply.
- **Extension:** ID `kkkeigfecmfhmainhbkcpdhjnloifbdf` (manifest `key`); gateway `http://localhost:8000`
  (`extension/config.js` + manifest host permission); permissions `identity`, `storage`.

## 8. Known limitations and open issues

1. **Gemini unavailable** (§5): no real end-to-end COMPLETED job yet, so real Gemini notes, real frames
   in a job, real Notion publishing and the "no duplicate page" guarantee against real Notion are all
   **Unverified**.
2. **Deployed code uncommitted** (§6) until the user commits ADR-21.
3. **README missing (R8).**
4. **Quota:** a failing Gemini job can use 4–5 of the 16 daily requests; with ADR-21 it takes about 24
   min to dead-letter.
5. **Real Chrome:** YouTube DOM changes could hide the button. The service-worker lifetime during long
   polling is Unverified (resumes on the next event).
6. **Broker:** the DLQ and parking queue have no retention limit; inspection is manual. Single-node
   RabbitMQ.
7. **Historical:** two unexplained intermittent test failures (2026-11-19), not reproduced since.
8. **Local `.env`:** contains unused keys (`BACKOFF_JITTER`, `GEMINI_LIMIT_SAFETY_FACTOR`,
   `LLM_MAX_WAIT_SEC`, …). They are harmless and were not edited (secrets file).

## 9. Remaining mandatory work (priority order)

1. **User:** commit ADR-21; reload the extension in Chrome.
2. **Restore Gemini:** after the quota reset (13:30 IST on 2026-11-21), one probe request; then **one
   real end-to-end job** on a short video. Verify the summary, frames + captions, the Notion page, "View
   in Notion", and no duplicate page. If it is still unavailable, decide on a model (user).
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
| 2026-11-21 | `deployment_adr21.md` | ADR-21 (uncommitted working tree) deployed and verified; this file restored as the source of truth; `CLAUDE.md` created |
| 2026-11-21 | `adr21_gemini_failure_handling.md` | ADR-21 implemented + tested (888 / 94) |
| 2026-11-20 | `gemini_controlled_tests_T1_T2.md`, `gemini_integration_root_cause_diagnosis.md`, `first_real_job_failure_diagnosis.md` | Gemini 503 `UNAVAILABLE` diagnosed (external) |
| 2026-11-20 | `deployment_phaseC_00ef0f4.md` | `00ef0f4` deployed and verified |
| 2026-11-20 | `phaseC_part{1,2,3}_*.md` | OAuth (gateway + extension), Take Notes / polling / popup |
| 2026-11-19/20 | `phaseB_part{1..4}_*.md`, `phaseA_*`, `phase2_*` | Pipeline stages, limiter, retries, DLQ audit |
