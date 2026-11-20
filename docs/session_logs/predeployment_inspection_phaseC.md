# Pre-deployment inspection (before the first real end-to-end deployment)

Date: 2026-11-20 (about 23:30 IST). **Read-only.**
- Nothing was built, pulled, deployed, restarted or installed.
- No source file, configuration, `.env` or database state was changed. The handover document was
  deliberately not updated.
- No Git operations (read-only `git status/log/diff/branch` only).
- No secret values were printed: variables are reported by name with set / empty / missing only.

## 1. What was inspected

| Area | Files / objects (read, not modified) |
|---|---|
| Git | branch, HEAD, `git status`, `git diff --stat`, stash, `main...HEAD` |
| Build | `backend/Dockerfile` (stages `base` / `runtime` / `test`), `backend/.dockerignore`, `backend/requirements*.txt` (unchanged since HEAD) |
| Compose | `docker-compose.yml` (services, images, targets, `depends_on`, ports, variable references); `docker compose config --format json` rendered in memory and summarised by **names only** |
| Migrations | `backend/migrations/001`–`004`, `notesagent/common/migrate.py` (`schema_migrations` + advisory lock) |
| Worker | `notesagent/worker/stages.py` (`build_stage_registry`: all four stages) |
| Environment | `.env` (parsed for names and empty/non-empty only), `.env.example` |
| Extension | `extension/config.js` (`GATEWAY_BASE_URL`), `extension/manifest.json` (host permission) |
| Running stack | `docker compose ps`, `docker inspect` (image IDs, start times, restart counts, env **names**), `docker images`, `docker system df`, `GET /healthz`, read-only SQL, `rabbitmqctl list_*`, Redis `SCAN` key names |

## 2. Commands executed and results

| Command (summarised) | Result |
|---|---|
| `git branch --show-current; git log --oneline` | `phase-2`, **HEAD `5a63cdd`** "Implement complete video processing and Notion publishing pipeline"; `main` = `49eed18`; `phase-2` is 2 commits ahead of `main`; no stash |
| `git status --short` | **14 modified** files (backend: `config.py`, `crypto.py`, `gateway/main.py`, `gateway/submission.py`, `worker/runner.py`, `tests/support/fake_notion.py`; plus `docker-compose.yml`, `.env.example`, `docs/*` ×3, `extension/README.md`, `background.js`, `manifest.json`) and **15 untracked** entries (`gateway/oauth.py`, 2 OAuth test files, 3 Phase C session logs, `extension/{config.js, content.css, content.js, lib/, package.json, popup.css, popup.html, popup.js, tests/}`). Nothing staged. **Phase C Parts 1–3 are entirely uncommitted** |
| `docker compose ps -a` + `docker inspect` | gateway, outbox-publisher, worker-1, worker-2: **running, healthy, 0 restarts**, started 2026-11-19 20:36 UTC (= 2026-11-20 02:06 IST), image **`sha256:8cb661395cde…`**. migrate: exited (0). postgres:16, rabbitmq:4.4.1-management, redis:7-alpine: running, healthy, 0 restarts, created 2026-11-19 |
| `docker image inspect 8cb661395cde…`; `docker images -a` | **No such image.** The image record of the running backend no longer exists; its tag `notes-agent-backend:dev` moved to the rebuild of 2026-11-20 22:48 IST. The running containers still use its layers, but it cannot be re-tagged or used for a rollback |
| `docker images` | `notes-agent-backend:dev` = **`70dce0098cd2`** (built 2026-11-20 22:48:47 IST, runtime target); `notes-agent-backend:test` = `77cd8355f01c` (22:48:53) |
| newest backend source mtime (excluding tests) | `gateway/main.py` 22:47 IST, others 22:46 → **`70dce0098cd2` was built after the last backend change**; no backend file changed afterwards (Parts 2–3 touched only `extension/`, docs and `.env.example`) |
| `curl /healthz` | 200 `ok`: gateway ok; postgres ok (outbox_pending 0); rabbitmq ok (`jobs.work.v2`: 0 messages, 2 consumers); redis ok (2 live workers) |
| `curl -X POST /oauth/notion/authorize` (running gateway) | **404**: the running gateway has no OAuth routes. The route does not exist there, so nothing was created |
| worker log (last 24 h) | `"stages": ["TRANSCRIBING"]` → the running workers have only stage 1; the repository registers TRANSCRIBING, SUMMARIZING, EXTRACTING_FRAMES, PUBLISHING |
| `SELECT … FROM schema_migrations` | `001_init`, `002_result_integrity`, `003_retry_dlq_foundation`, `004_dead_letter_audit_outcomes`. The repository has exactly these four → **no pending migrations** |
| `SELECT status, count(*) FROM jobs …` | 2 jobs, both `FAILED` (earlier smoke/verification jobs); **0 non-terminal jobs** |
| outbox / dead_letter_audit | 2 outbox rows, **0 unpublished**; 0 audit rows |
| `rabbitmqctl list_queues -p /` | `jobs.work.v2` (0 msgs, 2 consumers), `jobs.dlq.audit` (0, 2), `jobs.dlq.audit.parked`, `jobs.dlq`, `jobs.retry.{5,10,30,90}s` — **all 0 messages**, 0 unacked. **No `cred.*` mailbox queues**. No policies. Exchanges `jobs`, `jobs.dlx`, `jobs.retry`. Vhosts `/`, `notes_test` |
| Redis db 0 key names | 2 worker heartbeats, `ratelimit:gemini:tpm`, `ratelimit:gemini:rpd:2026-11-20` = **1** (one Gemini request already counted today; limit 16). AOF enabled |
| Compose variable references vs `.env` (names only) | All **no-default** variables set: `DATABASE_URL`, `RABBITMQ_URL`, `REDIS_URL`, `JOB_PAYLOAD_ENCRYPTION_KEY`, `POSTGRES_{DB,USER,PASSWORD}`, `RABBITMQ_DEFAULT_{USER,PASS}`. `GEMINI_API_KEY`, `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`, `NOTION_REDIRECT_URI`, `NOTION_DEFAULT_PARENT_ID`: **set**. Empty but defaulted with `:-`: `GEMINI_RPM_LIMIT` (→ 4), `GEMINI_TPM_LIMIT` (→ 200000), `GEMINI_RPD_LIMIT` (→ 16). Missing but defaulted: `CREDENTIAL_READ_WAIT_SEC`, `FRAME_MAX_WIDTH`, `FRAME_SOURCE_MAX_HEIGHT`, `INFRA_MAX_ATTEMPTS`, `INFRA_RETRY_DELAY_SEC`, `LOCK_RETRY_DELAY_SEC`, `OAUTH_STATE_TTL_SEC`, `RABBITMQ_MANAGEMENT_URL`, `RATE_LIMIT_DEFAULT_COOLDOWN_SEC`, `RATE_LIMIT_MAX_WAIT_SEC`, `TRANSCRIPT_LANGUAGES`. Unused `.env` keys: `ALLOWED_EXTENSION_ORIGIN`, `BACKOFF_JITTER`, `CHAOS_ENABLED`, `GEMINI_LIMIT_SAFETY_FACTOR`, `LLM_MAX_WAIT_SEC`, `PUBLIC_BASE_URL`, `COMPOSE_PROJECT_NAME` (harmless) |
| `docker compose config --format json` (summarised) | Renders without error. **No empty variable in any service.** `NOTION_CLIENT_SECRET` → gateway only; `GEMINI_API_KEY` → worker only; `JOB_PAYLOAD_ENCRYPTION_KEY` → gateway + worker. All backend services use `notes-agent-backend:dev`, target `runtime`; `tests` only in profile `test` |
| Env names: running containers vs new config | gateway **+** `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`, `NOTION_REDIRECT_URI`, `NOTION_RPS_LIMIT`, `NOTION_VERSION`, `OAUTH_STATE_TTL_SEC`. worker **+** `FRAME_MAX_WIDTH`, `FRAME_SOURCE_MAX_HEIGHT`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_THINKING_LEVEL`, `MAX_FRAMES`, `NOTION_DEFAULT_PARENT_ID`, `NOTION_VERSION`, `TRANSCRIPT_CHUNK_TOKENS`. outbox-publisher: unchanged. Nothing removed |
| Extension URL check (booleans) | `GATEWAY_BASE_URL` = `http://localhost:8000`; `GATEWAY_PORT` = 8000 → **match**; `PUBLIC_BASE_URL` equals it; `NOTION_REDIRECT_URI` equals the extension's `chromiumapp.org/notion` URI: **true**. The gateway is published on `127.0.0.1:8000`, so `localhost` is correct |
| `.env` mtime | 2026-11-20 22:10 IST, before the Phase C Part 1 real-settings check with image `70dce0098cd2` (gateway and worker settings loaded OK, OAuth configured, default parent parsed), so that check still applies |
| Non-secret sizing | `WORKER_REPLICAS=2`, `WORKER_MEM_LIMIT=768m`, `GATEWAY_MEM_LIMIT=256m`, `MAX_VIDEO_DURATION_SEC=1200`, `MAX_FRAMES=8`, `GEMINI_MODEL=gemini-3.8-flash`, `NOTION_VERSION=2026-03-11` |
| `docker system df` | 11.2 GB images (6.8 GB reclaimable), 6.6 GB build cache; no space problem visible |

Housekeeping: one inspection command briefly wrote two env-**name** lists to `/tmp` (Git Bash). They
contained no values and were deleted immediately.

## 3. Running stack vs latest code

| Component | Running | Repository (working tree) |
|---|---|---|
| Backend image | `8cb661395cde` (Phase B Part 1 code, deployed 2026-11-20 02:06 IST) | builds to the equivalent of `70dce0098cd2` (Phase B Parts 1–4 + Phase C Part 1) |
| Gateway | `/jobs`, `/healthz`; **no OAuth**; does not unseal `nva1.` tokens | + `POST /oauth/notion/authorize`, `/oauth/notion/callback`, sealed-token unsealing, new OAuth/Notion env |
| Workers | stage TRANSCRIBING only (later stages → `PIPELINE_STAGE_UNAVAILABLE`) | all four stages (Gemini, FFmpeg frames, Notion publishing), `notion.com` result host, default parent as a URL |
| Outbox publisher | same code paths | no relevant change (only the image is rebuilt) |
| Migrations | 001–004 applied | 001–004 → **none pending** |
| postgres / rabbitmq / redis | `postgres:16`, `rabbitmq:4.4.1-management`, `redis:7-alpine` | same images and config → **no change** |
| Extension | not loaded in Chrome (unknown) | Phase C Part 3; gateway URL matches the deployment |

## 4. Deployment readiness

**No hard blockers found.**

- **Rebuild and recreate:** `gateway`, `worker` (×2), `outbox-publisher`; `migrate` runs once more and
  finds nothing to apply.
- **Leave untouched:** `postgres`, `rabbitmq`, `redis` (same image and config, so Compose keeps them
  running; volumes untouched).
- **Data and queue risk: minimal.** No non-terminal job, no unpublished outbox row, no queued or unacked
  message, no credential mailbox. The 2 existing FAILED jobs stay readable.
- **Configuration:** the existing `.env` is sufficient; every required variable is set and every missing
  one has a default.

Risks and caveats (not blockers):
1. **No exact rollback image.** `8cb661395cde` is untagged and no longer inspectable. A rollback means
   rebuilding from Git `5a63cdd` (full pipeline without OAuth), which differs from what runs now.
   Recommendation: tag the new image before deploying (step 2 below), so future rollbacks are possible.
2. **Uncommitted code would be deployed.** The image is built from the working tree (Phase C Parts 1–3
   uncommitted). For traceability the user may want to commit first (user's decision; I do not run Git).
3. **The real external services are untested end to end:** first real Notion OAuth (whether Notion
   accepts the `chromiumapp.org` redirect), real Gemini output on a full video, a real yt-dlp video
   download at the pinned `yt-dlp==2026.10.26` (YouTube changes often), and real Notion File Uploads.
   The first real job is the test; start with a short public video.
4. **Gemini quota:** 1 of 16 daily requests used (Pacific day). A short video needs about 1–2 requests;
   avoid many test jobs today.
5. **The extension must be reloaded in Chrome** after loading the new files. Chrome will newly ask for
   "Read and change your data on www.youtube.com" (content script).
6. **Worker memory:** 768 MB per worker with FFmpeg on ≤ 720p. Tested only with synthetic videos and one
   real short video; watch memory on the first long video.
7. `docker compose up -d` also re-runs `migrate`, which is harmless (advisory lock + `schema_migrations`).

## 5. Recommended ordered plan (NOT executed)

| # | Step | Command |
|---|---|---|
| 0 | (User, optional) commit Phase C Parts 1–3 for traceability | (user's Git) |
| 1 | Pre-check: nothing in flight | `curl -s http://localhost:8000/healthz` (expect `work_queue_messages: 0`, `outbox_pending: 0`) |
| 2 | Build the backend runtime image from the current tree (cache makes this fast; guarantees the image matches the code) | `docker compose build migrate` (builds `notes-agent-backend:dev`, target `runtime`; gateway, worker and publisher share it) |
| 3 | Tag it for rollback | `docker tag notes-agent-backend:dev notes-agent-backend:phaseC-2026-11-20` |
| 4 | Recreate the backend services. Compose first re-runs `migrate` to completion (`service_completed_successfully`; nothing to apply). postgres, rabbitmq and redis have unchanged config and are only checked, not recreated | `docker compose up -d gateway outbox-publisher worker` |
| 5 | Verify the containers | `docker compose ps` (all healthy, new image ID `docker inspect --format '{{.Image}}' notes-agent-gateway-1`) |
| 6 | Verify the health check | `curl -s http://localhost:8000/healthz` (2 live workers, 2 consumers) |
| 7 | Verify the new code is live (no state besides one expiring OAuth state in Redis) | `curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8000/oauth/notion/authorize` → 200 (not 404) |
| 8 | Verify the worker stages | `docker compose logs --since 5m worker \| grep '"stages"'` → all four stages |
| 9 | Verify the logs | `docker compose logs --since 5m gateway worker outbox-publisher \| grep -ciE "traceback\|error"` → 0 |
| 10 | (User) reload the extension and do the first real Notion sign-in, then one short public video | manual, separate approval |

Rollback (if needed after step 4): the exact previous image is gone (risk 1). The closest rollback is
to rebuild from committed `5a63cdd` (user checks it out), then `docker compose build migrate` and
`docker compose up -d gateway outbox-publisher worker`. After step 3, later rollbacks can use the
`phaseC-2026-11-20` tag.

## 6. Unresolved issues / next steps

- Awaiting the user's approval to execute steps 2–9 (and, separately, step 10).
- Decide whether to commit Phase C Parts 1–3 before deploying (user).
- The handover document is not updated in this session (by instruction); it should be updated after the
  deployment.
