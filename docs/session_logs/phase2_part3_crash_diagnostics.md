# Session log — Phase 2, Part 3: crash-test failure diagnostics

- **Date:** 2026-11-19 (session started 15:50 IST)
- **Branch:** `phase-2` (baseline `main` = `phase1-baseline` = `49eed18`; no commits on `phase-2`)
- **Scope (authorised):** add observational diagnostics to two intermittently failing crash tests and
  verify them. No assertion, timing, retry, transaction, worker or RabbitMQ-configuration changes.
  Part 3 is **not** declared complete in this session.

## 1. Objective

Capture enough evidence to identify the cause if either test fails again:

1. `tests/test_retries.py::test_crash_after_dead_letter_commit_before_ack`
2. `tests/test_retries.py::test_lock_unavailable_delays_without_counting`

…and the unexplained setup/teardown **error** seen in crash-test repetition 2.

## 2. Initial project state

| Item | State |
|---|---|
| RabbitMQ memory | `total_memory_available_override_value = 536870912`, watermark 307 MiB; no OOM since |
| Full suite | 555 passed, 0 failed, 0 skipped, 0 expected failures |
| Targeted Part 3 tests | 49 passed |
| Crash-test repetitions | #1 16/16, **#2 2 failed + 13 passed + 1 error (502 s)**, #3 16/16, #4 16/16, #5 16/16 |
| Failed tests run individually | 5/5 and 5/5 passed |
| Failure details of repetition 2 | **lost** — the run's output was filtered (`grep`), no traceback kept |
| Application code changed during investigation | none |

## 3. Inspection (before any change)

### Files inspected
- `backend/tests/test_retries.py` — both failing tests, helpers `kinds`, `outbox_rows`, `run_count`.
- `backend/tests/test_mailbox_races.py` — existing diagnostic pattern (`test_missing_mailbox_fails_immediately`).
- `backend/tests/conftest.py` — fixtures `procs`, `fx`, `db`, `api`, autouse `clean_state` (vhost reset).
- `backend/tests/support/processes.py` (`Processes`: start/sigkill/log), `support/helpers.py`
  (`wait_until`, `wait_for_status`), `support/rabbit.py` (`RabbitAdmin`, `queue_state`),
  `support/run_worker_hooks.py` (crash / DB-failure hooks).

### Observations
- **Mailbox pattern:** inline milestone polling + a `diagnostics()` text passed as the *assertion message*
  (elapsed time, milestones, job row, event timeline, outbox row, worker return code, mailbox listing,
  worker-log tail). It is evaluated only when one of the test's own `assert`s fails.
- **Gaps of that pattern for the current problem:**
  1. failures raised inside helpers (`wait_until`/`wait_for_status` timeouts, `Popen.wait` timeouts,
     `procs.wait_for_log`) are plain `AssertionError`/`TimeoutExpired` *without* diagnostics;
  2. **setup/teardown errors** (e.g. `clean_state` vhost reset, process teardown) are not covered at all —
     repetition 2 had exactly such an error;
  3. diagnostics live only in pytest's stdout of a `--rm` container: if the output is filtered or lost,
     the evidence is gone (this is what happened in repetition 2).
- `test_crash_after_dead_letter_commit_before_ack` has 8 sequential steps, 4 of them waits with their own
  timeouts (30 s / 20 s / 20 s / 10 s) — a slow broker could exceed any one of them.
- `test_lock_unavailable_delays_without_counting` relies on the 5 s lock-delay queue round trip within a
  30 s wait.

## 4. Implementation plan (observational only)

1. **`backend/tests/support/diagnostics.py` (new):** a read-only collector `TestDiagnostics` that reuses
   the mailbox pattern's content and adds what the task requires — milestones with timestamps and
   durations, tracked job ids, job row (status, retry state, counters, error), event timeline with
   event/message ids, outbox rows (destinations, published/attempts/last_error), checkpoints, fixture
   markers (run/started/hook files with times), worker processes (pid, return code incl. SIGKILL, log
   tail), RabbitMQ queue state via the management API (no AMQP declares — read-only), RabbitMQ node
   health (uptime ⇒ restarts, memory used/limit, memory/disk alarms), PostgreSQL start time and Redis
   uptime (restart detection). Every section is wrapped so collection can never raise or alter state.
2. **`backend/tests/conftest.py`:** an opt-in `diag` fixture and a `pytest_runtest_makereport`
   hook-wrapper that, when a test using `diag` fails in **setup, call or teardown**, attaches the
   diagnostics to pytest's failure report (pytest's own traceback is kept unchanged) **and writes them to
   a file**. Successful runs produce no extra output.
3. **Persistence:** diagnostics files go to `/app/test-artifacts/diagnostics/` inside the test container,
   bind-mounted to `backend/test-artifacts/` on the host (test profile only) and git-ignored, so they
   survive the `--rm` container.
4. **The two tests:** add the `diag` fixture parameter, `diag.track_job(job_id)` and `diag.mark(...)`
   milestone calls between existing statements. **No assertion, wait, timeout or ordering change.**
5. **Verification:** run both tests individually; run the 16-test crash set once; prove diagnostics are
   produced for an intentional call/setup/teardown failure using a throw-away test module that exists only
   inside an ephemeral container (not added to the repository); confirm passing runs are quiet; diff the
   two tests to show only diagnostic lines were added.

## 5. Work log

### 5.1 Files created / modified (implementation step, ~16:00 IST)

| File | Change | Why |
|---|---|---|
| `backend/tests/support/diagnostics.py` **(new)** | `TestDiagnostics` read-only collector: milestones, tracked jobs (row, retry state, counters, error), event timeline incl. event ids, outbox rows, checkpoints, RabbitMQ queues + node health (management API GETs only), PostgreSQL start time, Redis uptime, fixture marker files with times, processes (pid, return code, log tail 40 lines), verbatim pytest traceback; `write()` to `DIAG_DIR` | One reusable implementation of the mailbox pattern that also covers helper timeouts and setup/teardown failures |
| `backend/tests/conftest.py` | `pytest_runtest_setup` hook creates the collector before any fixture for tests that request `diag`; `diag` fixture (attaches `procs`, `fx`); `pytest_runtest_makereport` hook-wrapper attaches diagnostics to the failure report and writes them to a file on failure in setup/call/teardown; import of `TestDiagnostics` | Automatic, phase-independent capture; passing runs unaffected |
| `backend/tests/test_retries.py` | The two tests: added parameter `diag`, `diag.track_job(job_id)` and `diag.mark(...)` lines **between** existing statements | Milestone timing + job tracking. No existing line changed (verified by diff, §6) |
| `docker-compose.yml` | `tests` service only: bind mount `./backend/test-artifacts:/app/test-artifacts` | Diagnostics files survive the `--rm` container |
| `.gitignore` | `backend/test-artifacts/` | Diagnostic artefacts are never committed |

Not changed: application code (`backend/notesagent/**`), RabbitMQ configuration, retry logic, transaction
boundaries, worker behaviour, any assertion, wait, timeout or crash scenario.

### 5.2 Commands executed so far
- `docker compose config --quiet` → valid.
- `git check-ignore -v backend/test-artifacts/x.txt` → ignored by `.gitignore:48`.
- `docker compose --profile test build -q`; write probe inside the test container as uid 10001 →
  `/app/test-artifacts` writable.

### 5.3 Verification runs (16:0x–16:25 IST)

| # | Command (abridged) | Result | Duration |
|---|---|---|---|
| 1 | `pytest tests/test_retries.py::test_crash_after_dead_letter_commit_before_ack` | **1 passed**; no diagnostics file | call 2.05 s (setup 0.97 s, teardown 0.12 s) |
| 2 | `pytest tests/test_retries.py::test_lock_unavailable_delays_without_counting` | **1 passed**; no diagnostics file | call 6.17 s (setup 1.07 s, teardown 0.13 s) |
| 3 | 16-test crash set (`-k "crash or duplicate or sigkill or db_failure or lock_unavailable or delay_publish or survives or never_process"` over the 7 crash-test files) | **16 passed, 24 deselected**; 21 output lines; 0 diagnostics files; RabbitMQ `OOMKilled=false restarts=0` | 85.08 s |
| 4 | Intentional-failure self-check: throw-away module mounted read-only into an ephemeral container (`python -m pytest -p tests.conftest /tmp/selfcheck/test_diag_selfcheck.py`) — **not added to the repository** | 5/5 failure cases produced diagnostics (see §6.2) | 5.74 s |
| 5 | Byte comparison of the two tests with all `diag.` lines removed vs. their original text | **identical** for both; assertions 4 = 4 and 3 = 3 | — |
| 6 | mtime scan of `backend/` since session start | only `tests/conftest.py`, `tests/support/diagnostics.py`, `tests/test_retries.py` (+ `docker-compose.yml`, `.gitignore`); **no `backend/notesagent/**`, no `rabbitmq.conf`** | — |
| 7 | Secret scan of the 5 diagnostics files (against `.env` values, raw `ntn_` tokens) | none found | — |

Full outputs were saved outside the repository (scratchpad): `diag_crash_set.txt`, `selfcheck_out.txt`.

## 6. Diagnostic capability now in place

### 6.1 Captured automatically on failure (setup, call or teardown) of a `diag` test
- test id, phase, wall-clock start/collection time, elapsed time;
- milestones with `t+` and Δ durations (`diag.mark`), incl. "failure detected in <phase>";
- per tracked job: status, progress, `attempts`, `infra_attempts`, `retry` state, `error`, `result`,
  timestamps; full event timeline (+offsets, worker, detail incl. retry `event_id`s); outbox rows
  (event ids, destinations, published_at, attempts, last_error); checkpoints;
- RabbitMQ: queues `jobs.*` / `cred.*` (ready/unacked/consumers/policy), node uptime (restart detection),
  memory used/limit, memory and disk alarms, fd/process counts;
- PostgreSQL start time (restart detection), connection count; Redis uptime;
- fixture marker files (run/started/hook) with times relative to test start;
- every started process: pid, return code (−9 = SIGKILL), last 40 log lines;
- pytest's own failure text, verbatim.

Delivered twice: as a section in pytest's failure report and as a file
`backend/test-artifacts/diagnostics/<UTC timestamp>_<test>_<phase>.txt` (git-ignored, survives the
`--rm` container). Passing runs: no extra output and no files.

### 6.2 Self-check evidence (intentional failures, throw-away module)

| Case | pytest outcome | Diagnostics section | File |
|---|---|---|---|
| assertion in test body | FAILED | `diagnostics (call)` | `…_test_intentional_call_assertion_call.txt` (85 lines, all sections populated) |
| helper timeout (`wait_until`) — not covered by the old pattern | FAILED | `diagnostics (call)` | `…_test_intentional_helper_timeout_call.txt` |
| setup error after `diag` | ERROR | `diagnostics (setup)` | `…_setup_error_after_diag_setup.txt` |
| setup error **before** `diag` (fixture order) | ERROR | `diagnostics (setup)` (collector created by the setup hook; infra health still collected) | `…_setup_error_before_diag_setup.txt` |
| teardown error | ERROR (test body passed) | `diagnostics (teardown)` | `…_teardown_error_teardown.txt` |

Original tracebacks remained in pytest's output (12 matches of the intentional messages). These five
self-check files remain in `backend/test-artifacts/diagnostics/` (git-ignored) as evidence; their
names start with the throw-away test names (`test_intentional_*`).

## 7. Behaviour and assertions
- **Assertions unchanged:** proven by comparison (§5.3 #5). Only `diag` parameter, `diag.track_job` and
  `diag.mark` lines were added (9 and 4 lines).
- **Application behaviour unchanged:** no file under `backend/notesagent/` and no RabbitMQ configuration
  modified in this session (§5.3 #6). Waits, timeouts, scenarios and ordering unchanged.
- The collector is read-only (SELECT, management-API GET, Redis INFO, file reads).

## 8. Unexpected findings
- The existing mailbox pattern could not have captured repetition 2's evidence: its error was a
  setup/teardown error and its failures may have been helper timeouts — both outside the old pattern.
- No new failure occurred in this session, so the original cause of repetition 2 is **still unknown**.

## 9. Remaining problems and recommended next steps
1. Cause of crash-test repetition 2 (2 failures + 1 error, 502 s) — unknown; not reproduced in this
   session (individual runs and one crash-set run passed).
2. Recommended (needs approval): extended crash-set repetitions (e.g. 10–20) with the new diagnostics;
   any failure will now leave a complete file in `backend/test-artifacts/diagnostics/`.
3. Optionally extend `diag` to the other 14 crash tests (currently only the two that failed have it).
4. Part 3 is **not** declared complete. Nothing committed.

## 10. Git state at end of session
- Branch `phase-2`; `main` = `phase1-baseline` = `49eed18`; 0 commits since baseline; **0 staged files**.
- This session's changes are unstaged: `backend/tests/conftest.py`, `backend/tests/test_retries.py`,
  `docker-compose.yml`, `.gitignore` (modified); `backend/tests/support/diagnostics.py`,
  `docs/session_logs/` (new). Earlier Part 1–3 work remains uncommitted as before.
