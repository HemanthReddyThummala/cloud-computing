# ADR-23 worker-only deployment (limiter namespace present, empty)

Date: 2026-11-22 14:19 IST (08:49 UTC). This followed the procedure in
`adr23_gemini_limiter_namespace.md` §5, with the user's approval.

Not done:
- no API key switch; `.env` / `.env.gemini-alt` neither read nor displayed;
- no job; no external API call;
- no Redis counter changed;
- `migrate` not built or run (no new migrations);
- no code change; no Git operation;
- root `handover.md` untouched.

The shell had `GEMINI_LIMITER_NAMESPACE`, `DEMO_FAULTS_ENABLED`, `MAX_ATTEMPTS` and `GEMINI_API_KEY`
explicitly unset, so only `.env` and the Compose defaults applied.

## Baseline (08:49:36 UTC)

| Item | Value |
|---|---|
| gateway | `2252e6e419d4`, image `2a01b00b9603`, started 2026-11-20 22:56:26, 0 restarts, healthy |
| outbox-publisher | `d75e4adfc751`, image `2a01b00b9603`, started 2026-11-20 22:56:26, 0 restarts, healthy |
| postgres / rabbitmq / redis | `84989aa9e22c` / `2664d216b80a` / `f88eabc19088`, 0 restarts, healthy |
| workers | `07df16513cda` / `47094d34563a` on `78864607bddd` (ADR-22), healthy |
| Redis | `ratelimit:gemini:rpd:2026-11-21` = 16; no other Gemini, cooldown or demo keys |
| Queues and jobs | `jobs.work.v2` 0 (2 consumers), retry queues 0, `jobs.dlq` 5; 17 jobs, 0 unfinished |
| Images | `notes-agent-backend:dev` = `36c573002412`; `pre-adr23-workers` = `78864607bddd` |

## Steps

| Step | Result |
|---|---|
| Dry run `docker compose --dry-run up -d --no-deps --no-build --scale worker=2 worker` | `notes-agent-worker-1 Recreate`, `notes-agent-worker-2 Recreate` (plus Compose's temporary `<id>_notes-agent-worker-N` names). **Nothing else.** Exit 0 |
| Deploy `docker compose up -d --no-deps --no-build --scale worker=2 worker` | exit 0, 08:49:49 → 08:49:51 UTC; both healthy within seconds |

## Verification

| Check | Result |
|---|---|
| Workers | worker-1 `6666e84c8f6a`, worker-2 `5bac36ad32ba`; image **`36c573002412`**; both **healthy**; 0 restarts |
| `GEMINI_LIMITER_NAMESPACE` | env `[]` (empty) and loaded setting `''` on both. The limiter computes the original keys `ratelimit:cooldown:gemini`, `ratelimit:gemini:rpm`, `ratelimit:gemini:tpm`, `ratelimit:gemini:rpd:<day>` |
| `DEMO_FAULTS_ENABLED` | `false` (env) / `False` (settings) on both |
| `MAX_ATTEMPTS` | `5` on both |
| Other worker settings | limits 4 / 200000 / 16; `GEMINI_MODEL=gemini-3.5-flash-lite`; API key present (boolean only; never printed) |
| Startup logs | 2 × "worker started", each with `"gemini_limiter_namespace": "(default)"`; 0 "demo faults ENABLED"; 0 ERROR/CRITICAL/Traceback |
| `/healthz` | `status ok`; `live_workers` **2**; `work_queue_consumers` 2; `outbox_pending` 0 |
| Gateway, publisher, postgres, rabbitmq, redis | **identical to the baseline** (ID, image, start time, 0 restarts, healthy; `diff` empty) |
| Redis | `ratelimit:gemini:rpd:2026-11-21` = **16 (unchanged)**; 0 namespaced keys; 0 cooldowns |
| Queues and jobs | `jobs.work.v2` 0 / 2 consumers; `jobs.dlq` 5; jobs 17 (no new job) |

**Observation (fact only):** the limiter's own day calculation now selects
`ratelimit:gemini:rpd:2026-11-22`, which does not exist yet (0 used). The `2026-11-21` key keeps its
value. Whether to use the original project or the `alt` namespace for the remaining demos is the
user's decision. A readiness check should read the key that the deployed limiter computes.

## Result

**Deployment successful:**
- only the two workers were replaced, now on `36c573002412`;
- behaviour is identical to before (empty namespace = original keys, switch off, `MAX_ATTEMPTS=5`);
- all other services are untouched;
- the rollback image `pre-adr23-workers` is kept.

## Files changed (docs only)

- `docs/PROJECT_STATUS_AND_HANDOVER.md`: header, feature row, §6 deployment, §9, §10.
- This log.
