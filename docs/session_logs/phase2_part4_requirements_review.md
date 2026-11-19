# Session log — Phase 2, Part 4: requirements and design review (read-only)

- **Date:** 2026-11-19
- **Branch:** `phase-2`; `main` = `phase1-baseline` = `49eed18`; no commits; nothing staged
- **Status:** Part 3 formally approved as complete by the user (before this session).
- **Nature:** read-only investigation and planning. No code, tests, configuration or migrations changed;
  nothing deployed, staged or committed. Part 4 is **not** started.
- Earlier logs (unchanged): `phase2_part3_crash_diagnostics.md`, `phase2_part3_crash_reliability_10_runs.md`,
  `phase2_part3_final_audit.md`, `phase2_part3_final_cleanup.md`.

## 1. Documents and files inspected
- `PROJECT_SPEC.pdf` (via `pdftotext`): §1 broker "at-least-once delivery, redelivery on crash"; §4 state
  table (DEAD_LETTERED = "Retry budget exhausted; routed to the DLQ for inspection"; "any non-terminal →
  FAILED / DEAD_LETTERED"); §6 worker crashes, dead-letter queue ("routed to a DLQ for manual inspection
  instead of retried forever"; bounded, configurable, demonstrable); §8 README retry parameters.
- The approved Phase 2 plan and decisions in the earlier session transcript: plan §4 "DLQ and
  DEAD_LETTERED state", decisions Q1–Q6, accepted corrections D (idempotent audit consumer) and E, and
  the delivery-plan table (Part 4 = "At-least-once dead-lettering and the idempotent audit consumer";
  Part 6 = "DLQ inspection CLI, documentation, full suite, demo-profile run").
- `docs/decisions.md`: ADR-02 (outbox), ADR-03 (credential mailbox), ADR-05 (worker reliability),
  ADR-09 (topology v2), ADR-10 (classification), ADR-11 (retries).
- `docs/api.md` (DEAD_LETTERED example, error rules).
- `backend/migrations/001_init.sql` (status trigger, allowed transitions), `003_retry_dlq_foundation.sql`
  (`dead_letter_audit`).
- `common/broker.py` (topology, queue arguments, `publish_to` headers, `republish`), `common/states.py`,
  `common/config.py`, `common/jobs_repo.py`, `worker/runner.py` (consumers, malformed-message reject,
  terminal path, legacy drain), `outbox/publisher.py`.
- Tests: `test_results_and_dlq.py`, `test_worker.py::test_malformed_message_is_dead_lettered`,
  `test_retries.py`, `test_topology_v2.py`, `test_migration_003.py`, `tests/support/*`.
- Session logs: `phase2_part3_final_audit.md` (§5 residual risks), `phase2_part3_final_cleanup.md`.

## 2. Requirements discovered (source in brackets; M = mandatory, O = optional)

| # | Requirement | Source | M/O |
|---|---|---|---|
| R1 | A job whose message the **broker** dead-letters (crash loop → `delivery_limit`; malformed → `rejected`) must end in DEAD_LETTERED in PostgreSQL — not stay non-terminal forever | Spec §4, §6 ("no job is silently lost"); Phase 2 plan §4.2; Part 3 audit risk 1 | M |
| R2 | Dead-lettering is at-least-once (no loss between work queue and DLX) | Spec §1; plan §4.3; ADR-09 | M — **already done in Part 1** (`jobs.work.v2`: at-least-once + reject-publish) |
| R3 | A DLQ audit consumer runs inside every worker process (no new container) and consumes `jobs.dlq.audit`, a second queue bound to the `jobs.dlx` fanout | plan §4.2 | M (approved design) |
| R4 | `jobs.dlq` is never consumed; it stays for manual inspection; no replay | Spec §6; Q5; plan §4.2 | M |
| R5 | Error `code = DEAD_LETTERED_BY_BROKER` with the broker's reason; credential mailbox deleted | plan §4.2 | M |
| R6 | Idempotent: the same dead letter twice → recorded once (`dead_letter_audit.dedup_key` = message id + death reason; `seen_count`) | correction D; migration 003 | M |
| R7 | Never overwrite a terminal state (CAS from non-terminal only; DB trigger as backstop) | correction D; 001 trigger | M |
| R8 | Unreadable / unknown job id → record outcome `unreadable` / `unknown_job`, ack on the audit queue only; the copy stays in `jobs.dlq` | correction D | M |
| R9 | Respect the advisory lock: if a live worker holds the job, defer instead of dead-lettering a running job | correction D | M |
| R10 | Crash after the DB update, before the ack → redelivered → `already_terminal` → ack | correction D | M |
| R11 | Malformed message: DEAD_LETTERED via the audit consumer **when its job id can be read** | plan test list item 5 | M |
| R12 | Audit consumer under duplicates and crashes is tested | plan test list item 9 | M |
| R13 | Crash loop reaching the delivery limit ends in DEAD_LETTERED (end-to-end test) | plan test list item 4 | M |
| R14 | No message bodies stored in the audit table; no secrets logged | migration 003 comment; ADR-02, ADR-03 | M |
| R15 | `docs/api.md` + ADR describe the new error shape | process (ADR per part) | M |
| R16 | DLQ inspection CLI `scripts/dlq.py list|show` | plan §4 / delivery plan | **Part 6**, not Part 4 |
| R17 | Alerts/metrics on DLQ depth, `jobs.dlq` length limits, `/healthz` exposure | — (not in spec or plan) | O |

## 3. Existing implementation reused
- **Topology (Part 1):** `jobs.dlx` fanout, `jobs.dlq` (quorum), `jobs.work.v2` with `x-delivery-limit`
  (`QUEUE_DELIVERY_LIMIT=10`), DLX `jobs.dlx`, at-least-once + reject-publish. Job messages carry no
  per-message TTL, so the work queue never dead-letters with `expired`. Credential queues have no DLX,
  so expired tokens are never dead-lettered (important: they must never reach the audit path).
- **Table (Part 1):** `dead_letter_audit` (dedup_key UNIQUE, message_id, job_id without FK, death_reason
  ∈ {delivery_limit, rejected, expired, maxlen, unknown}, outcome ∈ {applied, already_terminal,
  unknown_job, unreadable}, seen_count, first/last_seen_at). No body column.
- **State machine (Phase 1):** every non-terminal state → DEAD_LETTERED allowed (`states.py`, 001
  trigger); terminal rows immutable.
- **Worker (Part 3):** advisory lock per job, `jobs_repo.transition` (CAS, clears `retry`), message
  identity (`retry.event_id` = AMQP `message_id`), malformed → `reject(requeue=False)`, worker-initiated
  dead letter via the outbox → `jobs.dlx`/`dead` (body `event_type = job.dead_lettered`), terminal-path
  credential cleanup, throttled `nack(requeue)` fallback (P3-D4).
- **Measured fact (Phase 1):** a consumer crash increments `x-delivery-count`; explicit
  `nack(requeue)` does not (`test_results_and_dlq.py`).
- **Test helpers:** `hold_message.py` (consume without ack → SIGKILL), `run_worker_hooks.py`,
  `rabbit.py` (`peek_messages`, `queue_state`), `diagnostics.py`.

## 4. Gap (what does not exist yet)
- No `jobs.dlq.audit` queue; nothing consumes dead letters; no code writes `dead_letter_audit`.
- A crash-looping job is today left non-terminal (e.g. TRANSCRIBING, possibly with `retry` set) while its
  message sits in `jobs.dlq` — `GET /jobs/{id}` would show it as running forever. Spec §6 violation.
- No end-to-end crash-loop test (only the measured-fact test for one crash).

## 5. Proposed architecture
```
jobs.work.v2 ──(delivery_limit / rejected, at-least-once)──▶ jobs.dlx (fanout) ──▶ jobs.dlq         (inspection only)
outbox job.dead_lettered (worker-initiated, Part 3) ───────▶ jobs.dlx           └─▶ jobs.dlq.audit  (NEW, quorum)
                                                                                       │ consumer in every worker
                                                                                       ▼ process (own channel, prefetch 1)
                                                         PostgreSQL: one transaction = CAS → DEAD_LETTERED
                                                         + job_events + dead_letter_audit (upsert) → commit
                                                         → delete cred.<job_id> → ack
```
Per audit message:
1. Parse: death info (reason, source queue) from the broker's headers; job id from the body, else the
   `x-job-id` header; message id. Worker-initiated copies (body `job.dead_lettered`, no death headers)
   are recognised (decision P4-D2).
2. Unreadable → upsert audit row `unreadable` → ack. Unknown job → `unknown_job` → ack.
3. `pg_try_advisory_lock(job)`: busy → defer (P4-D4).
4. Locked: read job. Terminal → upsert `already_terminal`, idempotent credential cleanup, ack.
   Retry scheduled and `message_id ≠ retry.event_id` → stale copy, job stays alive (P4-D3).
   Otherwise one transaction: CAS transition → DEAD_LETTERED with
   `error = {code: DEAD_LETTERED_BY_BROKER, message, reason}` + event `dead_lettered_by_broker` +
   audit upsert `applied`. Then delete the mailbox, then ack.
5. PostgreSQL unavailable → nothing recorded, throttled `nack(requeue)` (not counted), like P3-D4.

**No outbox row:** the broker has already put the copy into `jobs.dlq`; Part 4 only reconciles PostgreSQL.

## 6. Duplicates, idempotency and crash consistency
| Case | Handling |
|---|---|
| Same dead letter delivered twice (at-least-once DL, audit redelivery) | dedup_key upsert → one row, `seen_count` += 1; second CAS finds terminal → `already_terminal` |
| Worker-initiated copy (Part 3) also reaches `jobs.dlq.audit` | job already DEAD_LETTERED → no state change (P4-D2: record or skip) |
| Crash before the audit transaction commits | nothing committed; redelivery → applied once |
| Crash after commit, before mailbox delete / ack | redelivery → `already_terminal` → cleanup again (idempotent) → ack |
| Job completed meanwhile by another copy | terminal → never overwritten (CAS + trigger) |
| Live worker holds the lock | deferred (no dead-lettering of a running job) |
| Stale copy reached the limit while a newer retry is scheduled | not applied (P4-D3) |
| Audit message itself crash-loops the audit consumer | audit queue's own delivery limit, no DLX (P4-D5); the copy remains in `jobs.dlq` |
| Two workers' audit consumers get duplicates concurrently | advisory lock + CAS + unique dedup_key |

## 7. Decisions
**Already approved (not re-opened):** Q5 (inspection only, no replay); Q6 / ADR-09 (v2 queue,
at-least-once DL); plan §4.2 (audit consumer inside every worker, `jobs.dlq.audit` bound to the fanout,
`DEAD_LETTERED_BY_BROKER`, mailbox deleted, `jobs.dlq` never consumed); correction D (audit table and
outcomes, dedup key = message id + death reason, record-and-ack unreadable/unknown, CAS no overwrite,
advisory-lock deferral, crash-after-commit → already_terminal); CLI in Part 6.

**Need approval:**
- **P4-D1 — death reason source (correction to the approved plan).** The plan says
  `x-first-death-reason`. A retried job's message has already died once with `expired` in a delay queue
  (`jobs.retry.<N>s` → `jobs`), so its *first* death reason is `expired`, not `delivery_limit`.
  Proposal: use the most recent death (`x-last-death-reason` / `x-last-death-queue`, or the newest
  `x-death` entry) and require the source queue to be `jobs.work.v2` or legacy `jobs.work`. The exact
  header set delivered to AMQP 0-9-1 consumers by RabbitMQ 4.4.1 must be **measured first** [NV].
- **P4-D2 — worker-initiated copies on the audit queue** (fanout cannot filter). (a) recognise them and
  ack without writing an audit row (recommended: the table records broker deaths), or (b) record them as
  `already_terminal` with reason `unknown`.
- **P4-D3 — stale dead letters.** If `retry` is set and `message_id ≠ retry.event_id`, do not
  dead-letter. Needs an outcome value: migration **004** adding `stale` to the outcome CHECK
  (recommended), or reuse `already_terminal` (inaccurate).
- **P4-D4 — lock-busy deferral.** (a) sleep `LOCK_RETRY_DELAY_SEC`, then `nack(requeue)` — explicit
  nacks are not counted, no new topology (recommended); (b) a new delay queue that dead-letters back to
  the audit queue (more topology).
- **P4-D5 — audit queue arguments.** Quorum, durable, `x-delivery-limit = QUEUE_DELIVERY_LIMIT`, **no
  DLX** (a DLX to `jobs.dlx` would loop through the fanout). A poison audit message is eventually dropped
  from the audit queue only; the inspection copy stays in `jobs.dlq`.
- **P4-D6 — error shape.** `{code: "DEAD_LETTERED_BY_BROKER", message, reason: "delivery_limit" |
  "rejected"}` (+ `docs/api.md`). Message text user-facing, no internals.
- **P4-D7 — PostgreSQL unavailable in the audit consumer.** Throttled `nack(requeue)`, nothing counted
  (mirrors P3-D4).
- **P4-D8 — dedup key without message id.** Use `sha256(body)` + reason (the hash, not the body, is
  stored).
- **P4-D9 — job id from the `x-job-id` header** when the body is malformed (R11). Our own publisher sets
  it; anyone able to publish to the broker is already trusted.
- **P4-D10 — legacy `jobs.work`.** It dead-letters at-most-once (Phase 1 arguments). Accept as residual
  until the dev-stack migration removes it.

## 8. Potential risks
1. RabbitMQ 4.x death-header semantics differ from expectations (P4-D1) → measure first; stop if they do.
2. A job dead-lettered by the broker while a delayed copy is still in a delay queue: that copy later
   arrives, finds the job terminal and is dropped (existing logic) — expected, to be tested.
3. Audit backlog when no worker runs: dead letters accumulate in `jobs.dlq.audit` (durable) and are
   reconciled on start.
4. Unbounded `jobs.dlq` growth (inspection only, no limits) — optional limit/alert later.
5. Test time: a real crash loop needs 10 SIGKILLs (`QUEUE_DELIVERY_LIMIT=10`, fixed queue argument).
6. Dev stack still on Phase 1 images and without the v2 migration; Part 4 adds one more queue to deploy.
7. Not fixed by Part 4: P3-D4 unbounded uncounted delays during long DB outages (approved), Phase 5
   Notion write reconciliation.

## 9. Recommended implementation sequence (after approval)
1. **Broker probe (measured-fact tests)** on 4.4.1: headers of a `delivery_limit` and a `rejected` dead
   letter, including a message that previously expired in a delay queue. Stop and report if the result
   contradicts P4-D1.
2. Pure module `worker/dead_letters.py` (parse death info, job id, message id, dedup key, worker-copy
   detection) + unit tests.
3. Migration 004 only if P4-D3 approves `stale` (+ migration test).
4. Topology: declare and bind `jobs.dlq.audit` (P4-D5) + topology test.
5. `jobs_repo`: `upsert_dead_letter_audit`, `apply_broker_dead_letter` (one transaction).
6. Audit consumer in the worker process (own channel, prefetch 1, graceful shutdown) + crash hooks.
7. Integration and crash tests (§10); ADR-12, `docs/api.md`, `.env.example` (if any new setting).
8. Full suite once; report; stop.

## 10. Tests
**Unit (mandatory):** death-info parsing (last vs first death, missing headers, source-queue check),
job-id extraction (body, header fallback, garbage), dedup key (with/without message id), worker-copy
detection, error document.

**Integration (mandatory):**
- crash loop (10 SIGKILLs) → DEAD_LETTERED `DEAD_LETTERED_BY_BROKER`/`delivery_limit`, mailbox deleted,
  audit row `applied`, copy still in `jobs.dlq`;
- crash loop after a retry hop → reason `delivery_limit` (not `expired`);
- malformed body with readable `x-job-id` → DEAD_LETTERED `rejected`; garbage without id → `unreadable`;
- unknown job id → `unknown_job`; completed job + late dead letter → unchanged;
- worker-initiated copy (Part 3 exhaustion) → error stays `RETRIES_EXHAUSTED`;
- duplicate dead letter (same message twice) → one row, `seen_count = 2`, one event;
- stale copy while a newer retry is scheduled → job not dead-lettered;
- live worker holds the lock → deferred, then resolved correctly;
- PostgreSQL unavailable → audit message kept, applied after recovery;
- expired credential queues never reach the audit path.

**Crash (mandatory):** SIGKILL the audit consumer (a) before the transaction commits, (b) after the
commit and before the ack → in both cases exactly one `dead_lettered_by_broker` event and correct final
state.

**Regression:** full suite once.

**Optional:** DLQ depth in `/healthz`, `jobs.dlq` length limit, `diag` on the new crash tests.

## 11. Dependencies on Parts 1–3
- Part 1: v2 topology, `jobs.dlx`/`jobs.dlq`, migration 003 table and outbox destinations.
- Part 2: error codes / classification (the audit consumer does not classify, but the error vocabulary
  must stay consistent).
- Part 3: message identity (`retry.event_id`) for the stale rule; worker-initiated dead letters reaching
  the fanout; advisory-lock and P3-D4 patterns reused.

## 12. Git and scope at end of session
Only file written: this log (untracked). No code, tests, configuration or migrations changed; nothing
deployed, staged or committed. Part 4 not started — awaiting approval.
