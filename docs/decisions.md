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
- Phase 2: each outbox row carries an explicit destination (`destination_exchange` ∈ {jobs, jobs.retry,
  jobs.dlx}, `destination_routing_key`; migration 003). Submissions keep the default (`jobs`/`job.work`);
  later parts reuse the same outbox for retry scheduling and DLQ copies. An unroutable destination
  is never dropped: the publish raises and the row stays pending with `last_error`.

## ADR-09 Broker topology v2 and the drain-safe work-queue migration (Phase 2, Part 1)

- **Topology v2:** `jobs.work.v2` (quorum; `x-delivery-limit`; DLX `jobs.dlx`;
  `x-dead-letter-strategy=at-least-once` + `x-overflow=reject-publish`) bound to `jobs`/`job.work`.
  Delay queues `jobs.retry.<N>s` (quorum, `x-message-ttl=N s`, at-least-once dead-lettering back to
  `jobs`/`job.work`) bound to the `jobs.retry` exchange with key `<N>s`, one per value in
  `BACKOFF_SCHEDULE_SEC` plus `LOCK_RETRY_DELAY_SEC` and `INFRA_RETRY_DELAY_SEC`. Part 1 declares them;
  retry logic itself is Part 3.
- **Why a new queue name:** RabbitMQ refuses to redeclare a queue with different arguments, and
  deleting the running Phase 1 queue could lose messages. Services never declare or alter the legacy
  `jobs.work`; workers drain it with `basic.get` polling (no consumer) until it disappears.
- **Verified broker limitation (RabbitMQ 4.4.1):** quorum queues reject `queue.delete` with `if-empty`
  or `if-unused` (`NOT_IMPLEMENTED`, connection closed), over AMQP and the management API, even when
  empty. A delete-time "only if empty" guard is therefore impossible.
- **Decision (Option B, approved): broker-enforced write block instead.**
  `scripts/migrate_work_queue.py` → `python -m notesagent.ops.work_queue_migration`:
  ensure v2 + binding → unbind legacy → apply policy `notes-agent-legacy-work-write-block`
  (pattern `^jobs\.work$`, `{max-length: 0, overflow: reject-publish}`, priority 1000, apply-to queues)
  → verify via the management API that it is effective on `jobs.work` and on no other queue → require
  two management samples ≥ 6 s apart (stats refresh ~5 s) with ready = unacked = total = consumers = 0,
  plus a live AMQP count of 0 → delete → only then remove the policy.
  Verified on 4.4.1: with the policy, new publishes are **nacked by the broker**; messages already in the
  queue and unacked messages returned by a crashed worker are **kept** (nothing is dropped).
- **Idempotent and fail-closed:** every step reads live broker state first; a re-run resumes from any
  interruption (including "deleted but policy not yet removed"). Refuses — never overwrites — a
  same-named policy with another definition or any other policy matching `jobs.work`. Management API
  unavailable, incomplete statistics, unconfirmed policy or a failed delete → refuse; the policy is never
  removed before the queue is confirmed gone. Exit codes: 0 complete, 1 refused, 3 still draining.
- **Credentials:** the management client takes them from `RABBITMQ_URL`, sends them only as an HTTP Basic
  header, and never includes them in URLs, errors, `repr` or output. The broker user needs permission to
  manage policies (the default administrator user has it).
- **Residual window:** while both `jobs.work` and `jobs.work.v2` are bound (between steps 2 and 3) a
  message can be routed to both; duplicates are handled by the advisory lock / CAS / terminal-drop logic.

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
- Acknowledge **only after** the job's new state is durably committed in PostgreSQL: the stage
  checkpoint, or — for a failed stage — the retry / dead-letter transaction (job state + audit event +
  outbox row, ADR-11). The worker does not wait for the broker: the separate outbox publisher (ADR-02)
  delivers the retry / DLQ message to RabbitMQ with publisher confirms after the commit, at least once.
- SIGKILLed worker → its connection drops → RabbitMQ redelivers the unacknowledged message.
- Per-job PostgreSQL advisory lock (released automatically when the session dies) + compare-and-set
  state transitions; messages for terminal jobs are acknowledged and dropped.
- Bounded retries: `MAX_ATTEMPTS`, `BACKOFF_SCHEDULE_SEC` (deterministic; default 10, 30, 90 s, the
  last step repeats; ADR-11) via TTL delay queues; exhausted → DEAD_LETTERED + `jobs.dlq`. Quorum `x-delivery-limit` as a backstop against messages that crash every worker.
- The broker image is pinned to `rabbitmq:4.4.1-management` (same digest as the image the tests ran
  against), because the semantics below are version-dependent. Re-run the suite before changing it.
- Verified broker semantics (RabbitMQ 4.4.1, quorum queues, Phase 1 review):
  - a consumer crash / closed channel **does** increment `x-delivery-count`, so `x-delivery-limit`
    bounds crash loops (message then dead-lettered with reason `delivery_limit`);
  - an explicit `nack(requeue=true)` does **not** increment it, so it can NOT bound explicit requeues.
    Therefore the worker never relies on nack-requeue for retries. (Phase 1's interim header-counted
    retries were replaced in Phase 2 Part 3 by the classified, outbox-scheduled retries of ADR-11.)
  - lock-busy duplicate deferrals go through the `jobs.retry.<LOCK_RETRY_DELAY_SEC>s` delay queue
    (confirmed copy, same `message_id`, then ack) and never consume attempts.
- **DEAD_LETTERED:** worker-side exhaustion is handled since Part 3 (ADR-11): the job becomes
  `DEAD_LETTERED`, a DLQ copy is published via the outbox, the credential mailbox is deleted.
  **Still open (Part 4):** messages dead-lettered by the *broker* (crash loops hitting
  `x-delivery-limit`, malformed messages) do not yet update PostgreSQL — the DLQ audit consumer.
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
- Global limiter: implemented in ADR-13 (atomic Redis Lua checks: sliding-window RPM and input TPM,
  RPD per Pacific day; shared cooldown after any 429). The SDK's built-in auto-retry is disabled so retries stay visible.
- Daily quota exhausted → global cooldown and bounded retries → DEAD_LETTERED with
  `error.code = RETRIES_EXHAUSTED`, `last_error_code = GEMINI_QUOTA_EXHAUSTED` and the absolute
  `resets_at` (approved D4; code naming per P3-D3, ADR-11).
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

## ADR-10 Error classification (Phase 2, Part 2)

- **Principle:** **FAILED** = we know the operation cannot succeed (unavailable video, bad credentials,
  invalid request, misconfiguration). **DEAD_LETTERED** = a *retryable* failure exhausted its attempts.
  Retries are never used merely to reach the DLQ; the DLQ demo uses a genuinely recurring transient failure.
- **Classes** (`common/errors.py`): `FatalStageError` (budget none → FAILED; constructor unchanged since
  Phase 1), and `RetryableError` subclasses `TransientError`, `RateLimited(retry_after_sec)` (relative,
  capped at `RETRY_AFTER_MAX_SEC` = 3600), `QuotaExhausted(resets_at)` (absolute, timezone-aware, **never
  capped**) — all on the provider budget (`MAX_ATTEMPTS`) — and `RateLimiterUnavailable` on the separate
  infrastructure budget (`INFRA_MAX_ATTEMPTS`; the provider was not called).
- **Pure classifier** (`common/http_errors.py`): no network, DB, broker, Redis, worker or HTTP-library
  dependency; it never counts attempts or schedules anything (Parts 3–5 act on its result).
- **Generic:** 2xx success; 408/425/500/502/503/504 transient; 429 rate-limited; other 4xx fatal;
  1xx/3xx/other fatal.
- **Notion** (verified against the official status-codes and request-limits pages):
  429 rate-limited (`Retry-After` integer seconds, else `additional_data.retry_after`); 429 with
  `public_api_request_blocked` fatal ("retrying won't help"); **529 `service_overload` rate-limited**
  ("retry it the same way as a 429"); 500/502/503/504 transient — the docs allow retrying these for
  non-idempotent requests only with the application's own idempotency protection and warn a 503 write may
  already be saved. `notion_write_outcome_unknown` uses a **conservative allow-list**: it is `false` only
  for GET / HEAD / OPTIONS / DELETE (case-insensitive) and `true` for every other method, **including a
  missing or unknown method**; a non-empty `retry_guidance` forces `true` regardless of method.
  `committed_resource_id` is kept only if it is a valid UUID (32 hex, dashes optional); guidance text and
  other upstream fields are never copied. The flag **records uncertainty — it does not make a retry safe**.
  **Phase 5 requirement:** when it is `true`, the publishing stage must reconcile (read the current Notion
  state via the marker/resume protocol) before attempting another write.
  409 `conflict_error` transient (D6; docs: "Make sure the parameters are up to date and try again" — a
  retry re-runs the stage, rebuilding the request from current state). 401 fatal `NOTION_UNAUTHORIZED`;
  403 fatal; 404 fatal; all 400 codes (`validation_error`, `invalid_json`, `invalid_request`,
  `invalid_request_url`, `missing_version`, `invalid_grant`, `invalid_beta`) fatal.
- **Gemini:** 429 with `quota_exceeded` or a `QuotaFailure` "PerDay" quota → `QuotaExhausted` resetting
  at the next midnight America/Los_Angeles (DST-correct); other 429 → rate-limited (`Retry-After`, else
  `RetryInfo.retryDelay`); 500/503/504 transient; 400/401/403/402/404 fatal. Both documented body shapes
  are parsed. (Real free-tier responses to be confirmed in Phase 3.)
- **YouTube (HTTP level):** 429 rate-limited; 5xx transient; **403 transient** (D2); **404/410 fatal
  `YOUTUBE_VIDEO_UNAVAILABLE`** (D3). Library-exception mapping follows the same rule in Phase 3.
- **Exceptions** (checked in this order, on the exception and up to 5 wrapped causes/contexts):
  `SSLEOFError`/`SSLZeroReturnError` transient (D4a, connection dropped) → certificate verification /
  `CertificateError` fatal → other `SSLError` fatal (D4) → timeout / connection / DNS / `OSError`
  transient → anything else transient `UNEXPECTED_ERROR` (D1, bounded).
- **Safety:** errors hold only a stable code, a fixed message, the provider and whitelisted scalar details;
  upstream bodies, URLs and exception text never appear in messages, `detail`, `str`, `repr` or logs.
  `raise_classified()` raises `from None`. Log redaction additionally masks Google API keys and `key=`
  query parameters.

## ADR-11 Classified retries via the transactional outbox (Phase 2, Part 3)

- **Failure handling in the worker:** a stage exception is classified (ADR-10). Fatal → `FAILED` (one
  execution, no retry, no DLQ copy). Retryable → ONE PostgreSQL transaction (compare-and-set on status and
  on the counter) that increments the counter and either
  * **schedules a retry:** sets `jobs.retry`, writes a `retry_scheduled` event and an outbox row
    (`job.retry`, destination `jobs.retry`/`<N>s`, NOTIFY), or
  * **dead-letters:** `DEAD_LETTERED` with `error = {code: RETRIES_EXHAUSTED, message, last_error_code,
    attempts[, resets_at]}`, a `dead_lettered` event and an outbox row for the DLQ copy (`jobs.dlx`).
  Only after the commit is the message acked; on dead-letter the credential mailbox is deleted. If the
  transaction fails, nothing is counted and nothing is published.
- **Budgets (P3-D6):** `MAX_ATTEMPTS` = total executions incl. the first (5 ⇒ at most 4 retries) on
  `attempts`; `RateLimiterUnavailable` uses `infra_attempts` / `INFRA_MAX_ATTEMPTS` only.
- **Delays:** `BACKOFF_SCHEDULE_SEC[min(n−1, last)]` (last step repeats); `RateLimited` →
  `max(step, retry_after_sec)`; `QuotaExhausted` → the normal step (P3-D2: no waiting until the reset;
  `resets_at` is preserved in the retry state and in the terminal error). The stored `next_attempt_at`
  is the actually selected delay. Delays longer than a bucket are reached by **uncounted hops** through
  the delay queues (largest bucket ≤ remaining, else the smallest); a stage never runs before
  `next_attempt_at` (P3-D1).
- **Message identity = authorisation:** each scheduled retry gets a NEW `event_id`, stored in
  `jobs.retry.event_id` and used as the outbox `event_id` → AMQP `message_id`. Hops, lock deferrals and
  infrastructure delays republish with the SAME `message_id`. While `jobs.retry` is set, only the message
  with that id may run the next attempt; any other (e.g. the original redelivered after a crash, an old
  retry copy) is stale → acked and dropped, nothing counted. `jobs.retry` is cleared by the next state
  transition. Duplicate copies of the same retry are serialised by the advisory lock (deferral) and
  dropped once the state has moved on.
- **Own infrastructure unavailable (P3-D4):** if the job state cannot be read or recorded (PostgreSQL
  unavailable, lock not obtainable, retry transaction failing) nothing is counted: a confirmed copy goes
  through the lock delay queue and only then is the original acked. If that publish also fails the
  message is not acked; after a throttle (`LOCK_RETRY_DELAY_SEC`) it is returned to the broker (no hot
  loop); a lost connection makes the broker redeliver it, which counts toward `x-delivery-limit`.
  Measured on RabbitMQ 4.4.1 (Part 4 probe): only **`nack(requeue=True)`** is uncounted;
  `reject(requeue=True)` **is** counted and must never be used for temporary conditions.
- **API:** `GET /jobs/{id}` shows `retry = {reason, attempt, max_attempts, next_attempt_at}` while a job
  waits (status stays at the current stage); internal fields (`event_id`, `budget`, …) are never exposed.
- **Logging:** stage failures are logged with type/code only — upstream exception text is not logged.

## ADR-12 Broker dead-letter auditing (Phase 2, Part 4)

- **Problem:** RabbitMQ dead-letters a job message by itself after repeated consumer crashes
  (`delivery_limit`, `QUEUE_DELIVERY_LIMIT`) or when a worker rejects a malformed message (`rejected`).
  The copy lands in `jobs.dlq`, but PostgreSQL is not told: the job would poll as "running" forever.
- **Topology:** `jobs.dlx` (fanout) → `jobs.dlq` (inspection only, never consumed) **and**
  `jobs.dlq.audit` (quorum; consumed by the auditor that runs inside every worker process, own channel,
  prefetch 1). `jobs.dlq.audit` has `x-delivery-limit = AUDIT_QUEUE_DELIVERY_LIMIT` and dead-letters
  **at-least-once** (`x-dead-letter-strategy=at-least-once` + `x-overflow=reject-publish`; without
  reject-publish RabbitMQ silently falls back to at-most-once — verified) via the default exchange to
  `jobs.dlq.audit.parked` (quorum, `x-delivery-limit=-1`, no TTL, no length limit, no DLX, not bound).
  Requires the `stream_queue` feature flag (enabled on 4.4.1).
- **Carried-over delivery count (verified):** dead-lettering keeps `x-delivery-count`; a crash-looped
  message reaches the audit queue at `QUEUE_DELIVERY_LIMIT + 1`. Therefore `AUDIT_QUEUE_DELIVERY_LIMIT`
  must be greater than `QUEUE_DELIVERY_LIMIT + 1` (validated at start-up); unset it defaults to
  `QUEUE_DELIVERY_LIMIT + 11` (10 survivable auditor crashes).
- **Origin marker:** the outbox publisher stamps every message with `x-event-type = <outbox event
  type>` (headers survive dead-lettering). A worker-initiated DLQ copy (Part 3) has no `x-death`,
  `x-event-type = job.dead_lettered` **and** body `event_type = job.dead_lettered` → acked, nothing
  recorded. Any disagreement → `inconsistent`.
- **Death parsing:** the newest `x-death` entry (`x-death[0]`, newest first — verified) must come from
  `jobs.work.v2` or legacy `jobs.work`, with reason `delivery_limit` or `rejected`, and must equal
  `x-last-death-queue/-reason`. `x-first-death-*` is never used (it is `expired` for a message that
  went through a delay queue). Otherwise → `inconsistent`, the job is not changed.
- **Job id:** body `job_id` and header `x-job-id`, each only if a valid UUID; both valid and different →
  `inconsistent`; one valid → used; none → `unreadable`.
- **Decision (one transaction, job row locked, under the job's advisory lock):** unknown job →
  `unknown_job`; terminal → `already_terminal` (a completed job is never overwritten); `jobs.retry` set
  and `message_id ≠ retry.event_id` → `stale` (the newer retry is untouched); missing `message_id` →
  `inconsistent` (never treated as stale or current; note: aio-pika/aiormq always set one); otherwise
  CAS transition → DEAD_LETTERED with `error = {code: DEAD_LETTERED_BY_BROKER, message, reason}` and
  event `dead_lettered_by_broker`. The audit row is upserted in the same transaction.
- **Audit table (`dead_letter_audit`, migrations 003/004):** `dedup_key = <message_id>:<reason>` or
  `sha256:<body hash>:<reason>`; a repeat only increments `seen_count` (and notes `last_outcome`). No
  bodies stored. Outcomes: applied, already_terminal, unknown_job, unreadable, stale, inconsistent.
- **Ack and recovery:** ack only after the commit (after deleting the credential mailbox for
  applied/already_terminal). Crash after the commit → redelivered → `already_terminal`, `seen_count+1`,
  no second event (the CAS cannot succeed twice). Busy advisory lock or PostgreSQL error → pause
  `LOCK_RETRY_DELAY_SEC`, then `nack(requeue=True)` (uncounted). Auditor crashes are counted; at the
  limit the message is **parked**, never dropped.
- **Parked messages** are for manual inspection (management UI, get + nack). Replay is out of scope
  (Q5); if ever approved: confirmed copy without death headers, same `message_id`, to `jobs.dlq.audit`,
  then ack the parked original (idempotent via the dedup key and the CAS).

## ADR-13 Shared external-API rate limiter (Phase A, Part 2)

- **Where:** `backend/notesagent/common/ratelimit.py` (`RateLimiter`), one instance per worker on the
  worker's existing Redis client; stages receive it as `StageContext.limiter`. Every external call must
  first `await limiter.acquire(provider, tokens=...)`. No new service or dependency.
- **Atomic and shared:** each check is ONE Redis Lua script using Redis' own clock (`TIME`), so all
  workers share one time source and concurrent workers cannot take the same slot.
- **Gemini** (limits are per Google project; values used AS GIVEN = user's AI Studio limits × 0.8):
  `GEMINI_RPM_LIMIT` (4) and `GEMINI_TPM_LIMIT` (200,000 input tokens) as sliding 60-second windows;
  `GEMINI_RPD_LIMIT` (16) per **Pacific calendar day** (key `ratelimit:gemini:rpd:<YYYY-MM-DD>`, resets
  at midnight Pacific, DST-correct). A request is admitted only if all three allow it; the RPD count is
  taken when the permit is granted. `settle()` replaces the token estimate with the real count. A
  request larger than the TPM limit is `LLM_REQUEST_TOO_LARGE` (fatal). Replaces the earlier plan of a
  separate `GEMINI_LIMIT_SAFETY_FACTOR` (it would apply the margin twice).
- **Notion:** `NOTION_RPS_LIMIT` (2.5/s) per connection as strict spacing (one call every 400 ms);
  callers pass a connection key (a hash, never a token). Notion documents 180 requests/minute (avg 3/s)
  per connection plus an unpublished per-workspace limit.
- **YouTube:** no documented quota for the chosen libraries, so **no numeric limit** — only the shared
  cooldown.
- **Shared cooldown:** when a stage fails with a provider `RateLimited` (429) or `QuotaExhausted`, the
  worker's existing failure path calls `limiter.report(error)` before scheduling the retry: Redis key
  `ratelimit:cooldown:<provider>` = "not before T" for every worker, where T = now + `Retry-After` (already
  capped at 3600 s by ADR-10), or `RATE_LIMIT_DEFAULT_COOLDOWN_SEC` (10) if none was sent, or the quota
  reset time. A cooldown is never shortened. The limiter's own errors are not reported back.
- **When a limit is reached:** `acquire` sleeps if the wait fits `RATE_LIMIT_MAX_WAIT_SEC` (60);
  otherwise it raises `RateLimited` (`<P>_LOCAL_RATE_LIMIT` / `<P>_COOLDOWN_ACTIVE`, with the wait as
  retry-after) or `QuotaExhausted` (`GEMINI_DAILY_LIMIT_REACHED`, resets at midnight Pacific), and the
  existing outbox retry system (ADR-11) re-queues the job. No second retry system.
- **Redis unavailable:** `acquire` raises `RateLimiterUnavailable` (infrastructure budget,
  `INFRA_MAX_ATTEMPTS`); the provider is never called without a permit. `report`/`settle` failures are
  logged only.
- **Configuration:** `GEMINI_RPM_LIMIT`, `GEMINI_TPM_LIMIT`, `GEMINI_RPD_LIMIT`, `NOTION_RPS_LIMIT`,
  `RATE_LIMIT_MAX_WAIT_SEC`, `RATE_LIMIT_DEFAULT_COOLDOWN_SEC` (settings + worker service in Compose with
  defaults that also apply to empty `.env` values).

## ADR-14 TRANSCRIBING stage: yt-dlp metadata + youtube-transcript-api (Phase B, Part 1)

- **Method:** `yt-dlp` (`extract_info(download=False, process=False)`, metadata only) for the title and
  the **true duration**; `youtube-transcript-api` (`list` → choose → `fetch`) for the timestamped
  transcript. No API key. The official YouTube Data API is not usable (`captions.download` needs OAuth
  and edit rights on the video). Pinned: `yt-dlp==2026.10.26`, `youtube-transcript-api==1.2.5`
  (+ `requests`, `defusedxml`, `urllib3`, `charset-normalizer`, `certifi`). Both libraries are
  unofficial interfaces and may break or be blocked; versions are pinned and easy to bump.
- **Constraint A:** after the metadata request and **before any transcript request**, a duration above
  `MAX_VIDEO_DURATION_SEC` (1200) → FAILED `VIDEO_TOO_LONG`; exactly 1200 is accepted (the raw,
  unrounded duration is compared). A missing, zero, negative, non-finite or non-numeric duration (e.g.
  live streams) → FAILED `VIDEO_DURATION_UNKNOWN` — never treated as 0.
- **Transcript choice:** `TRANSCRIPT_LANGUAGES` (default `en`) in priority order, manual before
  auto-generated; otherwise the first available transcript (manual first). Segments keep their order
  and are stored as `{start, duration, end, text}` (seconds, 3 decimals; text stripped; empty segments
  dropped). An empty result → FAILED `YOUTUBE_TRANSCRIPT_UNAVAILABLE`.
- **Output:** the stage checkpoint `{video_id, title, duration_sec, duration_exact, transcript:
  {source, language_code, language, is_generated, segment_count, segments}}` plus `video_title` /
  `duration_sec` on the job row, written by the worker together with the TRANSCRIBING → SUMMARIZING
  transition in ONE transaction, then acked (ADR-05). The stage has no side effects; a redelivery before
  that commit repeats it, after it the job resumes at SUMMARIZING without repeating it.
- **Rate limiting:** a `Provider.YOUTUBE` permit from the shared limiter (ADR-13) before each of the
  three requests (metadata, transcript list, transcript fetch); no numeric quota. The blocking library
  calls run in a thread with a per-request timeout (`YOUTUBE_REQUEST_TIMEOUT_SEC`, 30 s).
- **Classification (ADR-10 codes, no upstream text kept):**
  - FAILED: `YOUTUBE_VIDEO_UNAVAILABLE` (private, deleted, removed, terminated account, members-only,
    geo-blocked, upcoming live, HTTP 404/410, `VideoUnavailable`/`InvalidVideoId`),
    `YOUTUBE_AGE_RESTRICTED`, `YOUTUBE_VIDEO_UNPLAYABLE`, `YOUTUBE_TRANSCRIPT_UNAVAILABLE`
    (`TranscriptsDisabled`/`NoTranscriptFound`/empty), `VIDEO_TOO_LONG`, `VIDEO_DURATION_UNKNOWN`.
  - Retry via the existing outbox system + shared cooldown: HTTP 429 → `YOUTUBE_RATE_LIMITED`
    (Retry-After honoured); bot check / `RequestBlocked` / `IpBlocked` → `YOUTUBE_BLOCKED` (RateLimited,
    default cooldown); 5xx, 403, network/transport errors → TransientError; other library failures
    (PoToken, unparsable data) → `YOUTUBE_TRANSCRIPT_TEMPORARY`. Bounded by `MAX_ATTEMPTS`, then
    DEAD_LETTERED.
  - yt-dlp exposes availability reasons only as messages: fixed patterns are matched, the text is never
    stored or logged.
- **Decision D-1 (compliance audit) settled:** permanently unavailable / private / deleted videos end
  **FAILED** after one execution (not retried, not dead-lettered). The DLQ is demonstrated with retryable
  failures instead (e.g. persistent network/5xx errors, demo profile `MAX_ATTEMPTS=3`).
- **Known risk:** yt-dlp warns that YouTube extraction without a JavaScript runtime (deno) is
  deprecated. Metadata works without it (verified 2026-11-20); frame extraction (later) may need one.

## ADR-15 SUMMARIZING stage: Gemini study notes with grounded timestamps (Phase B, Part 2)

- **Model and SDK:** the official `google-genai` SDK (pinned `3.6.1`, plus its dependencies
  `google-auth`, `httpx2`, `httpcore2`, `tenacity`, `websockets`, `truststore`, `distro`, `sniffio`,
  `pyasn1`, `pyasn1_modules`). Model `GEMINI_MODEL` = `gemini-3.8-flash` (ADR-06, free tier, no paid
  fallback). `GEMINI_THINKING_LEVEL=low`, JSON output constrained by `response_json_schema`,
  `max_output_tokens` 8192, request timeout 120 s. The SDK's own retries are disabled
  (`HttpRetryOptions(attempts=1)`); retries happen only through ADR-11. The key comes from
  `GEMINI_API_KEY` (`SecretStr`, passed to the worker by Compose; never logged or put in errors).
- **Input and grounding:** the TRANSCRIBING checkpoint's segments are rendered as
  `[#<global segment index> <mm:ss>] text`. Gemini returns segment **numbers** (`start_segment`,
  `segment`), never free-form times. Each number is validated against the chunk it was given; the
  stored timestamp is that segment's real `start` (seconds in the original video). Invalid or invented
  numbers are dropped and counted (`validation.dropped_*`). Captions describe what the speaker presents
  ("The speaker shows …") and why it matters; the checkpoint states that frames are not yet
  extracted or visually checked.
- **Output (checkpoint, validated with pydantic):** `summary`, `key_points` (≤ 12), `sections`
  (`title`, `start_sec`, `points`, chronological), `important_timestamps` (`timestamp_sec`,
  `segment_index`, `caption`, `reason`, `transcript_excerpt`; chronological, de-duplicated when closer
  than 10 s, at most `MAX_FRAMES` (8) spread evenly over the video), `model`, `chunk_count`,
  `gemini_requests`, `usage`, `validation`, `grounding`. Malformed/empty/schema-violating output →
  `GEMINI_INVALID_OUTPUT` (TransientError, bounded retries → DEAD_LETTERED); a safety block →
  FAILED `GEMINI_OUTPUT_BLOCKED`.
- **Chunking:** lines are packed in order into chunks of at most `TRANSCRIPT_CHUNK_TOKENS` (12,000)
  estimated tokens (≈ 3 characters per token, conservative), whole segments, no overlap. One chunk →
  one request (a ~20-minute talk normally fits). Several chunks → one request per chunk plus one small
  merge request (overall summary + de-duplicated key points from the chunk summaries, no transcript);
  sections and timestamps are merged deterministically in chunk = video order. The effective chunk
  budget is `min(TRANSCRIPT_CHUNK_TOKENS, GEMINI_TPM_LIMIT − 2,000)`, so every request fits what the
  limiter can grant within one minute.
- **Rate limiting:** a `Provider.GEMINI` permit (estimated input tokens) before EVERY request and
  `settle()` with the real `prompt_token_count` afterwards (ADR-13).
- **Errors (ADR-10 classification of the SDK's `APIError` body/headers):** 429 → `GEMINI_RATE_LIMITED`
  (RetryInfo / Retry-After honoured, shared cooldown); daily quota → `GEMINI_QUOTA_EXHAUSTED` (resets at
  midnight Pacific, cooldown until then); 500/503/504 and `httpx2` transport errors → transient;
  400/401/403/402/404 → FAILED; missing key → FAILED `GEMINI_NOT_CONFIGURED`.
- **Idempotency:** the result is stored as the stage checkpoint together with the transition in one
  transaction before the ack (ADR-05); a redelivery after that resumes at EXTRACTING_FRAMES without
  calling Gemini. Partial chunk results are **not** persisted: a crash or retry during SUMMARIZING
  repeats all of its Gemini requests (each costs RPD; acceptable because a ≤ 20-minute video normally
  needs one request).

## ADR-16 EXTRACTING_FRAMES stage: real frames with yt-dlp + FFmpeg (Phase B, Part 3)

- **Source:** ONE `yt-dlp` download per job (one `Provider.YOUTUBE` limiter permit) of a **video-only**
  stream, preferring H.264: `bv*[height<=FRAME_SOURCE_MAX_HEIGHT][vcodec^=avc1]/…mp4/…/b[height<=…]`
  (default 720p), into a private temporary directory. `retries`/`fragment_retries` = 0 (no hidden retry
  loop; ADR-11 retries), `max_filesize` guard (`FRAME_DOWNLOAD_MAX_MB`, 1024). The stream URL is never
  logged or stored. The 20-minute limit is already enforced by TRANSCRIBING (ADR-14).
- **No JavaScript runtime:** verified on 2026-11-20 that `yt-dlp 2026.10.26` lists formats (24, 12
  video-only) and downloads a stream without one; it still prints its deprecation warning. If YouTube
  stops serving formats without a JS runtime, the download fails with "format not available" and the
  stage degrades to text-only (below) — installing `deno` would then be the fix (not installed now:
  not needed, ~100 MB).
- **FFmpeg:** Debian `ffmpeg` 7.1.5 installed in the image's base stage (worker and test images;
  `--no-install-recommends`, ≈ 430 MB). For each timestamp: `ffmpeg -ss T -i source -frames:v 1
  -vf scale='min(FRAME_MAX_WIDTH,iw)':-2 -q:v FRAME_JPEG_QUALITY` (accurate input seek, never
  upscaled; defaults 1280 px, q 3), then `ffprobe` must confirm a decodable MJPEG image, non-empty,
  JPEG magic bytes, ≤ 5 MiB (Notion free-workspace upload limit). Timestamps must be finite and in
  `[0, duration)` where duration is measured with `ffprobe` on the downloaded file.
- **Real frames only:** the only image bytes ever stored are those FFmpeg decoded from the downloaded
  source at that timestamp. Nothing is generated, upscaled or substituted; a failed timestamp is simply
  omitted. Each frame keeps `timestamp_sec`, `segment_index`, the Gemini `caption`/`reason`,
  `transcript_excerpt`, and `caption_basis = "transcript"` (the caption was written from the speech,
  not from looking at the image), plus `image {mime_type, width, height, bytes, sha256, data_base64}`.
- **Checkpoint:** `{requested, extracted, caption_basis, frames[], failed[{timestamp_sec, reason}],
  source{format_id, ext, width, height, vcodec, duration_sec}}`, stored (base64 JPEG in jsonb) with the
  transition in one transaction before the ack (ADR-05). The summarization contract is unchanged.
- **Graceful degradation (spec §6):** each failed timestamp → job warning
  `{"code": "FRAME_EXTRACTION_FAILED", "timestamp_sec": t, "reason": …}` (reasons:
  `TIMESTAMP_OUT_OF_RANGE`, `FFMPEG_ERROR`, `FFMPEG_TIMEOUT`, `FFMPEG_MISSING`, `NO_OUTPUT`,
  `EMPTY_OUTPUT`, `NOT_A_JPEG`, `UNREADABLE`, `TOO_LARGE`, `SOURCE_UNAVAILABLE`, `SOURCE_MISSING`); the
  other frames continue. A non-retryable source problem (format not available, unexpected download
  failure, file missing/too large) makes every timestamp such a warning and the stage still succeeds
  (text-only). The runner already turns any warning into COMPLETED_WITH_WARNINGS at PUBLISHING.
- **Errors raised (existing retries, ADR-10/14):** 429 / bot check → RateLimited (shared cooldown);
  network, 403, 5xx → TransientError; video now unavailable/private/404/410/age-restricted → FAILED
  (as in ADR-14). No timestamps → no YouTube request at all.
- **Temporary files:** `FRAME_WORK_DIR/<pid>-<job_id>-*` is deleted in `finally` (success, error,
  cancellation). At each stage start, directories whose owner PID is not alive (or a zombie), or that
  are older than `FRAME_TMP_MAX_AGE_SEC` (6 h), are purged — leftovers of SIGKILLed workers.

## ADR-17 PUBLISHING stage: one Notion page per job, reconciled before every write (Phase B, Part 4)

- **Client:** `worker/notion.py` (`httpx==0.28.1` + `httpcore==1.0.9`, now runtime pins; they were
  already pinned for tests). It sends `Authorization: Bearer <per-job token>` (from the encrypted
  credential mailbox, ADR-03) and `Notion-Version: NOTION_VERSION` (2026-03-11), to
  `NOTION_API_BASE_URL` (default `https://api.notion.com/v1`). It makes no retries of its own, and it
  takes a `Provider.NOTION` limiter permit before EVERY request (ADR-13; connection key = SHA-256 of the
  token, never the token). Errors go through `classify_http(Provider.NOTION, …, method=…)`:
  429/529 → RateLimited (Retry-After); 5xx → transient with `notion_write_outcome_unknown` for writes;
  401/403/404/400 → FAILED. Transport errors → `NOTION_UNREACHABLE` (transient; writes marked
  uncertain). The token never appears in logs, errors or `repr`.
- **Destination:** the job's `notion_parent_id`, else `NOTION_DEFAULT_PARENT_ID` (validated, normalised
  UUID; empty = unset). Neither → FAILED `NOTION_PARENT_MISSING` before any request or token read.
  Parents are pages (`{"page_id": …}`); a database ID is not supported.
- **Page:**
  - title = the video title (or "YouTube video <id>");
  - a deterministic block plan:
    - marker paragraph "Notes generated by Video Notes Agent · job <job_id> · source: <url>" (first block);
    - Summary;
    - Key points (bullets);
    - Sections (heading_3 with a link to the time in the video + bullets);
    - Key frames (only if frames were selected): a note that captions are transcript-based, one image
      block per extracted frame (Notion File Upload API, single part: create → send multipart → attach
      by `file_upload` id), each captioned "mm:ss (linked) — caption. Why it was chosen: reason", and a
      note if selected frames were missing.
  - Rich text is split into ≤ 2000-character items.
  - Blocks are written in batches of `NOTION_BLOCKS_PER_REQUEST` (≤ 100): page creation with the first
    batch, then appends.
  - A job with no frames produces a text-only page. Every image has its LLM caption (constraint B).
- **No duplicate pages (spec §6), reconciliation before every write:** each attempt first lists the
  parent's child pages. A page with the same title whose FIRST block contains `job <job_id>` is this
  job's page; its existing top-level blocks are counted, and only the remaining part of the plan is
  written (appends follow plan order, so what exists is a prefix). This covers:
  - a redelivery after a crash, before or after all writes;
  - retries after 429;
  - writes whose outcome is unknown (5xx after commit, lost connection).

  Frames are uploaded only for image blocks still missing, immediately before attaching (unattached
  uploads expire after 1 h; orphaned uploads from crashed attempts are harmless and cannot be
  deleted). Pages of other jobs, including the same video, are never reused.
- **Result:** `{notion_page_url (from Notion), frames_requested (EXTRACTING_FRAMES), frames_published
  (image blocks actually on the page)}`. The runner validates it, and sets COMPLETED, or
  COMPLETED_WITH_WARNINGS when there are warnings or `frames_published < frames_requested`, in one
  transaction with the checkpoint `{notion_page_id, …result, blocks, resumed}`, before the ack.
- **Upload rejected permanently** (4xx) → that frame is replaced by a short note and reported as warning
  `FRAME_UPLOAD_FAILED`. A transient upload failure → retry of the stage (reconciliation prevents
  duplicates).
- **Known limits:**
  - reconciliation needs one listing pass over the parent's children (paginated) per attempt;
  - if someone edits the page between a crash and the retry, the prefix assumption can be violated
    (more blocks than planned → FAILED `NOTION_PAGE_CONFLICT`);
  - a database as parent is not supported;
  - not yet verified against the real Notion API (no OAuth token yet).
