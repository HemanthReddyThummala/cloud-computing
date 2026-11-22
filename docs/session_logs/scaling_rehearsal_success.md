# Worker-scaling rehearsal (runbook §4-L): 2 → 3 → 2 — SUCCESS

Date: 2026-11-22 02:41 IST (2026-11-21 21:11 UTC). User-approved. Previous attempt:
`scaling_rehearsal_stopped_dry_run.md`, which led to the corrected commands.

Not done:
- no jobs; no YouTube, Gemini or Notion calls;
- no code, `.env` or Git change;
- every Compose command named the `worker` service and used `--no-deps`;
- root `handover.md` untouched.

## 1. Baseline (02:41:02 IST)

| Container | ID | Restarts | Started (UTC) | Health |
|---|---|---|---|---|
| notes-agent-worker-1 | `35b1154cf89e` | 0 | 2026-11-21 11:28:39.160 | healthy |
| notes-agent-worker-2 | `44b596e75218` | 0 | 2026-11-21 11:28:39.305 | healthy |
| notes-agent-gateway-1 | `2252e6e419d4` | 0 | 2026-11-20 22:56:26.636 | healthy |
| notes-agent-outbox-publisher-1 | `d75e4adfc751` | 0 | 2026-11-20 22:56:26.359 | healthy |
| notes-agent-postgres-1 | `84989aa9e22c` | 0 | 2026-11-19 05:43:34 | healthy |
| notes-agent-rabbitmq-1 | `2664d216b80a` | 0 | 2026-11-19 08:27:49 | healthy |
| notes-agent-redis-1 | `f88eabc19088` | 0 | 2026-11-19 05:43:33 | healthy |

- `/healthz`: ok; `live_workers` **2**; `work_queue_consumers` **2**; outbox_pending 0.
- RabbitMQ: `jobs.work.v2` 2 consumers, `jobs.dlq.audit` 2.
- Services: 1 each, plus worker × 2.

## 2. Scale up (2 → 3)

| Step | Command | Result |
|---|---|---|
| Dry run | `docker compose --dry-run up -d --no-deps --scale worker=3 worker` | worker-1/2 "Running"; only worker-3 created/started |
| **Scale up** (02:41:09) | `docker compose up -d --no-deps --scale worker=3 worker` | exit 0; worker-1/2 "Running"; **worker-3 created and started** |

**Verification:**
- `notes-agent-worker-3` (`df9c6547aa68`, image `6aad6362b38a`, same as workers 1 and 2) was
  **healthy after about 2 s** and logged `worker started` with all four stages.
- `docker compose ps -q worker` → **3**.
- `/healthz`: ok, **`live_workers` 3**, **`work_queue_consumers` 3**.
- `rabbitmqctl list_queues -p / name consumers`: `jobs.work.v2` **3**, `jobs.dlq.audit` **3**.
  - A first attempt printed nothing because Git Bash rewrote `-p /`; it was re-run with
    `MSYS_NO_PATHCONV=1`.
- Workers 1 and 2, the gateway, publisher, postgres, rabbitmq and redis had the **same IDs, 0 restarts
  and unchanged start times**.

## 3. Scale down (3 → 2)

| Step | Command | Result |
|---|---|---|
| Dry run | `docker compose --dry-run up -d --no-deps --scale worker=2 worker` | worker-1/2 "Running"; worker-3 stop + remove |
| **Scale down** (02:41:31) | `docker compose up -d --no-deps --scale worker=2 worker` | exit 0; **worker-3 stopped and removed** in about 1 s; worker-1/2 "Running" |

**Verification:**
- `/healthz` back to **`live_workers` 2** after about **3 s**; `work_queue_consumers` **2**; status ok.
- RabbitMQ: `jobs.work.v2` 0 messages / **2** consumers; `jobs.dlq.audit` 0 / **2**.
- `docker compose ps -q worker` → **2**; no `notes-agent-worker-3` container remains.
- **The post-run state of all 7 containers is identical to the baseline** (`diff` of the recorded ID /
  restarts / start time / health lines: no difference).
- Worker errors since the scale-up: **0**. Job rows created: **0**.

## 4. Result

**Success:** 2 → 3 → 2 with only the worker service touched, the configuration restored exactly, and no
unrelated container recreated or restarted.

**Runbook updates:**
- the readiness table marks scaling as rehearsed;
- the `live_workers` note is corrected to what was observed (about 3 s after a graceful stop, up to the
  15 s heartbeat TTL after a SIGKILL).

## 5. Files changed

- `docs/FAILURE_DEMONSTRATION_RUNBOOK.md`: readiness row, `live_workers` note.
- `docs/PROJECT_STATUS_AND_HANDOVER.md`: failure-demo row, history.
- This log.

## 6. Not covered by this rehearsal

- Jobs processed while 3 workers run (optional in the runbook; needs real jobs and approval).
- Remaining live demos: crash recovery (ready, 1 real job); DLQ, degradation and 429 (live gaps, need
  the demo fault switch or a D-1 decision).
