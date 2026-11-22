# Worker crash-recovery rehearsal (runbook §1-L) — SUCCESS

Date: 2026-11-22 02:44–02:50 IST (2026-11-21 21:14–21:20 UTC). The user approved **exactly one** real
job. The user submitted it via Take Notes; Claude did not submit anything.

Not done:
- no second job, no manual retry;
- no code, `.env` or Git change;
- the gateway, publisher, PostgreSQL, RabbitMQ and Redis were not touched;
- root `handover.md` untouched.

## 1. Preflight (02:44:05 IST)

| Check | Result |
|---|---|
| Workers | exactly 2: `notes-agent-worker-1` `35b1154cf89e`, `notes-agent-worker-2` `44b596e75218`; healthy, 0 restarts, image `6aad6362b38a` |
| Other services | gateway `2252e6e419d4`, outbox-publisher `d75e4adfc751` (image `2a01b00b9603`), postgres `84989aa9e22c`, rabbitmq `2664d216b80a`, redis `f88eabc19088`: healthy, 0 restarts |
| `/healthz` | ok; `live_workers` 2; `work_queue_consumers` 2; outbox_pending 0 |
| Queues | `jobs.work.v2` 0 messages / 0 unacked / 2 consumers; retry queues 0; `jobs.dlq` 3 (old) |
| Jobs | 11 total, latest 2026-11-21 13:10 UTC; 0 non-terminal |
| Gemini limiter | 11/16 used for the current day (enough for one job) |

## 2. Method

- **Watcher** (host Python, scratchpad `diag/crash_watcher.py`, not in the repo). It:
  - followed `docker compose logs -f --since <start> worker`;
  - adopted the first job claimed after it started, and would abort with no kill on a second job;
  - on that job's `stage started` / `EXTRACTING_FRAMES` line, mapped the log prefix `worker-N` →
    `notes-agent-worker-N`;
  - checked that the name matches `notes-agent-worker-\d+`, that the container is running, and that the
    **database status is `EXTRACTING_FRAMES`**;
  - only then ran `docker kill <that container>` (SIGKILL), once;
  - continued until `job finished`.
- Started at 21:14:56 UTC, before the user clicked.
- **User action:** Take Notes once on `https://www.youtube.com/watch?v=fyTxwIa-1U0` ("Session Vs JWT: The
  Differences You May Not Know!", 419 s). It had been published once before by job `cb5eb8e1` (see §5).

## 3. Timeline (watcher output; host clock UTC)

| Time | Event |
|---|---|
| 21:17:49.460 | job `6141336d-6942-4a79-8d44-a590a1595586` adopted: **claimed by `worker-2`** (`44b596e75218-1`); TRANSCRIBING started |
| 21:17:51.894 | TRANSCRIBING checkpointed → SUMMARIZING started |
| 21:17:55.139 | SUMMARIZING checkpointed → **EXTRACTING_FRAMES started on `worker-2`** |
| 21:17:55.672 | DB status `EXTRACTING_FRAMES` confirmed → **`docker kill notes-agent-worker-2`** (exit 0) |
| right after | **`worker-1`**: `job resumed from checkpoint` (status EXTRACTING_FRAMES) → EXTRACTING_FRAMES started |
| 21:17:59.546 | worker-1: EXTRACTING_FRAMES checkpointed → PUBLISHING started |
| 21:18:06.285 | PUBLISHING checkpointed → **COMPLETED**; `job finished` on worker-1 |
| | watcher: `recovered_and_finished`, survivor `notes-agent-worker-1`, **10.6 s after the kill** |

## 4. Verification

**Job row:**
- `status` **COMPLETED**, progress 100;
- **`attempts` 0**, `infra_attempts` 0 (the crash was not counted as a failure);
- no error, 0 warnings;
- result `{"notion_page_url": "https://app.notion.com/p/Session-Vs-JWT-The-Differences-You-May-Not-Know-41f20c0598bd817e80dbcc31a4f051c5", "frames_requested": 5, "frames_published": 5}`.

**`job_events` (database clock):**

| id | time | event | from → to | worker | detail |
|---|---|---|---|---|---|
| 80 | 21:17:49.209 | submitted | → QUEUED | | idempotency_key true |
| 81 | 21:17:49.382 | claimed | QUEUED → TRANSCRIBING | `44b596e75218-1` (worker-2) | redelivered false |
| 82 | 21:17:51.881 | transition | → SUMMARIZING | worker-2 | |
| 83 | 21:17:55.135 | transition | → EXTRACTING_FRAMES | worker-2 | |
| 84 | 21:17:55.459 | **resumed** | EXTRACTING_FRAMES | **`35b1154cf89e-1` (worker-1)** | **`redelivered: true`, `delivery_count: 1`** |
| 85 | 21:17:59.531 | transition | → PUBLISHING | worker-1 | |
| 86 | 21:18:06.283 | transition | → COMPLETED | worker-1 | |

The database and host clocks differ slightly. The broker only sets `redelivered: true` once it has lost
the killed consumer's connection.

**Checkpoints:**
- TRANSCRIBING and SUMMARIZING were written by worker-2; EXTRACTING_FRAMES (70 KB) and PUBLISHING by
  worker-1.
- **Only the interrupted stage was repeated; Gemini was not called again.**

**Killed container:** `notes-agent-worker-2` exited with code **137** (SIGKILL), `OOMKilled=false`.

**HTTP requests in the job window (both containers' logs):**

| Worker | Requests |
|---|---|
| worker-2 (killed) | 1 × `POST …generativelanguage…/v1/models…` (Gemini); **0 Notion requests** |
| worker-1 (survivor) | **1 × `POST https://api.notion.com/v1/pages`** (one page created), 10 × `POST /v1/file_uploads` (5 frames × create + send), 3 × `GET /v1/blocks` (reconciliation) |

**→ Exactly one Notion page was created for this job.** Both logs: 0 errors, 0 key- or token-like strings.

**Jobs created since the watcher started:** **1** (no duplicate job).

## 5. Notion note

The same video had been published before (job `cb5eb8e1`, 2026-11-21). The parent page therefore holds
two pages with this title. The demo's page is the one whose first block reads "… job
6141336d-6942-4a79-8d44-a590a1595586 …" (URL above). **User-side visual check of the page: pending.**

## 6. Restore (worker-only)

1. `docker compose --dry-run up -d --no-deps --scale worker=2 worker` → worker-2 "Started" (the same
   container), worker-1 "Running".
2. `docker compose up -d --no-deps --scale worker=2 worker` → worker-2 started.
3. worker-2: the **same ID `44b596e75218`**, healthy, new start time 21:18:50 UTC.
4. worker-1, gateway, publisher, postgres, rabbitmq and redis: the **same IDs, start times and 0
   restarts** as in the preflight.
5. `/healthz` ok, `live_workers` 2, consumers 2; `jobs.work.v2` 0 messages / 0 unacked; `jobs.dlq` still
   3 (nothing dead-lettered).

**Frame work directory:**
- worker-2 kept one **empty** (4 KB) directory `/tmp/notesagent-frames/1-6141336d-…-sl9si4d1`: the kill
  happened about 0.5 s into the stage, before any download data was written.
- It is removed at the start of worker-2's next frame stage by `purge_stale` (`frames.py:199`, ADR-16).
- worker-1's directory is empty (it cleaned up after success).

## 7. Result

**SUCCESS** against spec §6:
- the worker was killed with SIGKILL during frame extraction;
- the broker reassigned the unacknowledged message to the healthy worker;
- the job was not lost and resumed from its last checkpoint (only the interrupted stage repeated);
- it completed with 5/5 frames;
- exactly one Notion page was created for the job;
- the stack was restored to 2 healthy workers.

## 8. Files changed

- `docs/FAILURE_DEMONSTRATION_RUNBOOK.md`: readiness row; rehearsal notes (timing, watcher method,
  evidence, same-title note, temp directory).
- `docs/PROJECT_STATUS_AND_HANDOVER.md`: crash-recovery status, history.
- This log.
