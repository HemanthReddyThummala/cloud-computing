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
  "notion_token": "<token from the extension's OAuth flow>",
  "notion_parent_id": "optional Notion page id (32 hex, dashes optional)" }
```

**202 Accepted**, header `Location: /jobs/{job_id}`:

```json
{ "job_id": "uuid", "status": "QUEUED", "video_id": "dQw4w9WgXcQ" }
```

Replaying the same `Idempotency-Key` with the same body (within `IDEMPOTENCY_KEY_TTL_HOURS`) returns the
**original** 202 body and `Location`, plus header `Idempotent-Replayed: true`. No new job is created.

| Status | `error.code` | When |
|---|---|---|
| 400 | `INVALID_JSON` | body is not JSON |
| 400 | `INVALID_REQUEST` | not an object, missing/extra/wrong-typed field |
| 400 | `INVALID_YOUTUBE_URL` | not a YouTube video URL with an 11-char id |
| 400 | `INVALID_NOTION_TOKEN` | empty or contains whitespace |
| 400 | `INVALID_NOTION_PARENT_ID` | not a Notion page id |
| 400 | `INVALID_IDEMPOTENCY_KEY` | malformed header |
| 413 | `PAYLOAD_TOO_LARGE` | body > 16 KiB |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | Content-Type is not `application/json` |
| 422 | `IDEMPOTENCY_KEY_REUSED` | key already used with a different body |
| 409 | `IDEMPOTENCY_CONFLICT` | rare race; retry |
| 503 | `BROKER_UNAVAILABLE` / `DATABASE_UNAVAILABLE` | dependency down; nothing was created |

The gateway does no pipeline work. The token is placed, encrypted, in a per-job RabbitMQ credential
mailbox; it is never stored in PostgreSQL (ADR-03).

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
Additive fields: `video_id, retry` (Phase 2: populated while waiting for a backoff retry), `created_at, updated_at`.
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

**DEAD_LETTERED → 200** (same shape as FAILED; produced from Phase 2 onwards)

```json
{ "status": "DEAD_LETTERED", "result": null, "warnings": [],
  "error": { "code": "RETRIES_EXHAUSTED", "message": "..." } }
```

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
