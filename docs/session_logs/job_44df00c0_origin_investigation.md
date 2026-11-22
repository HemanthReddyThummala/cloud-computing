# Origin of job 44df00c0 (read-only investigation)

Date: 2026-11-22 IST. The investigation was read-only:
- nothing was deleted;
- the rate limiter was not changed (one Redis `GET` only);
- no job was submitted;
- no external call was made;
- the demo switch was not enabled;
- `.env`, code, Git and the root `handover.md` were untouched.

## Sources

- `jobs`, `job_events`, `idempotency_keys`, `job_checkpoints` and `outbox` rows;
- the gateway logs for 21:26–21:34 UTC;
- Redis `GET ratelimit:gemini:rpd:2026-11-21`.

The worker logs from that period no longer exist: the workers were recreated at the ADR-22
deployment, which was 22:07 UTC.

## Findings

### 1. Origin: the extension's Take Notes flow, from the host browser

| Evidence | Value |
|---|---|
| Idempotency key | `vna:favi7avxIag:<uuid>`, 52 chars. This is the extension's Take Notes format, the same as every user-reported job |
| Gateway access log | `172.18.0.1:43732 POST /jobs 202` at 21:27:13.006 |
| Client address | `172.18.0.1` is the Docker bridge gateway, i.e. the host machine via the published port |
| Following requests | `GET /jobs/<id>` polling every ~3 s, interleaved for several jobs: the extension's progress polling |

### 2. It was one of four submissions within 7 seconds, on the same connection

| UTC | Job | Video | Duration | Outcome |
|---|---|---|---|---|
| 21:27:11.626 | `139874ed` | `NvZEZ-mZsuI`, "How Sockets Actually Work…" | 765 s | `COMPLETED` 21:27:31; Notion page |
| 21:27:13.005 | `44df00c0` | `favi7avxIag`, "What is WebSocket?…" | 932 s | `DEAD_LETTERED` 21:31:48 |
| 21:27:15.991 | `8513c17f` | `TlHvYWVUZyc`, "Kubernetes Explained in 6 Minutes" | 388 s | `COMPLETED` 21:27:34; Notion page |
| 21:27:18.455 | `5478d8b0` | `M988_fsOSWo`, "Cloud Computing In 6 Minutes" | 383 s | `COMPLETED` 21:27:59 (attempts 1: one `YOUTUBE_FORBIDDEN` 403 retry in EXTRACTING_FRAMES); Notion page |

- All four use the same keep-alive TCP connection (`172.18.0.1:43732`) and the Take Notes key format.
- Someone using the browser on the host clicked Take Notes on four videos in quick succession.
- **Who** clicked cannot be determined: jobs carry no user identity.
- Claude submitted none of them. Claude's only approved real job of that session was the crash-demo
  job `6141336d` at 21:17:49.

### 3. Path of `44df00c0`

1. Submitted and claimed by worker-2 (`44b596e75218-1`); TRANSCRIBING checkpoint at 21:27:16.
2. SUMMARIZING attempt 1, 21:27:18: a **real Gemini HTTP 503 `UNAVAILABLE`**. The ADR-21 backoff
   `gemini_unavailable` applied, with a 60 s delay.
3. Attempts 2–4 (21:28:18, 21:28:48, 21:30:18) were refused **locally** with
   `GEMINI_DAILY_LIMIT_REACHED` (`http_status` null), so no request was sent to Google.
4. Attempt 5, 21:31:48: `DEAD_LETTERED` with `RETRIES_EXHAUSTED`. The outbox published
   `job.dead_lettered`, giving the 4th message in `jobs.dlq`.

### 4. Gemini budget accounting (counter 12 → 16)

| Permit | Job | Evidence |
|---|---|---|
| 13 | `139874ed` | SUMMARIZING 21:27:15 → 21:27:18 (≈3 s: one request) |
| 14 | `44df00c0` | the 503 at 21:27:18 (a permit is taken before the request) |
| 15 | `8513c17f` | SUMMARIZING 21:27:20 → 21:27:23 |
| 16 | `5478d8b0` | SUMMARIZING 21:27:33 → 21:27:36 |

Per-job request counts are not stored in `result`, so this attribution is **inferred** from the
timings and the single-chunk durations. The total matches the counter exactly: `GET` returns 16.

When the retry of `44df00c0` came due 60 s later, the other three jobs had already used the remaining
permits. That is why it ended locally limited rather than with a second real request.

## Assessment

- **The system behaved as designed:**
  - idempotent extension submissions;
  - a real provider 503 classified and backed off;
  - the shared limiter refused requests beyond the daily budget instead of sending them;
  - retry exhaustion went to the DLQ with an outbox copy.
- **Incidental observation (not the R9 evaluation):** a 4-click burst drained with no LLM 429.
- **Not a security incident.**
  - The gateway port is published on the host. The jobs came from the host through the extension's own
    flow; there was no external client.
- **Consequence for the planned demos:**
  - The Gemini counter is 16/16, so jobs needing a real summary are refused locally.
  - The `dlq` demo makes no external call and is unaffected.

## Files changed

- `docs/PROJECT_STATUS_AND_HANDOVER.md`: §5 note on the burst; §10 history row.
- This log.
