# API Gateway — REST contract

Base URL (local): `http://localhost:8000`. All bodies are JSON. Every error uses the same shape:

```json
{ "error": { "code": "MACHINE_READABLE_CODE", "message": "Human-readable explanation." } }
```

Error messages are fixed strings: they never echo request values (tokens!) or upstream error text.

---

## POST /jobs — submit a job

Request headers: `Content-Type: application/json` (required), `Idempotency-Key` (optional, 8–128 chars of
`A–Z a–z 0–9 - _ : .`).

```json
{ "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "notion_token": "<sealed token from POST /oauth/notion/callback (nva1.…), or a raw Notion token>",
  "notion_parent_id": "optional Notion page id (32 hex, dashes optional)" }
```

**202 Accepted**, header `Location: /jobs/{job_id}`:

```json
{ "job_id": "uuid", "status": "QUEUED", "video_id": "dQw4w9WgXcQ" }
```

Replaying the same `Idempotency-Key` with the same body (within `IDEMPOTENCY_KEY_TTL_HOURS`) returns the
**original** 202 body and `Location`, plus header `Idempotent-Replayed: true`. No new job is created.

The extension (ADR-20) always sends `Idempotency-Key: vna:<videoId>:<uuid>` and reuses it on the next
click when the previous attempt's outcome was unknown (network error, timeout). It then follows the job
with `GET /jobs/{job_id}` alone, which already contains `result`, `warnings`, `error` and `retry`.

| Status | `error.code` | When |
|---|---|---|
| 400 | `INVALID_JSON` | body is not JSON |
| 400 | `INVALID_REQUEST` | not an object, missing/extra/wrong-typed field |
| 400 | `INVALID_YOUTUBE_URL` | not a YouTube video URL with an 11-char id |
| 400 | `INVALID_NOTION_TOKEN` | empty, contains whitespace, or a sealed token (`nva1.`) that fails to unseal |
| 400 | `INVALID_NOTION_PARENT_ID` | not a Notion page id |
| 400 | `INVALID_IDEMPOTENCY_KEY` | malformed header |
| 413 | `PAYLOAD_TOO_LARGE` | body > 16 KiB |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | Content-Type is not `application/json` |
| 422 | `IDEMPOTENCY_KEY_REUSED` | key already used with a different body |
| 409 | `IDEMPOTENCY_CONFLICT` | rare race; retry |
| 503 | `BROKER_UNAVAILABLE` / `DATABASE_UNAVAILABLE` | dependency down; nothing was created |

The gateway does no pipeline work. The token is placed, encrypted, in a per-job RabbitMQ credential
mailbox; it is never stored in PostgreSQL (ADR-03). A sealed token is unsealed first, so only the real
Notion token enters the mailbox (ADR-18).

---

## POST /oauth/notion/authorize — start Notion OAuth (ADR-18)

No body. **200** (header `Cache-Control: no-store`):

```json
{ "authorization_url": "https://api.notion.com/v1/oauth/authorize?client_id=…&response_type=code&owner=user&redirect_uri=…&state=…",
  "state": "single-use random value", "redirect_uri": "https://<extension-id>.chromiumapp.org/notion",
  "expires_in": 600 }
```

Caller: the extension's background service worker (ADR-19), which has a host permission for the
gateway, so no CORS is needed. The extension opens `authorization_url` with `chrome.identity.launchWebAuthFlow`. Notion redirects to
`redirect_uri` (intercepted by Chrome) with `code` + `state`, or `error` + `state`.

## POST /oauth/notion/callback — finish Notion OAuth

The extension forwards what Notion returned:

```json
{ "state": "…", "code": "…" }        or        { "state": "…", "error": "access_denied" }
```

The state is consumed on every call (single use). The gateway exchanges the code with Notion
server-side (client secret never leaves the gateway). **200** (`Cache-Control: no-store`):

```json
{ "notion_token": "nva1.…", "token_format": "sealed-v1",
  "workspace_id": "…", "workspace_name": "…", "bot_id": "…" }
```

`notion_token` is **sealed** (encrypted by the gateway); the extension stores it as-is and sends it in
`POST /jobs`. The raw access token, refresh token and code are never returned or logged.

| Status | `error.code` | When |
|---|---|---|
| 400 | `OAUTH_INVALID_REQUEST` | body not JSON / missing `state` / wrong types |
| 400 | `OAUTH_STATE_INVALID` | state unknown, expired or already used |
| 400 | `OAUTH_ACCESS_DENIED` | the user cancelled (`error=access_denied`) |
| 400 | `OAUTH_AUTHORIZATION_FAILED` | Notion returned another `error` |
| 400 | `OAUTH_CODE_MISSING` | neither `code` nor `error` given |
| 400 | `OAUTH_EXCHANGE_FAILED` | Notion rejected the code (e.g. expired / reused) — restart |
| 502 | `OAUTH_CLIENT_REJECTED` | Notion rejected the client ID/secret (gateway misconfiguration) |
| 502 | `NOTION_UNAVAILABLE` / `NOTION_UNREACHABLE` | Notion 5xx / network error |
| 502 | `OAUTH_EXCHANGE_FAILED` | Notion's response had no access token |
| 503 | `NOTION_RATE_LIMITED` | Notion 429 or our limiter; `Retry-After` header |
| 503 | `OAUTH_NOT_CONFIGURED` | client ID/secret/redirect URI not set (also for `/authorize`) |
| 503 | `OAUTH_STATE_STORE_UNAVAILABLE` | Redis down (also for `/authorize`) |

---

## GET /jobs/{job_id} — poll status (read-only)

**200**:

```json
{ "job_id": "uuid", "status": "EXTRACTING_FRAMES", "progress": 55,
  "video_id": "dQw4w9WgXcQ", "video_title": null, "duration_sec": null,
  "warnings": [], "error": null, "result": null, "retry": null,
  "created_at": "2026-11-19T05:30:00+00:00", "updated_at": "2026-11-19T05:30:04+00:00" }
```

Spec fields: `job_id, status, progress, video_title, duration_sec, warnings, error, result`.
Additive fields: `video_id`, `retry`, `created_at`, `updated_at`.

`retry` is `null` unless the job is waiting for a scheduled retry. Then the status stays at the
current stage (the job is waiting, not failed) and:

```json
"retry": { "reason": "NOTION_RATE_LIMITED", "attempt": 1, "max_attempts": 5,
           "next_attempt_at": "2026-11-19T05:31:10+00:00" }
```

`attempt` = failed attempts so far; `max_attempts` = total executions allowed. Internal retry fields
(event id, budget) are never exposed.
Unknown or malformed id → **404** `JOB_NOT_FOUND`. Polling never modifies the job (tested).

---

## GET /jobs/{job_id}/results — fetch the outcome

**Not finished yet → 409** (never a null result):

```json
{ "error": { "code": "JOB_NOT_FINISHED", "message": "The job has not reached a terminal state yet; poll GET /jobs/{job_id}." },
  "status": "SUMMARIZING" }
```

**COMPLETED → 200**

```json
{ "status": "COMPLETED",
  "result": { "notion_page_url": "https://www.notion.so/...", "frames_requested": 6, "frames_published": 6 },
  "warnings": [], "error": null }
```

**COMPLETED_WITH_WARNINGS → 200** (some frames omitted, or another non-fatal issue)

```json
{ "status": "COMPLETED_WITH_WARNINGS",
  "result": { "notion_page_url": "https://www.notion.so/...", "frames_requested": 6, "frames_published": 5 },
  "warnings": [ { "code": "FRAME_EXTRACTION_FAILED", "timestamp_sec": 461 } ], "error": null }
```

**FAILED → 200**

```json
{ "status": "FAILED", "result": null, "warnings": [],
  "error": { "code": "VIDEO_TOO_LONG", "message": "..." } }
```

FAILED error codes set by the TRANSCRIBING stage (ADR-14):

- `VIDEO_TOO_LONG`: longer than `MAX_VIDEO_DURATION_SEC` (1200 s; exactly 1200 is accepted). Rejected
  before any transcript request.
- `VIDEO_DURATION_UNKNOWN`: missing or invalid duration (for example a live stream).
- `YOUTUBE_VIDEO_UNAVAILABLE`: private, deleted or otherwise unavailable, including 404/410.
- `YOUTUBE_AGE_RESTRICTED`.
- `YOUTUBE_VIDEO_UNPLAYABLE`.
- `YOUTUBE_TRANSCRIPT_UNAVAILABLE`: captions disabled, missing or empty.

These fail after one execution. YouTube 429s, blocking, 5xx and network errors are retried instead:
`GET /jobs/{id}` shows `retry` while the job waits.

While or after TRANSCRIBING, `GET /jobs/{id}` reports the real `video_title` and `duration_sec`.

**Frame warnings (EXTRACTING_FRAMES, ADR-16):** a timestamp whose frame cannot be captured never fails
the job. It is omitted and reported as a warning, and the job ends `COMPLETED_WITH_WARNINGS` (or as a
text-only page if no frame could be captured):

```json
"warnings": [ { "code": "FRAME_EXTRACTION_FAILED", "timestamp_sec": 461.0, "reason": "TIMESTAMP_OUT_OF_RANGE" } ]
```

**DEAD_LETTERED → 200** (a *retryable* failure exhausted its retry budget; same shape as FAILED)

```json
{ "status": "DEAD_LETTERED", "result": null, "warnings": [],
  "error": { "code": "RETRIES_EXHAUSTED", "message": "...", "last_error_code": "GEMINI_QUOTA_EXHAUSTED",
             "attempts": 5, "resets_at": "2026-11-20T00:00:00-08:00" } }
```

`last_error_code` is the classified cause of the final failure; `resets_at` appears only when that cause
was a daily quota (absolute time, not capped).

When the last failure was a provider HTTP response (ADR-21), two optional fields give its sanitized
status, never message text:

```json
"error": { "code": "RETRIES_EXHAUSTED", "message": "...", "attempts": 5,
           "last_error_code": "GEMINI_HTTP_503", "last_http_status": 503, "last_provider_code": "UNAVAILABLE" }
```

Gemini 503 (`UNAVAILABLE`) and 504 (`DEADLINE_EXCEEDED`) are retried with `GEMINI_UNAVAILABLE_BACKOFF_SEC`
(default 60, 180, 600 s; a valid Retry-After raises the delay) instead of `BACKOFF_SCHEDULE_SEC`. While the
job waits, `retry.reason` is `GEMINI_HTTP_503` / `GEMINI_HTTP_504`.

**DEAD_LETTERED by the broker → 200** (RabbitMQ dead-lettered the job message itself: processing it
repeatedly crashed the workers, or the message was unreadable; ADR-12)

```json
{ "status": "DEAD_LETTERED", "result": null, "warnings": [],
  "error": { "code": "DEAD_LETTERED_BY_BROKER", "message": "...", "reason": "delivery_limit" } }
```

`reason` is `delivery_limit` or `rejected`. A dead letter whose broker metadata is missing or
inconsistent never changes the job; it is only recorded for inspection.

Guarantees (enforced by the worker **and** by PostgreSQL constraints, migration 002):

* `result` is non-null **only** for `COMPLETED` / `COMPLETED_WITH_WARNINGS`. A failed or dead-lettered
  job can never return a Notion URL.
* Every `result` contains an absolute `https://` `notion_page_url` (worker additionally requires a
  `notion.so` / `notion.site` host) and integer `frames_requested` ≥ `frames_published` ≥ 0. A publishing
  stage that does not produce this fails the job with `PUBLISH_RESULT_INVALID` instead of completing it.
* `error` is non-null **only** for `FAILED` / `DEAD_LETTERED`.
* `frames_published < frames_requested` always yields `COMPLETED_WITH_WARNINGS`.

Relation to the spec (§5): the spec's example body has `status`, `result`, `warnings`; this API adds an
`error` key (always present, `null` on success) so failures are explained, and adds `status` to the 409 body.

---

## GET /healthz — dependency check

**200** when PostgreSQL, RabbitMQ and Redis are all reachable, otherwise **503**; each check reports
`ok`/`down` and only an error *type* (never a message):

```json
{ "status": "ok",
  "checks": { "gateway": {"status": "ok"},
              "postgres": {"status": "ok", "outbox_pending": 0},
              "rabbitmq": {"status": "ok", "work_queue_messages": 0, "work_queue_consumers": 2},
              "redis": {"status": "ok", "live_workers": 2} } }
```

`live_workers` counts worker heartbeats (15 s TTL, so a just-stopped worker may be counted briefly);
`work_queue_consumers` is exact.

## GET /livez

Process liveness only (`{"status": "alive"}`), used by the container healthcheck.
