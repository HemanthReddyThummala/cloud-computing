# Worker-scaling rehearsal (runbook §4-L): stopped at the dry run, runbook corrected

Date: 2026-11-22 02:38 IST.

**Result: scaling was NOT performed.** A dry run of the documented command showed that it would also
recreate unrelated services. Per the instruction "if an unexpected problem occurs, stop and report",
nothing was changed on the stack.

Not done:
- no job; no Gemini, YouTube or Notion call;
- no `.env` or code change;
- no Git operation;
- root `handover.md` untouched.

## 1. Initial state (recorded before anything else)

| Item | Value |
|---|---|
| Worker replicas | **2**: `notes-agent-worker-1`, `notes-agent-worker-2`, both running and healthy, 0 restarts, image `6aad6362b38a`, started 2026-11-21 11:28 UTC |
| `WORKER_REPLICAS` (`.env`, non-secret) | `2` |
| Other services | gateway and outbox-publisher healthy on image **`2a01b00b9603`** (since 2026-11-20 22:56 UTC); postgres, rabbitmq and redis healthy since 2026-11-19; migrate exited (0) |
| `notes-agent-backend:dev` tag | **`6aad6362b38a`** (the worker image from the worker-only Flash-Lite deployment) |
| `/healthz` | `status: ok`; postgres ok (outbox_pending 0); rabbitmq ok (`work_queue_messages` 0, **`work_queue_consumers` 2**); redis ok (**`live_workers` 2**) |
| RabbitMQ consumers | `jobs.work.v2` **2**, `jobs.dlq.audit` **2** |

## 2. The problem

`docker compose --dry-run up -d --scale worker=3` (the runbook §4-L command at the time) reported:
- **Recreate:** `notes-agent-gateway-1`, `notes-agent-outbox-publisher-1`, `notes-agent-migrate-1`
  (re-run);
- **Running (untouched):** postgres, rabbitmq, redis, worker-1, worker-2;
- **Create:** worker-3.

**Cause:**
- A bare `up` reconciles every service in the project.
- The gateway and publisher run `2a01b00b9603`, but their Compose image `notes-agent-backend:dev` now
  resolves to `6aad6362b38a`, because the worker-only deployment re-tagged `:dev`.
- Compose therefore treats them as outdated and would recreate them. That restarts unrelated services
  (forbidden) and silently moves them to a new image.
- The same bare form appeared in runbook §1-L step 6, §4-L, the §4-L cleanup and `README.md` §7.

## 3. Verified correction (dry runs only)

| Command | Dry-run result |
|---|---|
| `docker compose --dry-run up -d --no-deps --scale worker=3 worker` | only **worker-3 created/started**; worker-1 and worker-2 "Running" (untouched); nothing else |
| `docker compose --dry-run up -d --no-deps --scale worker=2 worker` (from the current 2) | worker-1 and worker-2 "Running"; no change |

After the dry runs, `docker compose ps` was unchanged: all 7 services up and healthy, with the same uptimes.

## 4. Files changed (documentation only)

| File | Change |
|---|---|
| `docs/FAILURE_DEMONSTRATION_RUNBOOK.md` | scaling commands → `docker compose up -d --no-deps --scale worker=N worker` (§1-L step 6, §4-L up/down, §4-L cleanup); a warning box explaining why, and advice to dry-run first |
| `README.md` §7 | same two commands corrected |
| `docs/PROJECT_STATUS_AND_HANDOVER.md` | rehearsal status, history |
| this log | — |

## 5. Related observation (no action)

The runbook's DLQ demo-profile command `docker compose up -d gateway outbox-publisher worker`
intentionally recreates those services, and would also re-run migrate and move the gateway and
publisher to the current `:dev` image (`6aad6362b38a`). That is intended for that demo, but it should be
known beforehand.

## 6. Next step (needs the user's go-ahead)

Re-run the scaling rehearsal with the corrected commands:
1. `docker compose up -d --no-deps --scale worker=3 worker`;
2. verify 3 healthy workers, `live_workers` 3 and consumers 3;
3. `docker compose up -d --no-deps --scale worker=2 worker`;
4. verify 2 healthy workers and consumers 2, with workers 1 and 2 never restarted.
