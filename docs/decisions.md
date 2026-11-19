# Architecture Decision Record

Project: **Distributed Multimodal Video Note-Taking Agent** (see `PROJECT_SPEC.pdf`, the source of truth).
Status of this document: approved decisions as of Phase 0 (2026-11-19). Each entry states what was decided,
why, and what it does **not** guarantee.

---

## ADR-01 Components and runtime

- Four independently running components (spec §1.1): Chrome extension (MV3, plain JS, no build step),
  API Gateway (Python 3.12 / FastAPI), RabbitMQ 4 broker, worker pool (≥ 2 replicas).
- Supporting services in Docker Compose: PostgreSQL 16 (job state), Redis 7 (global rate limits,
  shared cooldowns, no job data), Outbox Publisher (separate container).
- Workers: one generic image, scaled with `docker compose up --scale worker=N`. No fixed
  `container_name` on scalable services. Compose project name: `notes-agent`.
- FFmpeg and yt-dlp exist **only inside the worker image**. Nothing is installed natively on Windows.
- Resource-conscious limits (16 GB laptop, Docker VM ≈ 7.6 GiB) are configured via `*_MEM_LIMIT`
  variables in `.env`.

## ADR-02 Transactional outbox for job publication

- `POST /jobs` writes the job row **and** an outbox event in **one PostgreSQL transaction**.
- A separate Outbox Publisher claims pending events (`FOR UPDATE SKIP LOCKED`), publishes them as
  persistent messages, waits for **publisher confirms**, and only then marks them published.
- A PostgreSQL commit and a RabbitMQ publish are **not atomic**. A publisher crash after the confirm
  and before marking the row yields a **duplicate message**; consumers are idempotent (ADR-05).
- Outbox events contain **no secrets**.

## ADR-03 Notion token handling: encrypted per-job RabbitMQ credential mailbox

- The extension supplies the Notion token with every `POST /jobs` (spec §5).
- The token is **never stored in PostgreSQL** and never placed in job, retry or outbox messages.
- The gateway encrypts the token (Fernet, `JOB_PAYLOAD_ENCRYPTION_KEY`) and publishes it, with publisher
  confirm, into a per-job durable queue `cred.<job_id>` **before** committing the job transaction.
  If the confirm fails, the request returns 503 and no job is created. If the commit fails, the orphaned
  queue expires by itself.
- **Explicit note:** the encrypted token **is temporarily persisted by the RabbitMQ broker** (durable
  queue on the broker's disk) until the job reaches a terminal state or the queue expires
  (`CREDENTIAL_TTL_HOURS`, default 24). It is *not* true that the token is stored nowhere.
- Workers read the credential with `basic.get` (manual ack) + `nack(requeue=true)` on the SAME message
  (non-consuming read; nothing is republished), so worker crashes and retries never lose it and the
  message keeps its original TTL (verified: repeated reads do not extend it).
- A crash between `get` and `nack` leaves the message unacked until RabbitMQ notices the dead connection
  (immediate for a killed local process; up to the AMQP heartbeat timeout for a network partition). A
  reader that finds the mailbox queue present but empty therefore waits, bounded by
  `CREDENTIAL_READ_WAIT_SEC` (default 90 s), instead of failing; a missing queue fails immediately.
  Normally only the one worker holding the job's advisory lock reads a job's mailbox. The mailbox is deleted when the job reaches
  COMPLETED / COMPLETED_WITH_WARNINGS / FAILED / DEAD_LETTERED.
- Missing or expired mailbox → job FAILED with `NOTION_CREDENTIALS_UNAVAILABLE` (resubmit).
- TTL semantics (verified in Phase 1): the **message** expires `CREDENTIAL_TTL_HOURS` after submission
  (absolute bound). The **queue** expires after being *unused* for that long; any `queue.declare`
  (including passive) or `basic.get` by a worker resets the queue's idle timer. An emptied queue left
  behind after the message expired is harmless, holds no secret, and is removed by `x-expires`.
- Tokens are never logged or echoed in error responses (log redaction filter + fixed error codes).

## ADR-04 Idempotency keys for `POST /jobs`

- The extension sends an `Idempotency-Key` header (UUID, generated once per "Take Notes" click and
  reused for retries of that same submission).
- The gateway stores `(idempotency_key, request fingerprint, job_id)` with a TTL
  (`IDEMPOTENCY_KEY_TTL_HOURS`). A repeated key with the same request returns the **original** 202
  response (same `job_id`) and creates no new job or credential mailbox. The same key with a different
  request body returns 422 `IDEMPOTENCY_KEY_REUSED`.
- The fingerprint excludes the Notion token value (only a keyed hash is stored, so the token can't be
  recovered from it).
- Without the header, every request creates a new job (spec-compatible).

## ADR-05 Worker reliability and idempotency

- RabbitMQ quorum queues, manual acknowledgements, `prefetch=1`.
- Acknowledge **only after** the stage checkpoint is durably committed (or after a retry / DLQ message
  has been confirmed by the broker).
- SIGKILLed worker → its connection drops → RabbitMQ redelivers the unacknowledged message.
- Per-job PostgreSQL advisory lock (released automatically when the session dies) + compare-and-set
  state transitions; messages for terminal jobs are acknowledged and dropped.
- Bounded retries: `MAX_ATTEMPTS`, `BACKOFF_SCHEDULE_SEC` (+ jitter) via TTL delay queues; exhausted →
  DEAD_LETTERED + `jobs.dlq`. Quorum `x-delivery-limit` as a backstop against messages that crash every worker.
- The broker image is pinned to `rabbitmq:4.4.1-management` (same digest as the image the tests ran
  against), because the semantics below are version-dependent. Re-run the suite before changing it.
- Verified broker semantics (RabbitMQ 4.4.1, quorum queues, Phase 1 review):
  - a consumer crash / closed channel **does** increment `x-delivery-count`, so `x-delivery-limit`
    bounds crash loops (message then dead-lettered with reason `delivery_limit`);
  - an explicit `nack(requeue=true)` does **not** increment it, so it can NOT bound explicit requeues.
    Therefore the worker never relies on nack-requeue for retries: unexpected errors are counted in the
    `x-processing-attempts` header (republish confirmed copy, then ack) and after `QUEUE_DELIVERY_LIMIT`
    attempts the message is rejected (`requeue=false`) → dead-lettered with reason `rejected`.
    (Interim Phase 1 behaviour; Phase 2 replaces it with classified errors + exponential backoff.)
  - lock-busy duplicate deferrals republish a new message and therefore never consume attempts.
- **Phase 2 REQUIREMENT (not done in Phase 1):** when a message reaches `jobs.dlq`, PostgreSQL is NOT
  updated — the job stays in its last non-terminal status forever. Phase 2 must set `DEAD_LETTERED`
  (with an `error`), delete the credential mailbox, and use at-least-once dead-lettering. A strict
  `xfail` test (`test_phase2_requirement_dead_lettered_job_state`) tracks this gap.
- Result integrity (migration 002 + worker validation): only COMPLETED/COMPLETED_WITH_WARNINGS carry a
  `result`, which must hold an https Notion URL and frame counts; only FAILED/DEAD_LETTERED carry `error`.
- **Real orchestration only.** No simulated stages or fake progress in product code. Stages not yet
  implemented make a job end FAILED with `PIPELINE_STAGE_UNAVAILABLE`. Crash and redelivery behaviour is
  proven with pytest fixtures against real RabbitMQ / PostgreSQL containers.

## ADR-06 LLM: Google Gemini API, free tier only

- Provider: Gemini Developer API (`google-genai` SDK). No Anthropic dependency.
- Initial model: `GEMINI_MODEL=gemini-3.8-flash`. Per the official pricing page (checked 2026-11-18),
  it is stable and "Free of charge" on the Free tier, with a 1,048,576 input-token context.
- Free-tier quotas are **not published** in the docs; they are per project and shown in AI Studio.
  The user's values go in `GEMINI_RPM_LIMIT` / `GEMINI_TPM_LIMIT` / `GEMINI_RPD_LIMIT`.
- **No paid usage:** no billing account on the project, no automatic switch to another model or tier.
  Any paid alternative requires explicit user approval.
- Global limiter: atomic Redis token buckets (RPM, input TPM, RPD with reset at midnight Pacific),
  shared cooldown after any 429. The SDK's built-in auto-retry is disabled so retries stay visible.
- Daily quota exhausted → global cooldown and bounded retries → DEAD_LETTERED
  `LLM_DAILY_QUOTA_EXHAUSTED` (approved D4).
- 429s cannot be made impossible (Google: limits "not guaranteed"); they are handled, not prevented.
- Data use (free tier): Google may use prompts and responses to improve products, and human reviewers
  may read them. Only public video transcripts are sent. Apps for users in the EEA, Switzerland or the
  UK require paid services.

## ADR-07 Notion OAuth and publishing

- Code exchange and token refresh run **on the gateway only** (the client secret never reaches the
  extension; Notion's docs require a server for the exchange).
- Redirect URI candidate (Mode A): `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion`
  (via `chrome.identity.launchWebAuthFlow`). **Not verified with Notion**: the official docs do not
  state the accepted URI forms. The localhost-callback fallback (Mode B) is **also unverified** and must
  not be assumed to work. Verification needs explicit user approval (D2).
- Destination: `NOTION_DEFAULT_PARENT_ID` ("Video Notes" page), selected by the user in Notion's page
  picker during authorization.
- Invalid or expired token during a job → FAILED `NOTION_UNAUTHORIZED` with refresh-and-resubmit
  instructions (approved D3).
- Publishing is resumable without duplicate pages: a job marker block is included in the initial create
  request; the page ID is saved immediately; after a crash the page is found by its marker (paginated
  child listing); appending resumes by matching existing blocks against the saved block plan.
  Limits respected: ≤ 100 blocks per request, ≤ 500 KB payload, ≤ 2000 characters per rich-text item.
  Frame bytes stay in PostgreSQL and expired (1-hour) uploads are re-uploaded rather than dropped.

## ADR-08 Extension identity

- A stable extension ID comes from the public key in `extension/manifest.json` (`key`).
- Extension ID: `kkkeigfecmfhmainhbkcpdhjnloifbdf`.
- The private key lives in `.secrets/extension-key.pem` (gitignored). It is only needed to pack a
  `.crx`; loading the unpacked extension does not need it. Keep a backup outside the repository.
