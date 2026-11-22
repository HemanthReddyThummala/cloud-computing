# ADR-22 worker-only deployment (demo fault switch present, disabled)

Date: 2026-11-22 03:36–03:4x IST (2026-11-21 22:06 UTC). The user authorised steps 0–6 only.

Not done:
- no fault armed; the switch was not enabled;
- no job; no external API call;
- `migrate` was not built or run (no new migrations);
- no `.env`, code or Git change;
- root `handover.md` untouched.

## Step 0 — Preflight (03:36:25 IST)

| Check | Result |
|---|---|
| `/healthz` | ok; `live_workers` 2; `work_queue_consumers` 2; outbox_pending 0 |
| Queues | `jobs.work.v2` 0 / 0 unacked / 2 consumers; `jobs.retry.{5,10,30,90}s` all 0 |
| Non-terminal jobs | **0** |
| Baseline | worker-1 `35b1154cf89e`, worker-2 `44b596e75218` (image `6aad6362b38a`); gateway `2252e6e419d4`, outbox-publisher `d75e4adfc751` (`2a01b00b9603`); postgres `84989aa9e22c`, rabbitmq `2664d216b80a`, redis `f88eabc19088`; all healthy, 0 restarts |

## Steps 1–3 — Tag, build, verify

| Step | Command | Result |
|---|---|---|
| 1 | `docker tag notes-agent-backend:dev notes-agent-backend:pre-adr22-workers` | `6aad6362b38a` now tagged `pre-adr22-workers` (rollback) |
| 2 | `docker compose build worker` | exit 0 → `notes-agent-backend:dev` = **`78864607bddd`** (cached base layers from the 03:16 test build); no container touched |
| 3 | `docker run --rm --network none --entrypoint sh notes-agent-backend:dev -c 'test -f …/worker/demo_faults.py && test -f …/ops/demo_faults.py && grep -q demo_faults_enabled …/config.py'` | **HAS-ADR22** |
| 3b | sha256 of all backend files in the image vs the working tree | **50/50 identical** |

## Step 4 — Dry run

`docker compose --dry-run up -d --no-deps --no-build --scale worker=2 worker` →
`notes-agent-worker-1 Recreate`, `notes-agent-worker-2 Recreate` (plus Compose's temporary name
`44b596e75218_notes-agent-worker-2` during worker-2's recreation). **Nothing else.**

## Step 5 — Recreate the workers

`docker compose up -d --no-deps --no-build --scale worker=2 worker` → exit 0, 03:36:57 → 03:37:00;
worker-1 and worker-2 recreated and started.

## Step 6 — Verification

| Check | Result |
|---|---|
| Workers | **2**, both **healthy** within about 2 s; worker-1 `d0c52d4391f0`, worker-2 `52022abad07f`; image **`78864607bddd`**; 0 restarts |
| `DEMO_FAULTS_ENABLED` | **`false` on both workers**: in the container environment **and** in the loaded settings (`demo_faults_enabled= False`); `GEMINI_MODEL=gemini-3.5-flash-lite` on both |
| Startup logs | 2 × `worker started` with stages `[EXTRACTING_FRAMES, PUBLISHING, SUMMARIZING, TRANSCRIBING]`; **0** "demo faults ENABLED"; 0 errors or tracebacks |
| `/healthz` | ok; `live_workers` 2; `work_queue_consumers` 2 |
| RabbitMQ | `jobs.work.v2` 0 / 0 / **2** consumers; `jobs.dlq.audit` 0 / 0 / **2** |
| Gateway, publisher, postgres, rabbitmq, redis | **identical to the baseline** (IDs, images, start times, 0 restarts; `diff` empty) |
| Redis | 0 `demo:fault:*` keys |
| Jobs created since the deployment | 0 |

## Observations (not caused by the deployment)

- **`jobs.dlq` holds 4 messages** (it was 3 after the crash rehearsal). The fourth belongs to job
  **`44df00c0`** (video `favi7avxIag`):
  - created **21:27 UTC**, i.e. after the crash rehearsal and before this deployment, through the normal
    submission path (not by Claude);
  - `DEAD_LETTERED` at 21:31 UTC with `RETRIES_EXHAUSTED` / `last_error_code`
    **`GEMINI_DAILY_LIMIT_REACHED`**: our own limiter's daily Gemini budget refused it locally;
  - its DLQ copy was published via the outbox at 21:31:48; `dead_letter_audit` has no rows (a
    worker-initiated copy, as designed).
- **Limiter Gemini counter for the current day: 16/16.** Gemini requests are currently refused
  locally. This affects the live `frames_*` and `gemini_429_once` demos (they need a real summary). The
  `dlq` demo makes no external call and is not affected.

## Result

**Deployment successful:**
- only the two workers were replaced, now on the ADR-22 image `78864607bddd`;
- the switch is off on both;
- the worker count is 2;
- every other service is untouched;
- the rollback image is preserved.

## Files changed (docs only)

- `docs/PROJECT_STATUS_AND_HANDOVER.md`: §6 deployment, remaining work, history.
- `docs/FAILURE_DEMONSTRATION_RUNBOOK.md`: prerequisite marked done; enable/disable commands now
  `--no-deps --no-build --scale worker=2 worker`.
- `README.md`: same command form.
- This log.
