# Deployment of ADR-21 (Gemini failure handling) to the dev stack

Date: 2026-11-21, 04:2x–04:3x IST (2026-11-20 22:5x UTC). The user authorised this deployment.

Not done (by instruction):
- no Gemini, YouTube or Notion request;
- no job;
- no `.env` change;
- no model change;
- no Git operations;
- no volume deletion;
- no `down -v`.

## 1. Initial state and a discrepancy

- Branch `phase-2`, **HEAD `00ef0f4`** (2026-11-20 23:37 IST), the commit that was already deployed.
- **ADR-21 was NOT committed** (contrary to the task text):
  - 17 modified files: backend `config.py`, `http_errors.py`, `jobs_repo.py`, `retry_policy.py`,
    `runner.py`, tests, `docker-compose.yml`, `.env.example`, docs, extension `lib/jobs.js`,
    `lib/popup-view.js` and tests;
  - plus untracked `test_unit_gemini_failures.py`, session logs and `handover.md`.
- The diff was exactly the ADR-21 session's work. The newest file times were 03:59 IST, from that
  session, so there were no other user edits.
- **Asked the user** before deploying. **Decision: deploy the working tree now**, tagged as
  uncommitted, and rewrite `docs/PROJECT_STATUS_AND_HANDOVER.md` as the single status document.

## 2. Pre-deployment inspection

| Check | Result |
|---|---|
| Running backend | gateway, outbox-publisher, worker ×2 on `70dce0098cd2` (`notes-agent-backend:00ef0f4`), healthy, 0 restarts, started 2026-11-20 18:10 UTC |
| Infrastructure | postgres `1a6ab3f5345e`, rabbitmq `61f90c401a5b`, redis `7f7d30b7fdd0`, running since 2026-11-19, healthy, 0 restarts |
| In flight | jobs 0 non-terminal of 5; outbox 0 unpublished of 20; work/retry queues 0 messages; `jobs.dlq` 3 (expected copies) |
| Migrations | 001–004 applied; none added by ADR-21 |
| Dockerfile / requirements / migrations changed since `00ef0f4` | no |
| Compose (rendered, non-secret values only) | worker `GEMINI_REQUEST_TIMEOUT_SEC=300`, `GEMINI_UNAVAILABLE_BACKOFF_SEC=60,180,600`, `BACKOFF_SCHEDULE_SEC=10,30,90`, `MAX_ATTEMPTS=5`, `GEMINI_MODEL=gemini-3.8-flash`, replicas 2 |
| Retry-After cap | `classify_http(..., retry_after_cap=RETRY_AFTER_MAX_SEC=3600)` for Gemini |
| Services needing recreation | gateway, outbox-publisher, worker ×2 (shared image); `migrate` re-runs as a Compose dependency (idempotent check only) |
| Risks | deployed code not committed (accepted by the user); nothing in flight; no topology or schema change → **no blocker** |

## 3. Commands executed and results

| # | Command | Result |
|---|---|---|
| 1 | `docker compose build migrate` | exit 0 → `notes-agent-backend:dev` = **`sha256:2a01b00b9603…`** (layers cached from the ADR-21 test build; created 2026-11-20 22:13 UTC) |
| 2 | sha256 of `/app/notesagent`, `migrations`, `requirements.txt` in the image vs the working tree | **48/48 identical** (includes the uncommitted ADR-21 code) |
| 3 | `docker tag notes-agent-backend:dev notes-agent-backend:adr21-uncommitted-2026-11-21` | tag → `2a01b00b9603`; `notes-agent-backend:00ef0f4` still → `70dce0098cd2` (rollback) |
| 4 | `docker compose --dry-run up -d --no-build gateway outbox-publisher worker` | would recreate migrate, gateway, outbox-publisher, worker-1, worker-2; postgres, rabbitmq, redis "Running" |
| 5 | `docker compose up -d --no-build gateway outbox-publisher worker` | **exit 0**, 04:26:19 → 04:26:27 IST; the 5 containers recreated and started; infrastructure "Running" (not recreated) |

## 4. Verification

| Check | Result |
|---|---|
| Containers | gateway, outbox-publisher, worker-1, worker-2 **healthy**, 0 restarts, image **`2a01b00b9603`**; migrate exited 0 on the same image; postgres/rabbitmq/redis: same image IDs and start times (2026-11-19), 0 restarts |
| migrate | `"migrations up to date", "applied_now": []` |
| `/healthz` | HTTP 200; postgres ok (outbox_pending 0); rabbitmq ok (work queue 0 messages, **2 consumers**); redis ok (**2 live workers**) |
| Startup logs (since 22:56 UTC, all backend services) | 16 lines: migrations up to date, **broker connected ×4**, gateway started, **outbox publisher started**, **worker started ×2** with stages `[EXTRACTING_FRAMES, PUBLISHING, SUMMARIZING, TRANSCRIBING]`, uvicorn startup. **0** tracebacks, ERROR/CRITICAL, exceptions or warnings; 0 key-like strings |
| Worker connections | PostgreSQL (the pool opens before "worker started"), RabbitMQ (broker connected; 2 consumers on `jobs.work.v2` and `jobs.dlq.audit`), Redis (2 heartbeats, `live_workers` 2) |
| Container environment | both workers: `GEMINI_REQUEST_TIMEOUT_SEC=300`, `GEMINI_UNAVAILABLE_BACKOFF_SEC=60,180,600`, `BACKOFF_SCHEDULE_SEC=10,30,90`, `MAX_ATTEMPTS=5`; gateway has no `GEMINI_API_KEY` |
| **Effective behaviour, deployed code + deployed env, no network** (`docker exec notes-agent-worker-1 python -`) | `get_settings()`: timeout 300.0, Gemini backoff [60, 180, 600], default [10, 30, 90], max 5, delay queues [5, 10, 30, 90]. Gemini 503 → delays **[60, 180, 600, 600]**, 5th failure → DeadLetterPlan with `last_error_code` GEMINI_HTTP_503, `last_http_status` 503, `last_provider_code` UNAVAILABLE. 504 first delay 60. `Retry-After: 999999` → classified **3600** → delay 3600. YouTube 503 → **10** (unchanged). **Real `GeminiClient` + real google-genai SDK with a mock httpx2 transport and a FAKE key:** `X-Server-Timeout: 300`, client read timeout 300.0, 1 request, host `generativelanguage.googleapis.com` (mocked, not contacted) |
| Data intact | jobs 5, job_events 43, checkpoints 4, outbox 20 (0 unpublished), migrations 4; the same 8 queues (no new delay queues), `jobs.dlq` 3, consumers 2/2; Redis `ratelimit:gemini:rpd:2026-11-20` + 2 heartbeats; volumes untouched |

**Verified:** the new image is running everywhere it should; the configuration is wired; the
retry/timeout logic of the deployed code produces the intended values; the infrastructure and data are
intact.

**Not verified (needs real traffic):**
- behaviour on a real Gemini 503/504 (no request was allowed);
- the popup wording in real Chrome;
- any real end-to-end job.

## 5. Extension

- No build step (plain ES modules, loaded unpacked). No source changes were needed in this session.
- `npm test`: **94 passed, 0 failed**. `node --check` passes for all files. Manifest unchanged (0.1.0;
  `identity`, `storage`; host `http://localhost:8000/*`).
- **Reload instructions for the user:**
  1. open `chrome://extensions`;
  2. Developer mode is on;
  3. on the "Video Notes Agent" card (ID `kkkeigfecmfhmainhbkcpdhjnloifbdf`) click the **reload** icon;
  4. if the card is missing: **Load unpacked**, then select
     `C:\Academics\SEM-5\Cloud Computing\Project\extension` (the folder containing `manifest.json`);
  5. reload any open YouTube tabs so the content script is re-injected.

  The Notion connection (sealed token in `chrome.storage.local`) survives a reload.
- **No real YouTube or browser test was performed in this session.**

## 6. Files changed in this session

| File | Change |
|---|---|
| `docs/PROJECT_STATUS_AND_HANDOVER.md` | Rewritten as the single, full status document (verified status + this deployment). It had been reduced to a pointer in the previous session |
| `CLAUDE.md` (new) | Project instructions: maintain `docs/PROJECT_STATUS_AND_HANDOVER.md` at the end of each session, session-log rule, Git/secret rules, commands |
| `docs/session_logs/deployment_adr21.md` (new) | this log |
| (memory, outside the repo) | note updated: the status document is `docs/PROJECT_STATUS_AND_HANDOVER.md` |

No application source, test, `.env` or Compose file was changed in this session. The root `handover.md`
was left untouched (by instruction).

## 7. Failures / unresolved

- No deployment failures.
- **The deployed code is not in a commit** until the user commits ADR-21. The tag
  `adr21-uncommitted-2026-11-21` marks it. After committing, the backend files should be re-checked
  against the image if anything changed.
- **Gemini availability is still unknown since 2026-11-20 21:52 UTC** (last 503). The daily quota
  resets 08:00 UTC (13:30 IST) on 2026-11-21.
- Still open: a real end-to-end job; README (R8); live demos.

## 8. Final deployed state

| Service | Image | State |
|---|---|---|
| gateway | `2a01b00b9603` (`notes-agent-backend:dev` / `adr21-uncommitted-2026-11-21`) | healthy |
| outbox-publisher | same | healthy |
| worker ×2 | same | healthy, 4 stages |
| migrate | same | exited 0 (nothing applied) |
| postgres / rabbitmq / redis | unchanged | healthy (since 2026-11-19) |
