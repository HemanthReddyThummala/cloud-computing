# Session log — Phase 2, Part 3: crash-test reliability, 10 consecutive runs

- **Date:** 2026-11-19 (session started 16:06:55 IST)
- **Branch:** `phase-2`; `main` = `phase1-baseline` = `49eed18`; 0 commits since baseline; 0 staged files
- **Scope:** run the existing 16-test crash set 10 times sequentially with the existing diagnostics
  (`tests/support/diagnostics.py`) to try to reproduce crash-test repetition 2. **No** changes to
  application code, tests, assertions, timings, retry policy or RabbitMQ configuration. No commits.
  Part 3 is not declared complete in this session.
- Previous session log (not modified): `phase2_part3_crash_diagnostics.md`.

## 1. Objective
Determine whether the intermittent failure of crash-test repetition 2 (2 failed + 1 error, 502 s;
`test_crash_after_dead_letter_commit_before_ack`, `test_lock_unavailable_delays_without_counting`)
reproduces with diagnostics enabled.

## 2. Initial state (16:06:55 IST)

| Item | Value |
|---|---|
| RabbitMQ | healthy, restarts 0, OOMKilled false, started 08:27:49Z, 155 MiB / 512 MiB, **no alarms**, watermark 307 MiB of 512 MiB |
| PostgreSQL | healthy, restarts 0, OOMKilled false, started 05:43:34Z, 67 MiB |
| Redis | healthy, restarts 0, OOMKilled false, started 05:43:33Z, 6 MiB |
| Previous evidence | full suite 555 passed; targeted 49 passed; crash reps #1 16/16, **#2 2F+13P+1E (502 s)**, #3–#5 16/16; diagnostic validation run 16/16 |
| Existing diagnostics files | 5 intentional self-check files (`*_test_intentional_*`) from the previous session — preserved, excluded from these results |

## 3. Method
- **Crash set (unchanged):** `-k "crash or duplicate or sigkill or db_failure or lock_unavailable or
  delay_publish or survives or never_process"` over `test_retries.py test_worker.py test_mailbox.py
  test_mailbox_races.py test_concurrency.py test_outbox.py test_results_and_dlq.py` → 16 selected.
- **Runner:** `scratchpad/reliability_runner.sh` (outside the repository) runs the 10 repetitions
  strictly sequentially, each in its own `docker compose --profile test run --rm --no-deps tests` container.
- **Persisted per repetition** (git-ignored, `backend/test-artifacts/reliability/`):
  `rep_NN.txt` (complete pytest output, `-rA --tb=long --durations=0`), `rep_NN.xml` (JUnit: per-test
  outcome incl. setup/teardown errors), `rep_NN_health.txt` (before/after health of RabbitMQ, PostgreSQL,
  Redis: health, restart count, OOMKilled, memory, RabbitMQ alarms), new diagnostics files (by name).
- **Continuous monitor:** `backend/test-artifacts/reliability/memory_samples.txt` — every 10 s: memory of
  RabbitMQ / PostgreSQL / Redis and RabbitMQ restart count.
- **Health gate:** before each repetition the runner checks all three services are `healthy` with an
  unchanged restart count; otherwise it stops (evidence is never overwritten — files are per repetition).
- Failed repetitions are **not** rerun or replaced.

## 4. Attempt 1 — aborted because of a runner (harness) defect (16:07–16:10 IST)

- **Rep 01:** all **16 tests passed** (16 progress dots, no F/E), duration 88 s — but pytest exited 1 at
  session end with `PermissionError: [Errno 13] Permission denied: '/app/C:'` in `junitxml.pytest_sessionfinish`.
- **Cause:** Git Bash (MSYS) converted the container path passed to `--junitxml=/app/test-artifacts/...`
  into a Windows path, so the JUnit file could not be written. A runner defect — not a test failure.
- **Rep 02:** was in progress; I stopped the runner and removed its in-flight test container
  (harness cleanup, not a test outcome).
- **Evidence preserved, not replaced:** `backend/test-artifacts/reliability/attempt1_harness_defect/`
  (`rep_01.txt`, `rep_01_health.txt`, `rep_02.txt`, `rep_02_health.txt`, `summary.txt`, `memory_samples.txt`).
- **Fix (runner only, outside the repository):** prefix the `docker compose run` with
  `MSYS_NO_PATHCONV=1`. No test, timing, application or configuration change.
- **Second harness issue:** stopping the background task did not end the runner's bash loop — it
  continued with rep 02 (exit 137 = its container removed by me, 16 s) and started rep 03. I killed the
  runner processes by PID and removed the in-flight rep-03 container; those files were also moved into
  `attempt1_harness_defect/` (`rep_03*.txt`, `summary_continued.txt`). Infrastructure verified healthy
  afterwards (all three services healthy, 0 restarts, no OOM). Lesson recorded: stop the runner by PID.
- A **fresh, complete series of 10 repetitions** was then started (attempt 2, §5). Attempt-1 rep 01
  (16/16 passed) is reported as additional evidence only.

## 5. Results — attempt 2 (updated as repetitions complete)

| Rep | Start | Duration (wall / pytest) | Passed | Failed | Errors | Diagnostics files | Infra after |
|---|---|---|---|---|---|---|---|
| 01 | 16:10:31 | 87 s / 84.26 s | 16 | 0 | 0 | none | see §6 |
| 02 | 16:12:07 | 88 s / 85.00 s | 16 | 0 | 0 | none | see §6 |
| 03 | 16:13:45 | 89 s / 85.43 s | 16 | 0 | 0 | none | see §6 |
| 04 | 16:15:24 | 88 s / 85.68 s | 16 | 0 | 0 | none | see §6 |
| 05 | 16:17:02 | 88 s / 84.97 s | 16 | 0 | 0 | none | see §6 |
| 06 | 16:18:39 | 88 s / 85.02 s | 16 | 0 | 0 | none | see §6 |
| 07 | 16:20:17 | 87 s / 84.88 s | 16 | 0 | 0 | none | see §6 |
| 08 | 16:21:53 | 88 s / 85.47 s | 16 | 0 | 0 | none | see §6 |
| 09 | 16:23:31 | 89 s / 85.68 s | 16 | 0 | 0 | none | see §6 |
| 10 | 16:25:10 | 88 s / 84.49 s | 16 | 0 | 0 | none | see §6 |
| **Total** | 16:10:31–16:26:42 | 87–89 s each | **160** | **0** | **0** | **none** | all healthy |

JUnit cross-check (`rep_NN.xml`): every repetition `tests=16 failures=0 errors=0 skipped=0` — errors
include setup/teardown errors, so **no setup or teardown error occurred**. Exit code 0 every time.

### Per-test outcomes (10 runs each)

| Test | Passed | Duration min–max |
|---|---|---|
| test_retries::test_crash_after_dead_letter_commit_before_ack (failed in old rep 2) | 10/10 | 2.55–2.95 s |
| test_retries::test_lock_unavailable_delays_without_counting (failed in old rep 2) | 10/10 | 6.60–7.08 s |
| test_retries::test_crash_after_retry_commit_before_ack | 10/10 | 4.59–5.03 s |
| test_retries::test_crash_during_the_retried_stage | 10/10 | 5.52–5.90 s |
| test_retries::test_crash_mid_stage_before_retry_tx_counts_nothing | 10/10 | 3.09–3.44 s |
| test_retries::test_db_failure_in_retry_transaction_counts_nothing | 10/10 | 13.76–14.14 s |
| test_retries::test_delay_publish_failure_leaves_message_unacked_then_recovers | 10/10 | 11.71–11.95 s |
| test_retries::test_duplicate_retry_copies_after_outbox_publisher_crash | 10/10 | 5.64–6.17 s |
| test_worker::test_sigkill_mid_stage_job_is_recovered_by_another_worker | 10/10 | 1.98–2.46 s |
| test_mailbox::test_mailbox_survives_worker_crash_and_is_deleted_after_completion | 10/10 | 2.10–2.34 s |
| test_mailbox_races::test_crash_between_get_and_nack_returns_the_same_message | 10/10 | 1.12–1.50 s |
| test_mailbox_races::test_worker_waits_for_credential_held_by_crashed_peer | 10/10 | 2.83–3.16 s |
| test_concurrency::test_two_workers_never_process_the_same_job_simultaneously | 10/10 | 4.89–5.20 s |
| test_outbox::test_crash_after_confirm_duplicates_message_but_job_runs_once | 10/10 | 2.53–2.83 s |
| test_results_and_dlq::test_duplicate_deferral_does_not_consume_attempts | 10/10 | 11.92–12.20 s |
| test_results_and_dlq::test_sigkill_redelivery_counts_toward_delivery_limit | 10/10 | 1.23–1.57 s |

Every test's duration varied by ≤ 0.6 s across the 10 runs — no run came close to its timeouts.

## 6. Infrastructure observations (attempt 2)

- **Health snapshots** (`rep_NN_health.txt`, before and after each repetition = 20 snapshots): RabbitMQ,
  PostgreSQL and Redis `healthy`, `restarts=0`, `OOMKilled=false` in all 20; RabbitMQ
  "reported no alarms, local or clusterwide" in all 20.
- **Memory** (`memory_samples.txt`, 79 samples every 10 s): RabbitMQ 155.6–**237.5 MiB** (watermark
  307 MiB, limit 512 MiB); PostgreSQL 66.6–80.8 MiB; Redis 4.8–6.9 MiB. RabbitMQ restart count stayed 0.
- **RabbitMQ log 10:40–10:58Z:** no alarm, memory, OOM or quorum force-delete messages.
- **Docker events 10:40–10:58Z:** no die/oom/restart for any service container.
- **Workers:** worker processes run inside each test container; the SIGKILLs are the tests' intentional
  crash injections and are asserted by the tests (e.g. exit code −9). No unexpected termination was
  observed — every test passed and no diagnostics file was produced. Dev-stack services were not touched.
- **Unusual delays:** none; wall time per repetition 87–89 s (cf. old rep 2: 502 s).

## 7. Interpretation

- The failure of old crash-test repetition 2 (2 failures + 1 error, 502 s) **did not reappear** in 10
  consecutive repetitions (160/160 test executions passed) with diagnostics enabled — nor in attempt-1
  rep 01 (16/16).
- Since the RabbitMQ memory fix the crash set has run 17 times: old #1, #3, #4, #5, the
  diagnostic-validation run and these 10 passed (15 runs); attempt-1 rep 01 also had 16/16 tests pass
  (its exit code 1 came from the runner's JUnit path defect); the only failing run remains old #2.
- **No evidence was produced about the cause** of old repetition 2 (no failure ⇒ no diagnostics). Its
  abnormal duration (502 s vs ~85 s) suggests a global stall (environment- or broker-level) rather than a
  logic defect in one test, but this is a hypothesis without evidence. Old rep 2 ran **after** the
  RabbitMQ memory fix, so the memory fix alone does not explain it.
- Not a shared-root-cause finding: nothing links it to the Phase 1 mailbox-test failure either.

## 8. Unresolved questions / next steps
1. Root cause of old repetition 2 — unknown, not reproduced (0 failures in 160 executions here).
2. Only 2 of the 16 crash tests carry `diag`; a failure in another test would show pytest's traceback
   but no diagnostics file (optional extension, needs approval).
3. The five intentional self-check diagnostics files are still preserved (not part of these results).
4. Part 3 is **not** declared complete; no commits. Awaiting review.

## 9. Git and scope at end of session
- Only repository file modified in this session: this log (`docs/session_logs/…10_runs.md`, new,
  untracked). No application code, tests, assertions, timings, retry policy or RabbitMQ configuration
  changed. Runner script lives in the scratchpad (outside the repository).
- Branch `phase-2`; `main` = `phase1-baseline` = `49eed18`; 0 commits since baseline; **0 staged**.
  Evidence files are git-ignored under `backend/test-artifacts/reliability/`.
