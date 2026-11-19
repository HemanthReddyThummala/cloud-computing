# Session log — Phase A: deploy Phase 2 to the development stack and verify

- **Started:** 2026-11-19 23:25 (host clock)
- **Branch:** `phase-2`; HEAD = `main` = `phase1-baseline` = `49eed18`; 0 staged; 49 status entries.
  Git identity unchanged (no config changes); no commits, staging or GitHub operations — the user
  handles all of those.
- **Objective:** move the running dev stack from Phase 1 to Phase 2 (incl. ADR-12 dead-letter auditor),
  preserving all PostgreSQL and RabbitMQ data; retire the legacy `jobs.work` queue with the drain-safe
  migration; verify health, topology and tests. No new features.

## 1. Initial state (inspected)
| Item | State |
|---|---|
| App containers (gateway, outbox-publisher, worker ×2, migrate) | image `sha256:b49bf637…` (untagged, created 2026-11-19 05:43 = Phase 1); running/healthy for 12 h |
| `notes-agent-backend:dev` tag | `4e0d75cad61d` (built 13:21, used by no container) |
| Infra | postgres:16, rabbitmq:4.4.1-management (started 08:27Z), redis:7-alpine — healthy |
| Volumes | `notes-agent_pgdata`, `notes-agent_rabbitmqdata`, `notes-agent_redisdata` |
| DB migrations | `001_init`, `002_result_integrity` (003/004 not applied) |
| DB data | jobs 0, job_events 0, job_checkpoints 0, idempotency_keys 0, outbox 0 (0 pending) |
| RabbitMQ vhost `/` | `jobs.work` (quorum, DLX jobs.dlx, delivery-limit 10, Phase 1 args) 0 msgs, 0 unacked, **2 consumers** (Phase 1 workers); `jobs.dlq` 0; exchanges `jobs`, `jobs.dlx`; bindings `jobs→jobs.work (job.work)`, `jobs.dlx→jobs.dlq`; no policies; 0 credential queues |
| `.env` | contains Phase 1-era variables (names only inspected); Phase 2 variables not in `.env` use compose defaults |

## 2. Safety assessment before changes
- Migrations 003/004 are additive (columns with constant defaults, new table, constraint swap on an
  empty table) — tested on populated data (`test_migration_003/004`).
- `scripts/migrate_work_queue.py` → `python -m notesagent.ops.work_queue_migration`: deletes `jobs.work`
  only after unbinding it, applying a verified write-block policy and confirming (two management
  samples + live count) 0 ready / 0 unacked / 0 consumers; fail-closed otherwise; `--check` changes
  nothing.
- No volume is removed or recreated; infrastructure containers are not to be recreated.

## 3. Commands executed and results

| Time | Command | Result |
|---|---|---|
| 23:25 | `docker compose ps -a`, `docker inspect`, `docker images`, `docker volume ls`, psql read-only queries, `rabbitmqctl list_queues/exchanges/bindings/policies/consumers -p /` | initial state in §1 |
| 23:26 | `docker compose up -d --dry-run` | plan: **recreate** migrate, gateway, outbox-publisher, worker-1, worker-2; postgres, rabbitmq, redis **Running** (not recreated). Verified afterwards that nothing changed |
| 23:26:29 | `docker compose build migrate` | `notes-agent-backend:dev` → `63cf80e0e2cd` (runtime target; layers cached from the 22:4x test build). Verified in the image: migrations 001–004, `worker/dead_letters.py`, `worker/dlq_audit.py`, publisher sets `HEADER_EVENT_TYPE` |
| 23:26:44–48 | `docker compose up -d` | recreated exactly the 5 app containers; infrastructure untouched. Migrate log: `migration applied 003_retry_dlq_foundation`, `migration applied 004_dead_letter_audit_outcomes`, `applied_now: [003, 004]` |
| 23:26:50 | worker logs | both workers: `worker started` (`work_queue: jobs.work.v2`, `draining_legacy: true`), `draining legacy work queue` |
| 23:27 | `rabbitmqctl list_queues/consumers/bindings -p /` | `jobs.work.v2` 2 consumers; **`jobs.dlq.audit` 2 consumers** (one auditor per worker; ack_required, prefetch 1); `jobs.work` **0 consumers** (Phase 1 workers stopped by the recreate); delay queues `jobs.retry.{5,10,30,90}s`; `jobs.dlx → jobs.dlq` and `jobs.dlx → jobs.dlq.audit` |
| 23:27 | queue arguments | `jobs.dlq.audit`: quorum, `x-delivery-limit 21` (= 10 + 11), `x-dead-letter-exchange ""` (shown as `[]` by rabbitmqctl = default exchange), `x-dead-letter-routing-key jobs.dlq.audit.parked`, `at-least-once`, `reject-publish`. `jobs.dlq.audit.parked`: quorum, `x-delivery-limit -1`, only the implicit default binding. `jobs.work.v2`: as ADR-09 |
| 23:27 | `curl /healthz` | **HTTP 200** `{"status":"ok"}`: gateway ok, postgres ok (outbox_pending 0), rabbitmq ok (work_queue_consumers 2), redis ok (live_workers 2) |
| 23:27 | `python scripts/migrate_work_queue.py --check` | v2 present (args verified), v2 binding present, legacy binding present (would be removed), policy absent (would be applied), `jobs.work` verified empty and idle (would be deleted) → `OK: check: safe to proceed`, exit 0 |
| 23:27:24–37 | `python scripts/migrate_work_queue.py` | binding `jobs → jobs.work` removed; write-block policy applied and verified effective on `jobs.work` only; `jobs.work` verified empty twice (6 s apart) and live → **deleted**; policy removed → `OK: complete`, exit 0 |
| 23:27:39 | worker logs; `rabbitmqctl` | both workers: `legacy work queue no longer exists; drain finished`. `jobs.work` absent, its binding absent, policies empty |
| 23:27 | `migrate_work_queue.py --check` (re-run) | `jobs.work: absent`, policy absent → `OK: complete`, exit 0 (idempotent) |
| 23:27 | smoke job: `POST /jobs` (dummy placeholder token, not a real credential) | `202`, `Location: /jobs/7a072e67-…`; worker: `job claimed` → `pipeline stage not available in this build (TRANSCRIBING)` → `FAILED / PIPELINE_STAGE_UNAVAILABLE` (expected — no stages exist yet); `GET /results` 200 with the error; credential mailbox deleted; all queues back to 0 |
| 23:28:13–23:34:27 | `docker compose --profile test run --rm --no-deps tests pytest … test_dead_letter_audit test_topology_v2 test_migration_004 test_health test_api test_worker test_outbox test_work_queue_migration` (isolated `notes_test` vhost/DB) | **105 passed** in 371.51 s, 0 failed |
| 23:34 | post-check | all 7 services healthy; RabbitMQ/PostgreSQL/Redis restarts 0; `/healthz` 200 |

Not done deliberately: no artificial dead letter was injected into the dev vhost — `jobs.dlq` is
inspection-only and never consumed, so it would leave a permanent fake message there. The auditor's
behaviour is covered by `test_dead_letter_audit.py` (17 tests, real crash loops) on the same broker and
code; in dev it is verified as running (2 consumers, correct arguments).

## 4. Final state
| Item | State |
|---|---|
| App containers | gateway, outbox-publisher, worker ×2 on `notes-agent-backend:dev` (`63cf80e0e2cd`, Phase 2), healthy |
| Infrastructure | postgres / rabbitmq / redis **not recreated** (same containers, restarts 0) |
| Volumes | unchanged: `notes-agent_pgdata`, `notes-agent_rabbitmqdata`, `notes-agent_redisdata` |
| DB migrations | 001, 002, 003, 004 |
| DB data | previously empty; now 1 job (`FAILED`, the smoke job); `dead_letter_audit` 0 rows |
| RabbitMQ `/` | `jobs.work.v2`, `jobs.retry.{5,10,30,90}s`, `jobs.dlq`, `jobs.dlq.audit`, `jobs.dlq.audit.parked`; **no `jobs.work`**; no policies |

## 5. Unresolved / notes
1. The dev `.env` still lacks the Phase 2 variables (compose defaults apply) and still contains the
   unused `BACKOFF_JITTER` line (noted in an earlier session; `.env` holds secrets and was not edited).
2. The broker dead-letter path was not exercised live in the dev vhost (reason above).
3. The old Phase 1 image `b49bf637…` is now dangling (untagged, unused); it was not removed.
4. The pipeline stages remain unimplemented (Phase B); every real job ends FAILED by design.

## 6. Git
HEAD = `main` = `phase1-baseline` = `49eed18`; **0 staged**; nothing committed or pushed; Git
identity unchanged. `git status` 49 entries before; after = the same plus this new untracked log
(inside the already-untracked `docs/session_logs/`). No source file was changed in this session.
