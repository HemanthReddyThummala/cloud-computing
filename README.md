# Distributed Multimodal Video Note-Taking Agent

Click **Take Notes** on a YouTube video (≤ 20 minutes). The system then:
1. fetches the timestamped transcript;
2. has Gemini write structured study notes and pick the visually important moments;
3. extracts the **real video frames** at those moments with FFmpeg;
4. publishes a Notion page with headings, bullet points, timestamp links and captioned images.

Everything is driven from a Chrome extension. A queue-backed pool of workers does the processing and
respects every external API's rate limits.

| | |
|---|---|
| **Base URL** | `http://localhost:8000` (gateway; bound to `127.0.0.1`, port `GATEWAY_PORT`) |
| **Health check** | `GET http://localhost:8000/healthz` (gateway, PostgreSQL, RabbitMQ, Redis, live workers) |
| **Default Notion destination** | one new child page per job, under the page given in `NOTION_DEFAULT_PARENT_ID` (see [Notion](#3-notion-integration-and-default-destination)) |
| **API reference** | [`docs/api.md`](docs/api.md) |

## Components

| Service (Compose) | Role |
|---|---|
| `extension/` (Chrome, unpacked) | Take Notes button on YouTube, Notion sign-in, job submission, progress popup |
| `gateway` | FastAPI REST API: accepts jobs, reports status/results, Notion OAuth code exchange. Does no pipeline work |
| `outbox-publisher` | publishes committed job events from PostgreSQL to RabbitMQ (transactional outbox) |
| `worker` (2 replicas) | runs the pipeline stages TRANSCRIBING → SUMMARIZING → EXTRACTING_FRAMES → PUBLISHING |
| `rabbitmq` | durable broker (quorum queues, at-least-once, retry delay queues, dead-letter queue) |
| `postgres` | job state, events, stage checkpoints, outbox |
| `redis` | shared rate limiter for Gemini / Notion / YouTube, worker heartbeats |
| `migrate` | one-shot database migration, run automatically before the gateway and workers start |

Job states: `QUEUED → TRANSCRIBING → SUMMARIZING → EXTRACTING_FRAMES → PUBLISHING → COMPLETED`
(or `COMPLETED_WITH_WARNINGS` if some frames were omitted). It ends `FAILED` for non-retryable errors and
`DEAD_LETTERED` when the retry budget is exhausted.

---

## 1. Prerequisites

- Docker Desktop (or Docker Engine) with Docker Compose v2.
- Google Chrome.
- A Google AI Studio API key (free tier) for Gemini.
- A Notion account where you can create a public integration.
- No other tools are needed on the host. Node.js is only needed to run the extension's tests.

## 2. Configure `.env`

```bash
cp .env.example .env
```

Then edit `.env`. **Never commit `.env`.**

1. Choose your own values for `POSTGRES_PASSWORD` and `RABBITMQ_DEFAULT_PASS`, and put the **same**
   values into `DATABASE_URL` and `RABBITMQ_URL`.
2. Generate `JOB_PAYLOAD_ENCRYPTION_KEY` (a Fernet key) and paste the output:

   ```bash
   docker run --rm python:3.12-slim python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
   ```
3. Set `GEMINI_API_KEY` (from <https://aistudio.google.com/apikey>).
4. Set the Notion values from step 3 below: `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`,
   `NOTION_REDIRECT_URI`, `NOTION_DEFAULT_PARENT_ID`.

### Environment variables

All variables, with defaults and comments, are in [`.env.example`](.env.example). The required and most
important ones:

| Variable | Used by | Meaning / default |
|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | postgres | database credentials (**required**) |
| `DATABASE_URL` | gateway, publisher, workers, migrate | `postgresql://USER:PASSWORD@postgres:5432/DB` (**required**) |
| `RABBITMQ_DEFAULT_USER`, `RABBITMQ_DEFAULT_PASS` | rabbitmq | broker credentials (**required**) |
| **`RABBITMQ_URL`** | gateway, publisher, workers | **broker connection string**, `amqp://USER:PASSWORD@rabbitmq:5672/` (**required**) |
| `REDIS_URL` | gateway, workers | `redis://redis:6379/0` (**required**) |
| `JOB_PAYLOAD_ENCRYPTION_KEY` | gateway, workers | Fernet key that encrypts the Notion token in transit (**required**, secret) |
| **`GEMINI_API_KEY`** | workers | **LLM API key** (secret) |
| **`GEMINI_MODEL`** | workers | **LLM model name**: `gemini-3.5-flash-lite`. Note: `docker-compose.yml` pins this value for the workers, so the `.env` value is ignored there |
| `GEMINI_THINKING_LEVEL` | workers | `low` |
| `GEMINI_REQUEST_TIMEOUT_SEC` | workers | per-request timeout, `300` |
| **`GEMINI_RPM_LIMIT` / `GEMINI_TPM_LIMIT` / `GEMINI_RPD_LIMIT`** | workers | **configured LLM rate limit**, shared by all workers through Redis: `4` requests/min, `200000` input tokens/min, `16` requests/day (= the AI Studio free-tier limits × 0.8 safety margin; set them from your own project's limits) |
| `NOTION_RPS_LIMIT` | gateway, workers | Notion requests per second per connection, `2.5` |
| `RATE_LIMIT_MAX_WAIT_SEC`, `RATE_LIMIT_DEFAULT_COOLDOWN_SEC` | workers | in-process wait for a permit (`60`); cooldown after a 429 without Retry-After (`10`) |
| **`NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`** | gateway | **Notion OAuth client credentials** (secret: never sent to the extension or the workers) |
| `NOTION_REDIRECT_URI` | gateway | `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion` |
| `NOTION_DEFAULT_PARENT_ID` | workers | default Notion destination page (32-hex page ID or full page URL) |
| `NOTION_VERSION` | gateway, workers | Notion API version, `2026-03-11` |
| **`MAX_ATTEMPTS`** | workers | **retry budget**: total executions per job, including the first (`5`) |
| **`BACKOFF_SCHEDULE_SEC`** | gateway, publisher, workers | **backoff steps** in seconds; the last step repeats: `10,30,90` → delays 10, 30, 90, 90 s |
| **`GEMINI_UNAVAILABLE_BACKOFF_SEC`** | workers | backoff for Gemini 503/504 only: `60,180,600`; a valid Retry-After raises the delay |
| `INFRA_MAX_ATTEMPTS` | workers | retry budget when Redis (the limiter) is unavailable, `20` |
| `INFRA_RETRY_DELAY_SEC`, `LOCK_RETRY_DELAY_SEC` | gateway, publisher, workers | delay when Redis is unavailable (`30` s) and while another worker holds the job lock (`5` s); each value also defines a delay queue |
| `QUEUE_DELIVERY_LIMIT` | gateway, publisher, workers | broker redelivery limit before a message is dead-lettered (`10`) |
| `MAX_VIDEO_DURATION_SEC` | workers | maximum video length, `1200` (20 min; longer videos fail with `VIDEO_TOO_LONG`) |
| `TRANSCRIPT_LANGUAGES`, `TRANSCRIPT_CHUNK_TOKENS`, `MAX_FRAMES` | workers | preferred caption languages (`en`); chunk size for long transcripts (`12000`); maximum frames per video (`8`) |
| `GATEWAY_PORT` | gateway | host port, `8000` (if changed, also change `extension/config.js` and the extension manifest's `host_permissions`) |
| `WORKER_REPLICAS` | workers | number of workers started by `up` (`2`) |
| `GEMINI_LIMITER_NAMESPACE` | workers | **empty** (default) = the original limiter keys. Set it only when the workers use a Gemini key from **another Google Cloud project** (quotas are per project): e.g. `alt` gives that project its own counters `ratelimit:gemini-alt:{rpm,tpm,rpd:<day>}` and cooldown `ratelimit:cooldown:gemini-alt` (ADR-23). Notion/YouTube keys are never namespaced. See "Using a Gemini key from another project" below |
| `DEMO_FAULTS_ENABLED` | workers | demo fault switch for live failure demos (ADR-22); **`false`**; keep it false in normal use |

## 3. Notion integration and default destination

1. At <https://www.notion.so/profile/integrations> create a **public** integration.
   - Capabilities: Read content, Insert content (Update content optional); no user information.
   - **Redirect URI:** exactly `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion`. The
     extension ID is fixed by the `key` in `extension/manifest.json`, so this URI is the same on every
     machine.
   - Copy its client ID and client secret into `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET`, and set
     `NOTION_REDIRECT_URI` to the URI above.
2. Create a page in Notion, for example "Video Notes". Put its ID or URL into `NOTION_DEFAULT_PARENT_ID`.

**Default destination behaviour:**
- Every job creates **one new child page** under `NOTION_DEFAULT_PARENT_ID`, titled with the video
  title.
- Its first block records the job ID and the source URL.
- A retried or redelivered job finds and completes its own page instead of creating a duplicate.
- Jobs submitted with the Take Notes button always use this default.
- API clients may pass `notion_parent_id` in `POST /jobs` to choose another page.

When connecting Notion in the extension, **share the default page with the integration** in Notion's
page picker. If the page is not shared (or does not exist), jobs fail at PUBLISHING with
`NOTION_OBJECT_NOT_FOUND`. Without any destination they fail with `NOTION_PARENT_MISSING`.

## 4. Start the stack

```bash
docker compose up -d --build
docker compose ps -a                    # all services "healthy"; migrate "exited (0)"
curl http://localhost:8000/healthz      # {"status":"ok", ... "live_workers":2}
```

This starts PostgreSQL, RabbitMQ, Redis, the migration, the gateway, the outbox publisher and **two
workers**. The RabbitMQ management UI is at <http://localhost:15672> (localhost only; your
`RABBITMQ_DEFAULT_*` credentials).

## 5. Load the Chrome extension (no build step)

1. Open `chrome://extensions` and switch on **Developer mode**.
2. Click **Load unpacked** and select the **`extension/`** folder of this repository.
3. Check that the extension ID is `kkkeigfecmfhmainhbkcpdhjnloifbdf`.
4. Click the extension icon, then **Connect Notion**. Approve access and share your default page.
5. Open a YouTube video and click **Take Notes** in the player toolbar.
6. Open the extension popup to follow Queued → Transcribing → Summarizing → Extracting Frames →
   Publishing → Done, then click **View in Notion**.

More detail is in [`extension/README.md`](extension/README.md).

## 6. REST API (summary)

| Method | Path | Result |
|---|---|---|
| `POST` | `/jobs` | `202` + `Location: /jobs/{id}`; body `{job_id, status, video_id}`. Request `{youtube_url, notion_token, notion_parent_id?}` (the extension supplies the token) |
| `GET` | `/jobs/{id}` | status, progress, title, duration, warnings, error, result, retry; `404` if unknown |
| `GET` | `/jobs/{id}/results` | `409` until finished, then the Notion page URL and frame counts, or the error |
| `GET` | `/healthz` | dependency health |
| `POST` | `/oauth/notion/authorize`, `/oauth/notion/callback` | Notion sign-in (used by the extension) |

Errors always use `{"error": {"code", "message"}}`. Full contract: [`docs/api.md`](docs/api.md).

## 7. Scaling and crash recovery

```bash
docker compose up -d --no-deps --scale worker=3 worker   # run 3 workers (touches only workers)
docker compose ps worker                       # list the worker containers
docker kill notes-agent-worker-1               # kill one worker (e.g. mid-job)
docker compose up -d --no-deps --scale worker=2 worker   # back to 2 workers (restarts a killed one)
docker compose logs -f worker                  # follow worker logs
```

A killed worker's unacknowledged job is redelivered to another worker and resumes from its last
completed stage. Messages are only acknowledged after the stage's result is stored.

**Retry / DLQ behaviour:**
- Retryable failures are retried after `BACKOFF_SCHEDULE_SEC` delays until `MAX_ATTEMPTS`; the job
  then becomes `DEAD_LETTERED` and a copy goes to the `jobs.dlq` queue.
- For a faster demonstration, set `BACKOFF_SCHEDULE_SEC=2,4,8` and `MAX_ATTEMPTS=3` in `.env` and run
  `docker compose up -d`.

### Failure demonstrations (spec §6)

Step-by-step procedures, with expected results and evidence queries, are in
[`docs/FAILURE_DEMONSTRATION_RUNBOOK.md`](docs/FAILURE_DEMONSTRATION_RUNBOOK.md). The DLQ, graceful
frame degradation and Gemini 429 demos use a **demo fault switch** (ADR-22). It is **off by default**, and
a fault only happens for one video an operator explicitly arms:

```bash
DEMO_FAULTS_ENABLED=true docker compose up -d --no-deps --no-build --scale worker=2 worker
docker compose exec worker python -m notesagent.ops.demo_faults arm <dlq|gemini_429_once|frames_partial|frames_all> --video <VIDEO_ID>
# then click Take Notes on that video; afterwards disable again:
docker compose up -d --no-deps --no-build --scale worker=2 worker
```

An arming expires after 15 minutes and affects only the first job of that video. The injected faults
go through the normal retry / rate-limit / warning / dead-letter code.

### Using a Gemini key from another project (ADR-23)

Gemini quotas belong to a Google Cloud project, and so do the limiter's counters when
`GEMINI_LIMITER_NAMESPACE` is set.

1. Put the other project's key and a namespace into a separate git-ignored file that you create
   yourself, e.g. `.env.gemini-alt`. Never commit it.
   ```
   GEMINI_API_KEY=<other project's key>
   GEMINI_LIMITER_NAMESPACE=alt
   ```
2. Layer it over `.env` for the workers only:
   ```bash
   docker compose --env-file .env --env-file .env.gemini-alt up -d --no-deps --no-build --scale worker=2 worker
   ```
3. Switch back to the original project and counters by running the plain command (only `.env` is used):
   ```bash
   docker compose up -d --no-deps --no-build --scale worker=2 worker
   ```

Notes:
- With `--env-file`, Compose stops loading `.env` automatically, so both files are listed.
- Lower `GEMINI_RPD_LIMIT` (and the other limits) in that file if the other project's quota is
  smaller.
- The original counters are never changed by the switch.

## 8. Tests

```bash
# backend (isolated test database / vhost / Redis db; no real external API calls)
docker compose --profile test build tests
docker compose --profile test run --rm --no-deps tests pytest -q
# extension (Node ≥ 20, no dependencies)
cd extension && npm test
```

On Git Bash for Windows, prefix the backend command with `MSYS_NO_PATHCONV=1`.

## 9. Stopping

```bash
docker compose down        # stops containers; data volumes are kept
```

Do **not** add `-v` unless you want to delete all job data.

## Further documentation

- **Specification compliance** (each spec requirement → implementation → evidence, with known gaps):
  [`docs/SPEC_COMPLIANCE.md`](docs/SPEC_COMPLIANCE.md).
- Design decisions: [`docs/decisions.md`](docs/decisions.md) (ADR-01 … ADR-23).
- Live failure demonstrations (step by step): [`docs/FAILURE_DEMONSTRATION_RUNBOOK.md`](docs/FAILURE_DEMONSTRATION_RUNBOOK.md).
- REST API: [`docs/api.md`](docs/api.md).
- Project status and verification evidence: [`docs/PROJECT_STATUS_AND_HANDOVER.md`](docs/PROJECT_STATUS_AND_HANDOVER.md).
- Specification: `PROJECT_SPEC.pdf`.
