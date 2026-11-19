# Session log — Phase A, Part 1: broker dead-letter handling (Phase 2 Part 4 design)

- **Started:** 2026-11-19 22:34 (host clock)
- **Branch:** `phase-2`; HEAD = `main` = `phase1-baseline` = `49eed18`; 0 staged; `git status` 41 entries
  (uncommitted Phase 2 work, preserved).
- **Objective:** when RabbitMQ itself dead-letters a job message (crash loop → `delivery_limit`, or a
  malformed message → `rejected`), keep the message in `jobs.dlq`, move a stuck job to DEAD_LETTERED
  when appropriate, and make duplicate deliveries and audit-consumer crashes unable to create duplicate
  events or lose the audit message. Approved design: `phase2_part4_final_design_review.md` (D1–D10),
  with the user's refinements (missing message id ≠ stale; worker copies need marker **and** body type).
- **Out of scope:** DLQ CLI, replay, dashboards, alerts, health-depth metrics, new containers, poison
  guard, Phase A Part 2. No commit, no staging, no deployment to the dev stack.

## 1. Inspection before implementation (verified in code, not from earlier reports)
- **Part 3:**
  - `jobs_repo.schedule_retry` / `dead_letter` are single transactions writing an outbox row with a
    destination.
  - The outbox publisher called `publish_to(..., body=payload)` with **no** extra headers.
  - Retry gating uses `message_id == jobs.retry.event_id` (`runner.py`).
- **Topology:** `jobs.work.v2` is quorum with `x-delivery-limit=QUEUE_DELIVERY_LIMIT`, DLX `jobs.dlx`,
  at-least-once + reject-publish. `jobs.dlx` (fanout) → `jobs.dlq` only. Delay queues as in ADR-09.
- **`dead_letter_audit`** (migration 003): outcome CHECK declared inline, so it is unnamed. Unused by
  code.
- **Design:** `phase2_part4_final_design_review.md` D1–D10; broker probe `phase2_part4_broker_probe.md`.
- **Legacy `jobs.work`:**
  - No Phase 2 code declares it, binds it or publishes to it; workers only drain it passively.
  - `scripts/migrate_work_queue.py` removes it after verifying it is unbound, write-blocked, empty and
    without consumers (ADR-09; covered by `test_work_queue_migration.py`).
  - In the dev vhost it still exists with 2 Phase 1 consumers, because the dev stack is not yet
    deployed with Phase 2.
- **Test infrastructure:**
  - The `TABLES` truncation list did not include `dead_letter_audit`.
  - The test image bakes the code in at build time (`notes-agent-backend:test`, separate from the dev
    image), so it is rebuilt before each run.

## 2. Changes and reasons

### Application
| File | Change | Reason |
|---|---|---|
| `backend/notesagent/common/config.py` | `audit_queue_delivery_limit` (optional) + validator (> `queue_delivery_limit + 1`) + `effective_audit_queue_delivery_limit` (default `+11`) | D5: the delivery count is carried over when a message is dead-lettered (probe F1) |
| `backend/notesagent/common/broker.py` | `QUEUE_DLQ_AUDIT`, `QUEUE_DLQ_PARKED`, `HEADER_EVENT_TYPE`, `audit_queue_arguments`, `parked_queue_arguments`; `declare_topology` declares parked, then audit, and binds audit to `jobs.dlx` | D5 topology; at-least-once via default exchange + reject-publish |
| `backend/notesagent/outbox/publisher.py` | adds the header `x-event-type = row.event_type` | D2 explicit origin marker |
| `backend/migrations/004_dead_letter_audit_outcomes.sql` (new) | looks up the unnamed outcome CHECK (must be exactly one), drops it, adds named `dead_letter_audit_outcome_allowed` incl. `stale`, `inconsistent` | D3 / D1 / D9 outcomes |
| `backend/notesagent/worker/dead_letters.py` (new) | pure rules: `parse` (D1/D2/D9), `dedup_key` (D8), `outcome_without_job`, `decide` (D3), `job_error` (D6) | testable without I/O |
| `backend/notesagent/worker/dlq_audit.py` (new) | `DeadLetterAuditor`: consume `jobs.dlq.audit` (prefetch 1); advisory lock; ONE transaction (row lock, decide, CAS transition + event, audit upsert); credential cleanup; ack after commit; busy lock / DB error → pause + `nack(requeue=True)` | core of Part 4 |
| `backend/notesagent/common/jobs_repo.py` | `get_job_for_update`, `upsert_dead_letter_audit` (ON CONFLICT → `seen_count+1`, `last_outcome`) | D8 idempotent recording |
| `backend/notesagent/worker/runner.py` | starts/stops the auditor in `JobWorker.run`; docstring | the auditor runs inside every worker (no new container) |

### Tests and test support
| File | Change |
|---|---|
| `tests/test_unit_dead_letters.py` (new) | 43 unit tests: death parsing incl. the captured expiry→delivery_limit layout, bytes headers, worker copy / origin conflicts, job-id resolution, dedup key, decide table, error shape, audit-limit validation, a source scan asserting `reject(requeue=True)` is never used |
| `tests/test_dead_letter_audit.py` (new) | 17 integration tests (see §3) |
| `tests/test_migration_004.py` (new) | constraint name, allowed/rejected outcomes, 004 applied on a populated 001–003 scratch database, rows untouched, idempotent |
| `tests/test_topology_v2.py` | new `test_dead_letter_audit_and_parking_queues` (exact arguments, bindings, no TTL/length limit) |
| `tests/test_migration_003.py`, `tests/test_migrations.py` | expected migration lists now include `004_…` (the assertions listed every migration) |
| `tests/support/helpers.py` | `dead_letter_audit` added to the per-test truncation list |
| `tests/support/rabbit.py` | `publish_raw(..., job_id)`, `publish_dead_letter`, `broker_death_headers`; ID-less messages go through the management HTTP API |
| `tests/support/run_worker_hooks.py` | `HOOK_AUDIT_CRASH=before\|after`, `HOOK_AUDIT_DB_FAIL=N` |

### Documentation
- `docs/decisions.md`: new ADR-12; ADR-11 note that only `nack(requeue)` is uncounted.
- `docs/api.md`: the DEAD_LETTERED-by-broker error shape.
- `.env.example`: a comment explaining the derived audit limit. Not passed by Compose, so Compose is
  unchanged.

## 3. Tests, commands, results
All runs use `MSYS_NO_PATHCONV=1 docker compose --profile test run --rm --no-deps tests pytest -p no:warnings -q …`
after `docker compose --profile test build tests` (test image only; the dev image and dev stack were
not touched).

| Run | Result |
|---|---|
| `tests/test_unit_dead_letters.py` | **43 passed** in 0.44 s |
| `tests/test_migration_004.py tests/test_migration_003.py tests/test_migrations.py tests/test_topology_v2.py` | **71 passed** in 27.80 s |
| `tests/test_dead_letter_audit.py`, first run (22:43–22:44) | **16 passed, 1 failed**: `test_missing_message_id_never_changes_the_job` — outcome `applied` instead of `inconsistent` (see below) |
| `tests/test_dead_letter_audit.py -k missing_message_id`, after the helper fix | **1 passed** |
| Full suite | _see §4_ |

**The one failure (a test-helper defect, not the auditor):**
- The crafted dead letter published with `message_id=None` arrived **with** a message id.
- Two isolated checks in throw-away scratch scripts (temporary exclusive queues) showed why:
  - `aio_pika.Message(message_id=None)` stays None at construction, but the delivered message carried
    a random id;
  - `aiormq.Channel.basic_publish` sets `properties.message_id = uuid4().hex` when it is empty (its
    source mentions this at lines 27–30, and the property mutated after publish).
- Consequence: no Python producer in this project can publish an ID-less message. The rule only guards
  against foreign publishers.
- Fix: the helper publishes ID-less messages through the management HTTP API (`/api/exchanges/…/publish`),
  and the test now also asserts `message_id IS NULL` in the audit row.
- The auditor code was not changed for this.

Integration coverage (`test_dead_letter_audit.py`):
- **Real crash loop:** 11 SIGKILLed consumers with `QUEUE_DELIVERY_LIMIT=10` → broker dead letter →
  DEAD_LETTERED / `delivery_limit`, audit `applied`, one event, DLQ copy kept, mailbox deleted, results
  API shows the error.
- **Real rejection:** a malformed body with a valid `x-job-id` → `rejected` → applied. Garbage with no
  id → `unreadable`.
- **Real Part 3 worker copy:** ignored; no audit row; the error stays `RETRIES_EXHAUSTED`.
- **Origin marker:** the outbox publisher's `x-event-type` header is present on published messages.
- **Crafted dead letters:**
  - unknown job;
  - completed job not overwritten;
  - stale copy keeps the newer retry, and the current retry id is applied;
  - duplicate → one row with `seen_count 2`, one event;
  - id mismatch;
  - missing message id;
  - inconsistent death headers.
- **Busy advisory lock:** deferred at least twice with `nack`, never parked, applied after release.
- **DB outage:** 2 simulated transaction failures → requeued twice → applied once.
- **Auditor SIGKILL before / after commit:** exactly one event; `seen_count` 1 / 2.
- **Audit crash loop** (limits 2 / 4, declared only by test processes):
  - a real work-queue crash loop delivers the message with `x-delivery-count 3` (carried over);
  - 2 audit-consumer kills → **parked**, not lost;
  - `x-death[0]` = audit `delivery_limit`, `x-death[1]` = `jobs.work.v2` `delivery_limit`;
  - message id and job id preserved.

## 4. Full suite and runtime verification
- **Full suite** (rebuilt test image, 22:46:18–22:56:39): **628 passed in 617.65 s**, 0 failed, 0 errors.
  The previous baseline was 555; the 73 added tests are new tests only.
  - Includes the legacy-queue removal tests (`test_work_queue_migration.py`): deletion only after the
    queue is unbound, write-blocked, empty and has no consumers; refusal while a consumer is attached.
  - Includes all Part 3 retry and crash tests, unchanged and passing with the auditor running in every
    worker.
- **Infrastructure afterwards:** RabbitMQ, PostgreSQL and Redis healthy, 0 restarts, no RabbitMQ
  alarms. No new diagnostics files (still the 5 intentional ones).
- **Dev stack untouched:** vhost `/` still has `jobs.work` (0 messages, 2 Phase 1 consumers) and
  `jobs.dlq`. No dev image was built and nothing was deployed.

## 5. Remaining risks and unresolved issues
1. **Not deployed (D10 open):** Phase 2, including the auditor, runs only in the test vhost. The dev
   stack still runs Phase 1 images and the legacy `jobs.work` with 2 Phase 1 consumers. Removing it means
   deploying Phase 2 (stop Phase 1 workers → start Phase 2 services → `scripts/migrate_work_queue.py`).
   This needs your explicit deployment approval and was not done.
2. **Graceful restarts count:** a worker stopped while it holds an audit message returns it by closing
   the channel. Like a crash, this probably increments the delivery count [NV for a graceful close].
   The margin (10 crashes) and the parking queue cover it.
3. **ID-less messages:** a message without `message_id` can only come from a non-Python or foreign
   publisher (aiormq always sets one). Such dead letters are recorded `inconsistent` and the job stays
   unchanged, by design. That job would need manual inspection.
4. **Unchanged jobs need manual follow-up:** jobs whose dead letters are `inconsistent`, `stale` or
   parked are not changed; they are visible only in `dead_letter_audit` / the parked queue. There is no
   inspection CLI or replay (out of scope, Q5).
5. **Parked queue and `jobs.dlq` grow without bound** (no retention policy, by design).
6. **`x-event-type` marker only on new messages:** messages published before this change carry no
   marker. A pre-change worker copy would be recorded `inconsistent`; harmless, since the job is
   already DEAD_LETTERED.
7. **Single-node broker:** behaviour across broker restarts or leader changes was not tested.

## 6. Final Git status
- HEAD = `main` = `phase1-baseline` = `49eed18`; **0 staged**; nothing committed.
- `git status`: 49 entries (was 41).
  - New untracked files: `migrations/004_dead_letter_audit_outcomes.sql`, `worker/dead_letters.py`,
    `worker/dlq_audit.py`, `tests/test_dead_letter_audit.py`, `tests/test_migration_004.py`,
    `tests/test_unit_dead_letters.py`, this log.
  - Newly modified tracked files: `tests/support/helpers.py`, `tests/support/rabbit.py`. All other
    edited files were already modified or untracked Phase 2 work.
- Scratch scripts `mid_check.py` and `mid_check2.py` are in the session scratchpad (outside the repo).
