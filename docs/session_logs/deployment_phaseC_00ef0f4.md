# Deployment of commit 00ef0f4 to the development stack

Date: 2026-11-20, 23:3x–23:4x IST (deployed 23:40:20–23:40:26 IST = 18:10 UTC).
Scope: replace the backend image of `gateway`, `outbox-publisher` and `worker` (×2).

Not done (by instruction):
- no change to `.env`, source code or Git;
- PostgreSQL, RabbitMQ and Redis untouched;
- no volumes deleted;
- no real Notion OAuth, no Notion API call, no YouTube job;
- extension not reloaded;
- handover not updated.

Previous read-only inspection: `predeployment_inspection_phaseC.md` (not modified).

## 1. Pre-deployment checks

| Check | Command | Result |
|---|---|---|
| Branch / HEAD | `git branch --show-current; git log --oneline -3` | `phase-2`, HEAD **`00ef0f4`** "feat: complete YouTube extension job submission and progress tracking" (parent `5a63cdd`) ✔ |
| Working tree | `git status --short` | **clean** ✔ |
| Commit content | `git show --stat 00ef0f4` | 39 files: the backend OAuth files (`config.py`, `crypto.py`, `gateway/{main,oauth,submission}.py`, `worker/runner.py`), tests, `docker-compose.yml`, `.env.example`, docs, extension. The last commit touching `backend/notesagent`, `migrations`, `requirements.txt` or `Dockerfile` is `00ef0f4` |
| Image = committed source | `docker run --rm --entrypoint sh notes-agent-backend:dev -c 'find … -exec sha256sum'` vs `sha256sum` of the working tree (= HEAD, `git diff --quiet HEAD -- backend`) | **All 48 files identical** (`notesagent/**/*.py`, `migrations/*.sql`, `requirements.txt`). The only textual difference was Windows `sha256sum`'s `*` binary marker, normalised before comparing. The Dockerfile is unchanged since the image was built. **No rebuild needed** |
| Compose image references | `docker compose config --format json` (names only) | gateway / migrate / outbox-publisher / worker → `notes-agent-backend:dev`, target `runtime`; worker `replicas: 2`; postgres:16, rabbitmq:4.4.1-management, redis:7-alpine |
| Running containers | `docker compose ps -a`, `docker inspect` | gateway, outbox-publisher, worker-1, worker-2 healthy on **`8cb661395cde`** (started 2026-11-19 20:36 UTC); migrate exited 0; infrastructure healthy, 0 restarts — **as in the inspection report** |
| Health / in flight | `curl /healthz`; read-only SQL; `rabbitmqctl list_queues -p /` | HTTP 200, 2 live workers, `outbox_pending` 0. Jobs: 2 total, **0 non-terminal**. Outbox: 2 total, **0 unpublished**. Migrations 001–004. All queues 0 messages / 0 unacked; consumers: `jobs.work.v2` 2, `jobs.dlq.audit` 2 |

No material difference from the inspection, so the deployment went ahead.

## 2. Image tag

| Command | Result |
|---|---|
| `docker tag notes-agent-backend:dev notes-agent-backend:00ef0f4` | ok |
| `docker image inspect --format '{{.Id}} {{.RepoTags}}' notes-agent-backend:00ef0f4` | `sha256:70dce0098cd24ed0c8d8ef5cce1555dbe004b3e5870e549360a3cc33de1bd437 [notes-agent-backend:00ef0f4 notes-agent-backend:dev]` ✔ |

This tag preserves **the image deployed now** (candidate `70dce0098cd2`) for future rollbacks. It does
**not** preserve the previously running image `8cb661395cde`. That image was already overwritten
before this session (see the inspection log), and its last containers were replaced by this
deployment, so it no longer exists.

## 3. Deployment

| Step | Command | Result |
|---|---|---|
| Version | `docker compose version` | v5.5.1 |
| Dry run (no changes) | `docker compose --dry-run up -d --no-build gateway outbox-publisher worker` | Would recreate `migrate`, `gateway`, `outbox-publisher`, `worker-1`, `worker-2`; postgres, rabbitmq and redis **Running** (not recreated) |
| Deploy | `docker compose up -d --no-build gateway outbox-publisher worker` | **exit 0**, 23:40:20 → 23:40:26 IST. Recreated: migrate, gateway, outbox-publisher, worker-1, worker-2. postgres / rabbitmq / redis: "Running" → "Healthy" (dependency checks only) |

- `--no-build` guaranteed that the verified existing image was used and nothing was rebuilt.
- **Migrations:** the one-shot `migrate` service is a Compose dependency of the backend services
  (`service_completed_successfully`), so it ran as part of startup. Its log:
  `"migrations up to date", "applied_now": []`, exit 0. **Nothing was applied**; `schema_migrations`
  still holds exactly 001–004.

## 4. Verification

| # | Check | Result |
|---|---|---|
| 1 | Containers | gateway, outbox-publisher, worker-1, worker-2 **running (healthy)**, 0 restarts; migrate exited (0); postgres, rabbitmq, redis healthy ✔ |
| 2 | Images | gateway, outbox-publisher, worker-1, worker-2, migrate → **`sha256:70dce0098cd2…`** (= `notes-agent-backend:00ef0f4`) ✔ |
| 3 | `/healthz` | **HTTP 200**, `status: ok`; postgres ok (outbox_pending 0); rabbitmq ok (work queue 0 messages, **2 consumers**); redis ok (**2 live workers**) ✔ |
| 4 | OAuth endpoints | See below ✔ |
| 5 | Worker stages | Both workers logged `"worker started"` with `stages: ["EXTRACTING_FRAMES", "PUBLISHING", "SUMMARIZING", "TRANSCRIBING"]` ✔ (before: TRANSCRIBING only) |
| 6 | Logs since deployment (gateway, outbox-publisher, worker, migrate) | 16 lines right after startup: **0** tracebacks, ERROR/CRITICAL or exceptions, 0 warnings. Messages: migrations up to date, broker connected ×4, gateway started, outbox publisher started, worker started ×2, uvicorn startup, health-check access lines ✔ |
| 7 | Infrastructure unchanged | postgres `1a6ab3f5345e`, started 2026-11-19 05:43:34 UTC; rabbitmq `61f90c401a5b`, 08:27:49 UTC; redis `7f7d30b7fdd0`, 05:43:33 UTC — **same image IDs and start times as before**, 0 restarts, healthy ✔ |
| 8 | No jobs or messages created | jobs: 2 (both FAILED, as before); outbox: 2, 0 unpublished; migrations 4; all queues 0 messages / 0 unacked; no `cred.*` mailbox; Redis keys only `ratelimit:gemini:{rpd:2026-11-20,tpm}` + 2 worker heartbeats ✔ |

### OAuth endpoint check (no Notion contact)

The schema was inspected first.
- `/openapi.json` lists `POST /oauth/notion/authorize` and `POST /oauth/notion/callback` (no declared
  body: the handlers parse the body themselves).
- Per `gateway/oauth.py`, `authorize` takes no body, stores one single-use state in Redis (TTL 600 s)
  and builds the Notion URL locally.
- `callback` takes JSON `{state, code}` or `{state, error}`.

| Request | Response | Meaning |
|---|---|---|
| `POST /oauth/notion/callback` `{"state": "deploy-check-unknown-state", "code": "not-a-real-code"}` | **400 `OAUTH_STATE_INVALID`** | Route live and **configured** (not 503 `OAUTH_NOT_CONFIGURED`); unknown state rejected before any exchange |
| `POST /oauth/notion/authorize` (no body) | **200**, `Cache-Control: no-store`; keys `authorization_url, expires_in, redirect_uri, state` | Endpoint `https://api.notion.com/v1/oauth/authorize`; query `client_id` (present, value not printed), `owner=user`, `redirect_uri`, `response_type=code`, `state`. `redirect_uri` = the extension's `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion` (true). The URL's state equals the state field; length 43; `expires_in` 600 |
| `POST /oauth/notion/callback` `{state: <that state>, error: "access_denied"}` | **400 `OAUTH_ACCESS_DENIED`** | Consumed the state created above without any Notion call; Redis `oauth:notion:state:*` keys afterwards: **0** |

Gateway log for these calls: `"notion authorization started"` and `"notion authorization not
granted"` plus access lines; **0** occurrences of the test state or code values. The temporary
response files were deleted from the scratchpad. Nothing was sent to Notion: the authorization URL was
only built, never opened.

## 5. Warnings / unresolved

- **Rollback:** the previous image `8cb661395cde` no longer exists. Rollback targets from now on:
  `notes-agent-backend:00ef0f4` (current), or a rebuild from an older commit.
- **Not yet verified against real services:** Notion OAuth with a real user, Gemini on a full video, a
  real yt-dlp video download plus FFmpeg on the deployed workers, and Notion page and file-upload
  creation. These need the next step (with approval).
- Gemini daily counter: 1 of 16 requests used today (Pacific day 2026-11-20).
- The handover document is **not** updated yet (by instruction).

## 6. Next step (awaiting the user's instruction)

1. Reload the unpacked extension in Chrome (ID `kkkeigfecmfhmainhbkcpdhjnloifbdf`; Chrome will ask for
   YouTube page access).
2. Connect Notion (the first real OAuth), sharing the "Video Notes" page.
3. One short public video: Take Notes → popup stages → Done → View in Notion; check the page content,
   frames and captions.
