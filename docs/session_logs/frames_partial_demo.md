# `frames_partial` live demonstration (runbook 3-L, ADR-22)

Date: 2026-11-22 IST (08:55 UTC onwards). The user approved this one demo, for video `M988_fsOSWo`
only.

Constraints:
- Claude never submits the job; the user clicks Take Notes exactly once;
- `MAX_ATTEMPTS=5` and the limits are unchanged;
- original limiter namespace (empty);
- no other fault;
- no Redis counter changes, no `.env` access, no Git;
- root `handover.md` untouched.

## 1. Preflight (08:55:13 UTC): passed

| Check | Result |
|---|---|
| Workers | `6666e84c8f6a` / `5bac36ad32ba` on `36c573002412`, healthy, 0 restarts; namespace `''`, `DEMO_FAULTS_ENABLED=False`, `MAX_ATTEMPTS=5`, limits 4/200000/16, backoff 10,30,90 |
| Other services (baseline) | gateway `2252e6e419d4`, publisher `d75e4adfc751` (`2a01b00b9603`), postgres `84989aa9e22c`, rabbitmq `2664d216b80a`, redis `f88eabc19088`; all healthy, 0 restarts |
| `/healthz` | ok; `live_workers` 2; consumers 2; `outbox_pending` 0 |
| Queues | work and retry queues 0; `jobs.dlq` 5 |
| Jobs | 17, 0 unfinished |
| Gemini (the deployed limiter's keys) | `ratelimit:gemini:rpd:2026-11-22` absent, so **0/16 used**; RPM 0; TPM 0; no cooldowns; 0 demo keys |

## 2. Video suitability (from our own database; no external call)

The earlier real job `5478d8b0` (2026-11-21) processed this video:
- "Cloud Computing In 6 Minutes | What Is Cloud Computing? | …";
- `duration_sec` **383** (6:23, inside 5–10 min);
- `COMPLETED`, with **`frames_requested` 5 / `frames_published` 5**, so it has ≥ 2 visual moments.

**Caveats:**
- The visual moments of the new job come from a new Gemini summary, so their number can differ.
  `frames_partial` needs ≥ 2 moments to inject anything; index 0 is never failed.
- The earlier job had one retryable `YOUTUBE_FORBIDDEN` (403) in EXTRACTING_FRAMES that recovered on
  retry. A similar retry here would be normal production behaviour.

## 3. Enable and arm

| UTC | Command | Result |
|---|---|---|
| — | `DEMO_FAULTS_ENABLED=true docker compose --dry-run up -d --no-deps --no-build --scale worker=2 worker` | only worker-1/worker-2 recreated; exit 0 |
| 08:55:39 | `DEMO_FAULTS_ENABLED=true docker compose up -d --no-deps --no-build --scale worker=2 worker` (no `MAX_ATTEMPTS` override) | exit 0; worker-1 `0e8bf92c37f9`, worker-2 `da8ed7b01348`, image `36c573002412`, both healthy, 0 restarts |
| — | Verification | env and loaded settings on both: `demo=True`, `max_attempts=5`, namespace `''`, limits 4/200000/16, backoff 10,30,90, model `gemini-3.5-flash-lite`; logs 2 × "demo faults ENABLED", 2 × namespace `(default)`, 0 errors; `/healthz` ok, 2 live workers |
| 08:56:01 | `docker compose exec worker python -m notesagent.ops.demo_faults arm frames_partial --video M988_fsOSWo` | `ARMED`, exit 0 |
| 08:56:01 | `… demo_faults status` | `demo:fault:video:M988_fsOSWo = frames_partial (ttl 899s)`, so it expires at about 09:11 UTC |

A read-only watcher waits for the next job and checks that `demo:fault:job:<id>` is bound.

## 4. The user's single click and the binding

| UTC | Event |
|---|---|
| 08:57:09.874 | The user clicked Take Notes once → job **`82c49d33-b912-47f1-9959-c85d47a7b620`**, the only job in the window |
| 08:57:11 | The watcher saw `demo:fault:job:82c49d33… = frames_partial`; the video key was consumed (0); 1 "bound" log line |
| — | The user confirmed the click ("i clicked take notes"). Verification began only after that |

## 5. Result: passed

| Requirement | Observed |
|---|---|
| Final state | **`COMPLETED_WITH_WARNINGS`**, `attempts` 0, `infra_attempts` 0, no error; 08:57:09.874 → 08:57:24.864 (15 s) |
| Path | claimed by worker-2 (`da8ed7b01348-1`) → TRANSCRIBING → SUMMARIZING (08:57:13) → EXTRACTING_FRAMES (08:57:16) → PUBLISHING (08:57:19) → `COMPLETED_WITH_WARNINGS` (08:57:24); 4 checkpoints; no retries |
| Partial degradation | `result`: **`frames_requested` 7, `frames_published` 4** |
| Warnings | 3 × `FRAME_EXTRACTION_FAILED`, reason **`DEMO_INJECTED_FAILURE`**, at 196.879 s, 241.439 s and 304.08 s |
| Injection | 3 × "demo fault injected" (`EXTRACTING_FRAMES`, frame_index **1, 3, 5**); frames 0, 2, 4 and 6 were real |
| Gemini | **1** `POST …/models/gemini-3.5-flash-lite:generateContent`; limiter `ratelimit:gemini:rpd:2026-11-22` 0 → **1**; no cooldown |
| Notion | **1** `POST /v1/pages`, 4 × `POST /v1/file_uploads` + 4 × `…/send` (the 4 real frames), 3 × `GET /v1/blocks/<id>/children`; `notion_page_url` present. **This verifies the extension's Notion sign-in** |
| Logs | 0 ERROR/CRITICAL/Traceback |
| Queues and jobs | work and retry queues 0; `jobs.dlq` 5 (unchanged); jobs 17 → 18 |

The Notion page's visual content (all text plus 4 images) was not opened by Claude. That it contains 4
images is inferred from the 4 file uploads and `frames_published` 4.

## 6. Disabling and final verification (08:57–08:58 UTC)

| Step | Result |
|---|---|
| `disarm --video M988_fsOSWo` | "Nothing armed" (already claimed); `status`: only `demo:fault:job:82c49d33… = frames_partial` (TTL ≈ 2 h, left to expire, inert with the switch off) |
| Dry run `docker compose up -d --no-deps --no-build --scale worker=2 worker` (variables unset) | only worker-1/worker-2 |
| Disable (same command) | exit 0 at 08:58:02 |
| Workers | worker-1 `1b606c30fae2`, worker-2 `5e4792a1e947`; image `36c573002412`; both **healthy**; 0 restarts; logs 2 × started, 0 "ENABLED", 0 errors |
| Effective config | **`DEMO_FAULTS_ENABLED=false`, `MAX_ATTEMPTS=5`**, namespace `''`, limits 4/200000/16 (env and loaded settings) |
| Switch check | `arm` → `REFUSED`, exit 2 |
| `/healthz` | ok; `live_workers` 2; consumers 2; `outbox_pending` 0 |
| Other services | **identical to the preflight baseline** (gateway, publisher, postgres, rabbitmq, redis) |

## Unresolved

- None for this demo.
- **Remaining:** `frames_all` and `gemini_429_once` (each needs separate approval). Gemini budget now
  1/16 used today.
