# First real end-to-end job — failure diagnosis (read-only)

Date: 2026-11-21 IST (= 2026-11-20 18:3x UTC), shortly after the job failed.

**Read-only.**
- No job submitted; no call to Gemini or Notion.
- No restart, rebuild, database change, `.env` change or Git operation.
- No secrets in output: logs were filtered to non-secret fields and scanned for key-like strings
  (0 hits); the transcript text was not printed (counts only).

## 1. The job

| Field | Value |
|---|---|
| Job ID | **`acffab62-5b35-4826-bd70-1bb43e7e51db`** (the only job for `D26sUZ6DHNQ`) |
| Video | "99% of Developers Don't Get Sockets", `duration_sec` 724 (12:04) |
| Final state | **`DEAD_LETTERED`**, progress 30, `finished_at` 2026-11-20 18:30:33 UTC |
| Error | `{"code": "RETRIES_EXHAUSTED", "attempts": 5, "last_error_code": "GEMINI_HTTP_503", "message": "The job kept failing with a retryable error and its retry budget is exhausted."}` |
| Failed stage | **SUMMARIZING** (Gemini) |
| Attempts | 5 = `MAX_ATTEMPTS` (provider budget); `infra_attempts` 0 |
| Submission | extension, with an `Idempotency-Key` (`vna:D26sUZ6DHNQ:…`); 1 idempotency row; Notion parent: default (none given) |
| Transcript | **saved**: TRANSCRIBING checkpoint at 18:24:35 UTC: English auto-generated captions (`youtube-transcript-api`), **334 segments, about 12,200 characters (about 3k tokens: one request, no chunking)**, `duration_sec` 724 |
| Summary / later stages | **no** SUMMARIZING, EXTRACTING_FRAMES or PUBLISHING checkpoint; `result` null; `warnings` [] |
| Frames / Notion | **never started**: no frame or yt-dlp download activity, no `api.notion.com` request in the worker logs → **no Notion page created** |

### Timeline (job_events + worker logs, UTC)

| Time | Event | Worker | Gemini request → response | Duration |
|---|---|---|---|---|
| 18:24:32.659 | submitted → QUEUED | — | | |
| 18:24:32.698 | claimed → TRANSCRIBING | 75d6a12fe96d-1 | | |
| 18:24:35.429 | → SUMMARIZING (transcript checkpointed) | 75d6a12fe96d-1 | | |
| 18:24:43.293 | attempt 1 failed → retry in **10 s** | 75d6a12fe96d-1 | POST `…/v1/models/gemini-3.8-flash:generateContent` → **503 Service Unavailable** | about 7.8 s |
| 18:24:53.344 | retry started (attempt 2) | 13650294ceef-1 | | |
| 18:24:55.387 | attempt 2 failed → retry in **30 s** | 13650294ceef-1 | → **503** | about 2.0 s |
| 18:25:25.428 | retry started (attempt 3) | 75d6a12fe96d-1 | | |
| 18:27:24.530 | attempt 3 failed → retry in **90 s** | 75d6a12fe96d-1 | → **504 Gateway Timeout** | **about 119 s** |
| 18:28:54.568 | retry started (attempt 4) | 13650294ceef-1 | | |
| 18:28:57.651 | attempt 4 failed → retry in **90 s** | 13650294ceef-1 | → **503** | about 3.1 s |
| 18:30:27.713 | retry started (attempt 5) | 75d6a12fe96d-1 | | |
| 18:30:33.154 | attempt 5 failed → **DEAD_LETTERED** (`retry budget exhausted`) | 75d6a12fe96d-1 | → **503** | about 5.4 s |
| 18:30:33.192 | DLQ copy audited ("worker-initiated dead-letter copy; job already dead-lettered") | | | |

Outbox rows 3–8 (`job.submitted`, 4 × `job.retry` to `jobs.retry` 10s/30s/90s/90s, `job.dead_lettered`
to `jobs.dlx`) were all published on the first attempt.

## 2. Gemini errors

- **Exactly one Gemini request per attempt, 5 in total:** 503, 503, 504, 503, 503. Gemini did **not**
  return 503 every time: attempt 3 was a 504.
- **The actual error message from Gemini is not available.** By design (ADR-10/15) the classifier
  neither logs nor stores provider error bodies, only the status-derived code. It cannot be recovered
  without calling Gemini, which is not allowed here.
- **Interpretation: temporary server-side unavailability / overload of the model, not our
  configuration.**
  - 503 (`UNAVAILABLE`) and 504 (`DEADLINE_EXCEEDED`) are Google-side statuses. Quota or rate limiting
    would be **429**, authentication **401/403**, a wrong model name **404**, and a bad request or
    config **400**. None of these occurred.
  - The 504 came after about 119 s. The SDK passes our `gemini_request_timeout_sec` (120 s) to Google
    as the server deadline, so this was a **server response at its deadline** (the model did not answer
    in time), not a local client timeout. A local timeout would have been logged as a network error,
    not as an HTTP 504.
  - The input was small (about 3k tokens), so request size is an unlikely cause.
  - The same key and model (`gemini-3.8-flash`, thinking `low`, SDK retries off) answered successfully
    on 2026-11-20 (Phase B Part 2 verification), which argues against a key or model configuration
    problem. [NV for this moment, since no call was allowed.]
- **Retry behaviour was exactly as configured:**
  - `http_errors.py:273`: Gemini 500/503/504 → `TransientError` → provider budget;
  - delays **10 / 30 / 90 / 90 s** = `BACKOFF_SCHEDULE_SEC` 10,30,90 with the last step repeating;
  - **5 executions** = `MAX_ATTEMPTS` 5;
  - then DEAD_LETTERED with `RETRIES_EXHAUSTED` and a DLQ copy.
  - Both workers shared the retries; no retry was lost or duplicated.
  - The total retry window was only about **6 minutes** (18:24:35 → 18:30:33).
- **No internal application error:** the only ERROR line since the deployment is the expected "retry
  budget exhausted; job dead-lettered"; no traceback in the gateway, workers or outbox publisher.

## 3. System health (after the failure)

| Check | Result |
|---|---|
| `/healthz` | HTTP 200 ok; outbox_pending 0; work queue 0 messages / 2 consumers; 2 live workers |
| Containers | all 7 running and healthy, 0 restarts; backend on `notes-agent-backend:dev` (= `00ef0f4`, deployed 18:10 UTC); infrastructure start times unchanged |
| RabbitMQ `/` | `jobs.work.v2` 0 / 2 consumers; `jobs.dlq.audit` 0 / 2 consumers; `jobs.dlq.audit.parked` 0; retry queues 0; **`jobs.dlq` 1 ready message** = this job's dead-letter copy (expected, ADR-12; kept for inspection, no consumer); no `cred.*` mailbox left |
| Redis limiter | `ratelimit:gemini:rpd:2026-11-20` = **6** (1 earlier verification + these 5; limit 16 per Pacific day, which resets at 08:00 UTC on 2026-11-21). The TPM window has expired. No cooldown keys (none expected for 5xx). 2 worker heartbeats |
| Duplicates | 1 job for the video, 1 idempotency row, 1 "job accepted" in the gateway log since the deployment |
| Notion | no PUBLISHING checkpoint, `result` null, no `api.notion.com` request → **no page created** |
| OAuth | gateway log since the deployment: "notion authorization completed" ×1 → **the first real Notion sign-in succeeded**. The 3 "started" include 1 from the deployment check; 1 "not granted" was that check's cleanup |
| Database | 3 jobs: 2 earlier FAILED, this one DEAD_LETTERED; 0 dead_letter_audit rows (correct: worker-initiated copy) |

## 4. Minor findings (no action taken)

1. **Popup wording:** `reasonText("GEMINI_HTTP_503")` matches none of the suffix rules, so the popup
   shows the raw code ("Last cause: GEMINI_HTTP_503") instead of "the AI model had a temporary
   problem". This is cosmetic; it would be a small change in `extension/lib/popup-view.js` (`_HTTP_5xx`
   suffix).
2. **Short retry window for provider outages:** about 6 minutes in total (10 + 30 + 90 + 90 s) is short
   for a Gemini overload episode, and each failed attempt also costs one of only 16 daily requests. A
   possible later change is a longer schedule for provider 5xx (for example 30 / 120 / 300 s). It is
   **not required** to retry this video.

## 5. Recommendation

**Retry after checking Gemini availability.** Not a project or model configuration problem, and no
code change is needed for the retry.

- Check that the model is up before resubmitting (Google AI Studio / Gemini API status, or the user's
  own AI Studio playground with `gemini-3.8-flash`). This check is outside the stack and was not done
  here.
- How to retry: there is no re-run endpoint, so click **Take Notes** on the same video again. The job is
  terminal, so the extension and gateway create a **new** job (new Idempotency-Key). TRANSCRIBING runs
  again (3 YouTube requests); the old job stays DEAD_LETTERED.
- **Budget:** 10 of 16 daily Gemini requests remain until 08:00 UTC (13:30 IST) on 2026-11-21. A
  successful retry needs 1 request (single chunk). If Gemini is still overloaded, a retry can use up to
  5 more, so avoid repeated blind retries.
- Optional follow-ups (separate approval): the popup wording fix (finding 1); a longer provider-5xx
  backoff (finding 2).

## Commands used (all read-only)

- `psql` via `docker exec notes-agent-postgres-1`:
  - `information_schema.columns`;
  - `jobs`, `job_events`, `job_checkpoints` (metadata and counts only, no transcript text), `outbox`
    (payload without a token field), `idempotency_keys` (key prefix only), `dead_letter_audit`.
- `docker compose logs --since/--until … worker|gateway|outbox-publisher`, filtered to non-secret fields
  with a Python filter; key-like pattern scan: 0 hits.
- `grep` of `backend/notesagent/common/http_errors.py`, `worker/gemini.py`, `common/config.py`.
- `redis-cli SCAN/TYPE/TTL/GET` (limiter counters and heartbeats only).
- `rabbitmqctl list_queues -p /`; `curl /healthz`; `docker compose ps`; `docker inspect` (restarts and
  start times).
