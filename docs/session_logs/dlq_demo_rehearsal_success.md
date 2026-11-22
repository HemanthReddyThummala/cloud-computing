# DLQ live demonstration (runbook 2-L, ADR-22): success

Date: 2026-11-22 IST (2026-11-21 22:15–22:29 UTC). The user approved the DLQ demonstration only.

Conditions set by the user:
- `MAX_ATTEMPTS=3` only during the window;
- no other submissions;
- workers verified before arming;
- the binding checked immediately after the click;
- the job-bound key not removed by hand;
- `MAX_ATTEMPTS=5` and `DEMO_FAULTS_ENABLED=false` restored and verified.

Not done:
- no `.env`, code or configuration change;
- no Git operation;
- the root `handover.md` untouched;
- no Redis counter, queue, job or DLQ modified manually;
- no rate-limiter change.

## 1. Read-only preflight (22:15 UTC)

| Check | Result |
|---|---|
| Workers | 2 healthy, 0 restarts, `d0c52d4391f0` / `52022abad07f` on `78864607bddd` (ADR-22); `DEMO_FAULTS_ENABLED=false`, `MAX_ATTEMPTS=5` (env and loaded settings) |
| Other services | Gateway, publisher, postgres, rabbitmq and redis healthy; `/healthz` ok, `outbox_pending` 0 |
| Queues | work and retry queues 0; **`jobs.dlq` = 4** |
| Jobs | 16, all terminal |
| Redis | 0 `demo:fault:*` keys; no YouTube cooldown |

**Code checks:**
- The `dlq` hook in `transcribe.call()` runs after `limiter.acquire(YOUTUBE)`. For YouTube that is only
  a cooldown check: there is no counter.
- The hook raises in place of `youtube.video_info`.
- `classify_metadata_error` maps the injected `TransportError` to a retryable `TransientError`
  `YOUTUBE_UNREACHABLE`.
- `limiter.report()` ignores `TransientError`, so no shared cooldown is set and other jobs are not
  affected.

**Two differences from the requested description, reported before approval:**
- the production `MAX_ATTEMPTS` is 5, so 3 attempts only applies with `MAX_ATTEMPTS=3` on the enable
  command;
- `disarm` removes only an unclaimed arming; the job-bound key has a 2 h TTL.

## 2. Demo window

| UTC | Step | Result |
|---|---|---|
| — | Dry run of the enable command | only worker-1 and worker-2 recreated |
| 22:24:10 | `DEMO_FAULTS_ENABLED=true MAX_ATTEMPTS=3 docker compose up -d --no-deps --no-build --scale worker=2 worker` | exit 0; workers `e4da8c591875` / `1224b1660c9a`, both healthy, on `78864607bddd` |
| — | Verification before arming | env and loaded settings `demo_faults_enabled=True`, `max_attempts=3` on both; 2 × "demo faults ENABLED"; `/healthz` ok, 2 consumers (`live_workers` briefly 4 while the old heartbeats expired, then 2) |
| ~22:25 | `arm dlq --video favi7avxIag` | `ARMED`; `status`: `demo:fault:video:favi7avxIag = dlq (ttl 899s)` |
| 22:27:26 | The user clicked Take Notes once | job **`129af1de-6a1d-4295-a601-7e48db320d02`**; the only job since the window started |
| 22:27:28 | Binding check by the watcher | `demo:fault:job:129af1de… = dlq`; the video key consumed (0); 1 "bound" log line |
| 22:28:07 | Final state | `DEAD_LETTERED` |

## 3. Evidence

| Requirement | Observed |
|---|---|
| Final state | `DEAD_LETTERED`, `attempts` 3, `infra_attempts` 0, `error.code` `RETRIES_EXHAUSTED`, `last_error_code` **`YOUTUBE_UNREACHABLE`**, `result` NULL |
| Events | `submitted` 22:27:26.360 → `claimed` (worker-2) → `retry_scheduled` attempt 1 (`YOUTUBE_UNREACHABLE`, delay 10 s, `http_status` null) → `retry_started` (worker-1) → `retry_scheduled` attempt 2 (delay 30 s) → `retry_started` (worker-2) → `dead_lettered` 22:28:06.904, attempt 3. Retries ran on both workers |
| DLQ copy | outbox `job.dead_lettered` → `dead`, published; **`jobs.dlq` 4 → 5**; work and retry queues 0 |
| 0 YouTube calls | the injection fired before every metadata request: 3 × `demo fault injected` (`TRANSCRIBING`, attempt 0/1/2). `video_title` and `duration_sec` are NULL (they are only set from YouTube metadata); 0 checkpoints |
| 0 Gemini calls | the job never reached SUMMARIZING; 0 `generativelanguage` / `HTTP Request` log lines; Gemini counter unchanged at **16** |
| 0 Notion calls | the job never reached PUBLISHING; 0 Notion log lines |
| Logs | 1 × "demo fault bound to job", 3 × "demo fault injected", 1 × "retry budget exhausted"; 0 tracebacks |
| Other jobs | none submitted in the window (job count 16 → 17) |

## 4. Disabling and final verification (22:28–22:29 UTC)

| Step | Result |
|---|---|
| `disarm --video favi7avxIag` | "Nothing armed" (already claimed) |
| Dry run of `docker compose up -d --no-deps --no-build --scale worker=2 worker` (no variables set) | only the two workers |
| Disable (same command) | exit 0 at 22:28:39 |
| Workers | worker-1 `07df16513cda`, worker-2 `47094d34563a`; image `78864607bddd`; both **healthy**; 0 restarts; 2 × "worker started"; 0 "ENABLED"; 0 tracebacks |
| Effective config | **`DEMO_FAULTS_ENABLED=false`, `MAX_ATTEMPTS=5`** on both (env and loaded settings) |
| Switch check | `arm` → `REFUSED`, exit 2 (it refuses before touching Redis) |
| `/healthz` | ok; `live_workers` 2; consumers 2; `outbox_pending` 0 |
| Other services | gateway, publisher, postgres, rabbitmq and redis unchanged, healthy |
| Redis | only `demo:fault:job:129af1de…` (≈ 2 h TTL, left to expire as instructed; inert with the switch off) |

## 5. Result

**The DLQ demonstration (spec §6) passed live:**
- a retryable failure was retried with backoff across both workers;
- after `MAX_ATTEMPTS` the job was dead-lettered, with a copy in `jobs.dlq`;
- no external API was used;
- D-1 was untouched: no private-video path was involved, and that code is unchanged.

## Files changed (docs only)

- `docs/PROJECT_STATUS_AND_HANDOVER.md`: §6 deployment (demo window, current worker IDs); §9 remaining
  work; §10 history.
- `docs/FAILURE_DEMONSTRATION_RUNBOOK.md`: readiness row 2; 2-L rehearsal notes; §6 remaining work.
- This log.
