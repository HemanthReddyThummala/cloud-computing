# Session log — Phase B Part 1: deployment verification (TRANSCRIBING stage)

- **Started:** 2026-11-20 02:05 (host clock)
- **Branch:** `phase-2`; HEAD `1aa6863` (user's commit); 0 staged; 17 status entries (uncommitted Phase B
  Part 1 work, preserved). No Git operations were performed in this session.
- **Objective:** deploy the current backend (shared rate limiter + TRANSCRIBING stage) to the dev stack
  and verify it with a real public video. No new pipeline stage. Preserve all PostgreSQL / RabbitMQ /
  Redis data.

## 1. Initial state
| Item | State |
|---|---|
| gateway, outbox-publisher, worker ×2, migrate | `notes-agent-backend:dev` = `63cf80e0e2cd` (built 2026-11-19 22:42 — before the rate limiter and the TRANSCRIBING stage); running/healthy 3 h |
| postgres / rabbitmq / redis | healthy (15 h / 12 h / 15 h) |
| Volumes | `notes-agent_pgdata`, `notes-agent_rabbitmqdata`, `notes-agent_redisdata` |
| DB | migrations 001–004; 1 job (FAILED, the earlier deployment smoke job); 0 checkpoints |
| RabbitMQ `/` | all queues empty; `jobs.work.v2` 2 consumers, `jobs.dlq.audit` 2 consumers |
| Test image | `notes-agent-backend:test` `8f818fe5da7a` (built 01:48, current code) |

## 2. Commands and results
| Time | Command | Result |
|---|---|---|
| 02:05 | `docker compose up -d --dry-run` (before the build) | only worker-1/2 would be recreated (their Compose environment gained new variables); infrastructure "Running" |
| 02:05:51 | `docker compose build migrate` | `notes-agent-backend:dev` → **`8cb661395cde`** (layers cached from the 01:48 test build of the same code). Verified in the image: `worker/transcribe.py`, `worker/youtube.py`, `yt-dlp 2026.10.26`, `youtube-transcript-api 1.2.5` |
| 02:05 | `docker compose up -d --dry-run` (after the build) | **Recreate**: migrate, gateway, outbox-publisher, worker-1, worker-2; postgres/rabbitmq/redis **Running** |
| 02:06:01–07 | `docker compose up -d` | exactly those 5 app containers recreated. Migrate log: `migrations up to date, applied_now: []` |
| 02:06 | `docker compose ps`; `docker inspect` | gateway, outbox-publisher, worker ×2 **healthy** on `8cb661395cde`; postgres/rabbitmq/redis healthy, **restarts 0**, start times unchanged (05:43Z / 08:27Z / 05:43Z on 2026-11-19) |
| 02:06 | `docker exec worker-N printenv …` | both workers: `MAX_VIDEO_DURATION_SEC=1200`, `TRANSCRIPT_LANGUAGES=en`, `GEMINI_RPM_LIMIT=4`, `NOTION_RPS_LIMIT=2.5` |
| 02:06 | worker logs `worker started` | both: `stages=['TRANSCRIBING']`, `work_queue=jobs.work.v2`, `draining_legacy=False` |
| 02:06 | `rabbitmqctl list_queues -p /` | `jobs.work.v2` 2 consumers, `jobs.dlq.audit` 2 consumers, all queues 0 messages |
| 02:06 | `curl /healthz` | **HTTP 200** `status ok` (postgres ok, outbox_pending 0; rabbitmq ok, 2 consumers; redis ok, live_workers 2) |

### Real job (public video `jNQXAC9IVRw`, "Me at the zoo", 19 s)
Command: `POST /jobs` with `youtube_url=https://www.youtube.com/watch?v=jNQXAC9IVRw` and a **dummy
placeholder** `notion_token` (not a real credential; no stage reads it yet).

- **Response:** `202 Accepted`, `Location: /jobs/9302ffd9-b1c9-4606-9585-0c87eb988ec0`,
  `{"status":"QUEUED","video_id":"jNQXAC9IVRw"}`.
- **Polling `GET /jobs/{id}`:** 02:06:33 `TRANSCRIBING` (progress 10) → 02:06:35 `FAILED` (progress 30),
  `video_title "Me at the zoo"`, `duration_sec 19`, error
  `PIPELINE_STAGE_UNAVAILABLE: "The SUMMARIZING stage is not available in this build of the worker."`.
  `retry null`. `GET /results` → 200 with the same error.
- **`job_events`:** submitted → claimed (QUEUED→TRANSCRIBING, worker `4623b707f5cd-1`) → transition
  TRANSCRIBING→**SUMMARIZING** (+2.4 s) → transition SUMMARIZING→FAILED. SUMMARIZING lasted only
  ~5 ms, too short for the 0.5 s poll.
- **`job_checkpoints`:** one TRANSCRIBING row:
  - title "Me at the zoo", `duration_sec 19`, `duration_exact 19.0`;
  - transcript `en`, manual (`is_generated false`), **6 segments**;
  - first `{start 1.2, duration 2.16, end 3.36, text "All right, so here we are, in front of the\nelephants"}`;
  - last `{start 16.881, duration 2.0, end 18.881, text "and that's pretty much all there is to\nsay"}`.
- `attempts 0`, `infra_attempts 0`, `retry NULL`; credential mailbox removed (0 `cred.*` queues).

### Over-20-minute rejection (without downloading a long video)
- **Method:** the deployed image's production stage (`make_transcribe_stage` + the real `YouTubeClient`,
  wrapped only to record calls) run once in a throwaway container, with `MAX_VIDEO_DURATION_SEC` set
  via its environment. The 19 s video's real metadata is used; no video is downloaded, and the dev
  stack and its configuration are not touched.
- **Results:**
  - `MAX=10` → `VIDEO_TOO_LONG | The video is 0:19 long; videos up to 0:10 are supported. | calls
    ['permit', 'video_info']` — rejected after the metadata request, **no transcript request**.
  - `MAX=19` (exactly the duration) → accepted, `duration 19`, calls `['permit', 'video_info',
    'permit', 'transcript_list', 'permit', 'fetch']`.
- The 1200 / 1201 boundary itself is covered by the automated tests (`test_unit_transcribe`,
  `test_transcribe_stage`).

### Log inspection (`docker compose logs gateway outbox-publisher worker`, 33 lines since the deployment)
- **Levels:** 32 INFO, 1 ERROR = `pipeline stage not available in this build` (expected, SUMMARIZING).
  0 tracebacks; 0 retry/cooldown/requeue lines.
- **Secret scan** (counts only, values never printed):
  - `POSTGRES_PASSWORD`, `RABBITMQ_DEFAULT_PASS`, `JOB_PAYLOAD_ENCRYPTION_KEY`, `DATABASE_URL`,
    `RABBITMQ_URL`, `REDIS_URL` (full values and password parts): **0 hits**.
  - `GEMINI_API_KEY` and `NOTION_CLIENT_SECRET` are empty in `.env`, so they were not checked.
  - The placeholder Notion token: **0 hits**.
- yt-dlp's JS-runtime warning does not appear (the client runs with `quiet`/`no_warnings`).

### Integration checks
- `MSYS_NO_PATHCONV=1 docker compose --profile test run --rm --no-deps tests pytest -p no:warnings -q -rfE
  tests/test_transcribe_stage.py tests/test_unit_transcribe.py tests/test_worker.py tests/test_health.py
  tests/test_api.py tests/test_ratelimit.py tests/test_topology_v2.py` (02:07:27–02:09:23):
  **124 passed** in 114.07 s. These run on the isolated test database/vhost/Redis db, with the test
  image built from the same sources.
- `python scripts/migrate_work_queue.py --check`: v2 present and bound, `jobs.work` absent, policy
  absent → `OK: complete`.
- **Final state:** all 7 services healthy; `/healthz` 200; infrastructure restarts 0; volumes unchanged.

## 3. Unresolved problems / notes
1. None blocking. The deployed stack transcribes real videos and stops honestly at SUMMARIZING (not
   implemented yet).
2. **One extra job row** was added to the dev database: the verification job (FAILED,
   `PIPELINE_STAGE_UNAVAILABLE`).
3. **Not exercised live:** YouTube 429/blocking, transcript-disabled and private videos (covered by
   automated tests with the real library exception classes). They were not provoked against YouTube.
4. yt-dlp's JavaScript-runtime deprecation stays a risk for the future frame stage.
5. The previous image `63cf80e0e2cd` is now untagged (not removed).

## 4. Git
No Git operations were performed: no staging, commit, push or identity change. HEAD `1aa6863`;
0 staged; the working tree holds the uncommitted Phase B Part 1 changes plus this log.
