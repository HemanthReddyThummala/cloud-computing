# `frames_all` live demonstration (runbook 3-L, ADR-22)

Date: 2026-11-22 IST (09:01 UTC onwards). The user approved this one demo, for video `TlHvYWVUZyc`
only.

Constraints:
- Claude never submits the job; the user clicks once;
- `MAX_ATTEMPTS=5`, empty namespace and the limits unchanged;
- no other fault;
- no Redis counter changes, no `.env` access, no Git;
- root `handover.md` untouched.

**Spec requirement demonstrated:** "if every frame fails, publish a text-only page".

## 1. Preflight (09:01:57 UTC): passed

| Check | Result |
|---|---|
| Workers | `1b606c30fae2` / `5e4792a1e947` on `36c573002412`, healthy, 0 restarts; namespace `''`, `DEMO_FAULTS_ENABLED=False`, `MAX_ATTEMPTS=5`, limits 4/200000/16, backoff 10,30,90, `gemini-3.5-flash-lite` |
| Other services (baseline) | gateway `2252e6e419d4`, publisher `d75e4adfc751` (`2a01b00b9603`), postgres `84989aa9e22c`, rabbitmq `2664d216b80a`, redis `f88eabc19088`; healthy, 0 restarts |
| `/healthz` | ok; `live_workers` 2; consumers 2; `outbox_pending` 0 |
| Queues and jobs | work and retry queues 0; `jobs.dlq` 5; 18 jobs, 0 unfinished |
| Gemini (the deployed limiter's keys) | `ratelimit:gemini:rpd:2026-11-22` = **1/16** (the `frames_partial` job); RPM 0; no cooldowns |
| Demo keys | only the inert `demo:fault:job:82c49d33…` (`frames_partial`, a finished job) |

**Finding (pre-existing, ADR-13, harmless; not changed):**
- `ratelimit:gemini:tpm` holds 1 entry (3,535 tokens, granted 08:57:13) with **no TTL** (`PTTL -1`).
- **Cause:** `settle()` runs `ZREM` then `ZADD` in its Lua script. When the reservation was the key's
  only member, `ZREM` deletes the key and `ZADD` recreates it without the expiry.
- **Effect:** none on limiting. The acquire script removes entries older than the 60 s window
  (`ZREMRANGEBYSCORE`) before counting, so the stale entry is never counted. Only the cleanup of an
  idle key is affected.
- **Possible fix** (needs approval): re-apply `PEXPIRE` in `_SETTLE`.

## 2. Video suitability (from our own database; no external call)

The earlier real job `8513c17f` (2026-11-21) processed this video:
- "Kubernetes Explained in 6 Minutes | k8s Architecture";
- `duration_sec` **388** (6:28);
- `COMPLETED`, **`frames_requested` 4 / `frames_published` 4**, 0 warnings, no retries.

**Caveat:** the new job's visual moments come from a new summary. `frames_all` fails every frame, so
any count ≥ 1 demonstrates the text-only page.

## 3. Enable and arm

| UTC | Command | Result |
|---|---|---|
| — | `DEMO_FAULTS_ENABLED=true docker compose --dry-run up -d --no-deps --no-build --scale worker=2 worker` | only worker-1/worker-2; exit 0 |
| 09:02:29 | `DEMO_FAULTS_ENABLED=true docker compose up -d --no-deps --no-build --scale worker=2 worker` (no other overrides) | exit 0; worker-1 `8eeee93c6786`, worker-2 `28085a5867e8`, `36c573002412`, both healthy, 0 restarts |
| — | Verification | env and loaded settings on both: `demo=True`, `max_attempts=5`, namespace `''`, limits 4/200000/16, backoff 10,30,90; logs 2 × "demo faults ENABLED", 2 × `(default)`, 0 errors; `/healthz` ok, 2 live workers |
| 09:02:48 | `docker compose exec worker python -m notesagent.ops.demo_faults arm frames_all --video TlHvYWVUZyc` | `ARMED`, exit 0 |
| 09:02:48 | `… demo_faults status` | `demo:fault:video:TlHvYWVUZyc = frames_all (ttl 899s)`, so it expires at about 09:17 UTC |

A read-only watcher waits for the next job and checks the binding.

## 4. The user's single click and the binding

| UTC | Event |
|---|---|
| 09:03:42.314 | The user clicked Take Notes once → job **`74779edd-af1a-444d-9a7b-b3e0dd3e3317`**, the only job in the window |
| 09:03:43 | The watcher saw `demo:fault:job:74779edd… = frames_all`; the video key was consumed (0); 1 "bound" log line |
| — | The user confirmed the click ("i clicked"). Verification began only after that |

## 5. Result: passed

| Requirement | Observed |
|---|---|
| Final state | **`COMPLETED_WITH_WARNINGS`**, `attempts` 0, `infra_attempts` 0, no error; 09:03:42.314 → 09:03:54.934 (12.6 s) |
| Path | worker-2 (`28085a5867e8-1`): TRANSCRIBING → SUMMARIZING (09:03:45) → EXTRACTING_FRAMES (09:03:49) → PUBLISHING (09:03:53) → `COMPLETED_WITH_WARNINGS` (09:03:54); 4 checkpoints; no retries |
| All frames failed | `result`: **`frames_requested` 4, `frames_published` 0** |
| Warnings | 4 × `FRAME_EXTRACTION_FAILED`, reason **`DEMO_INJECTED_FAILURE`**, at 40.14 s, 68.34 s, 116.46 s and 193.74 s |
| Injection | 4 × "demo fault injected" (`EXTRACTING_FRAMES`, frame_index **0, 1, 2, 3**) |
| Text-only page | **1** `POST /v1/pages`, **0** `file_uploads`, 3 × `GET /v1/blocks/<id>/children`; `notion_page_url` present. The EXTRACTING_FRAMES checkpoint is 586 bytes (no image data; `frames_partial`'s was 235,626) |
| Gemini | **1** `POST …gemini-3.5-flash-lite:generateContent`; limiter `rpd:2026-11-22` 1 → **2**; no cooldown |
| Logs | 0 ERROR/CRITICAL/Traceback |
| Queues and jobs | work and retry queues 0; `jobs.dlq` 5 (unchanged); jobs 18 → 19 |

The Notion page was not opened by Claude. "Text-only" is inferred from 0 uploads and
`frames_published` 0.

## 6. Disabling and final verification (09:04 UTC)

| Step | Result |
|---|---|
| `disarm --video TlHvYWVUZyc` | "Nothing armed" (claimed); `status`: the job keys `74779edd…` (`frames_all`) and `82c49d33…` (`frames_partial`), ≈ 2 h TTL, left to expire, inert |
| Dry run (variables unset) | only worker-1/worker-2 Recreate; exit 0 |
| Disable `docker compose up -d --no-deps --no-build --scale worker=2 worker` | exit 0 at 09:04:20 |
| Workers | worker-1 `2416170d2628`, worker-2 `fcc150835a0d`; `36c573002412`; both **healthy**; 0 restarts; 2 × started, 0 "ENABLED", 0 errors |
| Effective config | **`DEMO_FAULTS_ENABLED=false`, `MAX_ATTEMPTS=5`**, namespace `''`, limits 4/200000/16 (env and settings) |
| Switch check | `arm` → `REFUSED`, exit 2 |
| `/healthz` | ok; `live_workers` 2; consumers 2; `outbox_pending` 0 |
| Other services | **identical to the preflight baseline** |

## Unresolved

- The pre-existing `settle()` TTL quirk (§1): harmless; a fix needs approval.
- **Remaining demo:** `gemini_429_once` (needs separate approval; 2 limiter permits, 1 real request).
  Gemini budget today: 2/16 used.
