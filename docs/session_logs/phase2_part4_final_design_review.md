# Session log — Phase 2, Part 4: final requirements and design revision (read-only)

- **Date:** 2026-11-19
- **Branch:** `phase-2`; HEAD = `main` = `phase1-baseline` = `49eed18`; 0 staged; `git status` 41 entries
- **Nature:** design revision plus one isolated broker verification in a temporary vhost. No application
  code, tests, migrations, Compose, RabbitMQ configuration or project topology changed; nothing deployed,
  staged or committed. This log is the only project file written. Part 4 **not** started.
- Earlier logs (unchanged): `phase2_part4_requirements_review.md`, `phase2_part4_broker_probe.md`, and the
  Part 3 logs.

Evidence labels: **[RUN]** observed on the running RabbitMQ 4.4.1 · **[SRC]** verified in source ·
**[DOC]** documentation only · **[NV]** not verified.

## 1. Files and ADRs inspected
- `PROJECT_SPEC.pdf` §1, §4, §6; the approved Phase 2 plan (§4, Q1–Q6, corrections D/E, delivery table).
- `docs/decisions.md` ADR-02, ADR-03, ADR-05, ADR-09, ADR-10, ADR-11; `docs/api.md`.
- `outbox/publisher.py` (lines 54–87): `publish_to(row.destination_exchange, row.destination_routing_key,
  event_id=row.event_id, job_id=row.job_id, body=json(payload))`, with **no extra headers**.
- `common/broker.py`:
  - `publish_to` sets `message_id = event_id`, `x-job-id` and `content_type`.
  - `work_queue_arguments`, `legacy_work_queue_arguments`, `delay_queue_arguments`, `declare_topology`.
- `common/jobs_repo.py`: outbox payloads `job.submitted` (gateway `submission.py:95`), `job.retry`
  (line 125), `job.dead_lettered` (line 141).
- `worker/runner.py`: legacy drain (`_legacy_queue_exists`, `_drain_legacy_queue`), malformed reject,
  `nack` fallback.
- `ops/work_queue_migration.py`, `tests/support/legacy.py`.
- Migrations 001 (status trigger) and 003 (`dead_letter_audit`: outcome CHECK inline, i.e. unnamed).
- The broker probe log `phase2_part4_broker_probe.md` (experiments A–E).

## 2. Broker evidence used (from the earlier probe, [RUN])
- `x-death` is ordered newest first.
- `x-first-death-reason` can be `expired` while the final work-queue death is `delivery_limit`;
  `x-last-death-reason` / `x-last-death-queue` give the final death.
- `nack(requeue=True)` is not counted; `reject(requeue=True)` is counted.
- Consumer crashes are counted.
- With a finite limit and no DLX, an exhausted message is discarded.
- With a finite limit and a parking DLX, the message was kept (E2).
- Default delivery limit 20; `-1` disables it.
- `message_id`, `x-job-id` and custom headers survive dead-lettering.

## 3. Additional isolated verification (this session)

### 3.1 Environment and configuration checks
- Feature flags [RUN]: `stream_queue` **enabled**, `quorum_queue` enabled, `message_containers` and
  `message_containers_deaths_v2` enabled.
- Broker environment [RUN]: `dead_letter_worker_consumer_prefetch = 32`,
  `dead_letter_worker_publisher_confirm_timeout = 180000` ms.
- [DOC] Quorum queues dead-letter **at-most-once** by default. At-least-once needs
  `x-dead-letter-strategy = at-least-once` **and** `x-overflow = reject-publish` (otherwise the broker
  falls back to at-most-once), plus the `stream_queue` feature flag. F3 below confirms the fallback at
  runtime.

### 3.2 Probe F (temporary vhost `p4probe2-1795088176`, 17:06:17–17:13:00 IST)
- Mirror topology:
  - `px.jobs` → `px.work` (quorum, limit 3, DLX `px.dlx`, at-least-once, reject-publish);
  - `px.retry` → `px.delay.2s` (TTL 2 s, at-least-once back to `px.jobs`);
  - `px.dlx` fanout → `px.dlq` and `px.audit`;
  - `px.audit`: quorum, limit 3, `x-dead-letter-exchange=""`, `x-dead-letter-routing-key=px.audit.parked`,
    at-least-once, reject-publish;
  - `px.audit.parked`: quorum, `x-delivery-limit=-1`.
- Consumer crashes are real child processes killed with SIGKILL.
- The vhost was deleted afterwards. Project vhosts `/` and `notes_test` were unchanged (verified with
  `list_vhosts` and `list_queues`); RabbitMQ healthy, 0 restarts, no alarms.
- Script `broker_probe_f.py` and output `probe_output_f.jsonl` are in the scratchpad (outside the repo).

**F1 — real two-stage chain** (delay expiry → work queue crash loop → fanout → audit queue crash loop
→ parked) [RUN]:
- The work queue was delivered 4 times (counts absent, 1, 2, 3), then dead-lettered. Afterwards `px.dlq`
  held 1 message and `px.audit` held 1.
- **The audit copy arrived with `x-delivery-count = 4`, carried over from the work queue.** The audit
  queue dead-lettered it after **one** crash. The delivery count is not reset by dead-lettering.
- Afterwards: `px.audit` 0, `px.audit.parked` **1**, `px.dlq` 1 — exactly one parked copy.
- The parked copy kept `message_id = F1` and `x-job-id`. Its headers:
  - `x-death` (newest first): [delivery_limit @ px.audit (exchange px.dlx, 11:36:23); delivery_limit @
    px.work (px.jobs, 11:36:22); expired @ px.delay.2s (px.retry, 11:36:20)]
  - `x-first-death-*`: expired / px.delay.2s
  - `x-last-death-*`: delivery_limit / **px.audit**
  - `x-delivery-count`: 5; `x-acquired-count`: 5
  - So the original work-queue death is preserved as `x-death[1]`.
- Inspecting the parked message with get + `nack(requeue)` kept it (parked count still 1).

**F2–F4 — parking target missing at the moment of death, created 15 s later** [RUN]:

| Variant | Audit-queue arguments | Result after 300 s |
|---|---|---|
| F2 | at-least-once + reject-publish + DLX `""` / routing key | **delivered**: arrived 136.5 s after the parking queue was created (≈ 181 s after the death, matching the 180 s confirm timeout); exactly 1 copy; `message_id` and `x-death` intact |
| F3 | at-least-once **without** reject-publish | **lost**: parked 0, source 0 |
| F4 | default (at-most-once) | **lost**: parked 0, source 0 |

Duplicates: no duplicates were observed (exactly one copy in every case). At-least-once **may**
duplicate on a lost confirm [DOC]; consumers must therefore be idempotent [NV for the duplicate case
itself].

## 4. Final decision table

| ID | Final wording | Status | Evidence | Implementation consequence | Required tests |
|---|---|---|---|---|---|
| **P4-D1** | Final death = `x-death[0]`. Valid only if its `queue ∈ {jobs.work.v2, jobs.work}`, its `reason ∈ {delivery_limit, rejected}`, and it equals `x-last-death-queue` / `x-last-death-reason`. `x-first-death-*` is never used. Missing or inconsistent → outcome `inconsistent`, `death_reason = unknown`, **no job transition**. | Proposed, ready for approval | [RUN] A3/A4/F1 | Pure parser `dead_letters.parse_death(headers)` | Unit: rejected, delivery_limit, expiry-then-limit (first ≠ last), missing headers, mismatch between `x-death[0]` and `x-last-*`, wrong queue, wrong reason |
| **P4-D2** | Worker-initiated copy = no `x-death` **and** body `event_type = job.dead_lettered`: ack, no audit row, no state change (job is already DEAD_LETTERED). Any other message without `x-death` → `inconsistent`. **Recommended marker:** the outbox publisher adds header `x-event-type = <outbox.event_type>` to every publish (headers survive dead-lettering [RUN]). Worker copies then carry `x-event-type=job.dead_lettered`, while broker dead letters carry `job.submitted`/`job.retry`. No migration needed. | Conditional (the marker needs your approval) | [SRC] publisher.py 68–71, jobs_repo.py 141; [RUN] header preservation | Small change to `publisher.py` / `publish_to` call (not made) | Unit: classification table; integration: Part 3 exhaustion → `RETRIES_EXHAUSTED` unchanged, no audit row |
| **P4-D3** | Stale = job non-terminal **and** `jobs.retry` is set **and** `message_id ≠ retry.event_id` (Part 3 identity). Result: outcome `stale`; no transition; the newer retry is untouched. If `retry` is NULL, any message for the non-terminal job is treated as current (Part 3 gating would process it). Terminal jobs → `already_terminal`. **Migration 004:** replace the outcome CHECK of `dead_letter_audit` with `outcome IN ('applied','already_terminal','unknown_job','unreadable','stale','inconsistent')`. The 003 CHECK is inline/unnamed, so 004 must look up its generated name, drop it and add a named constraint; existing rows still satisfy it. | Proposed, ready for approval | [SRC] runner.py 290–297, 003 SQL | Migration 004 + migration test; decision function `classify_job_state` | Integration: stale copy while a newer retry is scheduled → job unchanged, retry intact; matching `event_id` → applied; migration test for old/new values |
| **P4-D4** | Lock busy → do nothing in the DB; sleep `LOCK_RETRY_DELAY_SEC`; **`nack(requeue=True)`**. `reject(requeue=True)` is forbidden. Not counted [RUN]. | Proposed, ready for approval | [RUN] B1/B2/D1 vs B3 | Shared helper `requeue_later(message)`; lint/grep test that `reject(requeue=True)` never appears | Integration: lock held by a live worker → deferred repeatedly, never parked, resolved after release |
| **P4-D5** | `jobs.dlq.audit`: quorum, durable, bound to `jobs.dlx`, `x-delivery-limit = AUDIT_QUEUE_DELIVERY_LIMIT`, `x-dead-letter-exchange = ""`, `x-dead-letter-routing-key = jobs.dlq.audit.parked`, `x-dead-letter-strategy = at-least-once`, `x-overflow = reject-publish`. `jobs.dlq.audit.parked`: quorum, durable, `x-delivery-limit = -1`, no TTL, no max-length, no DLX, not bound to any exchange. **New constraint from F1:** the delivery count carries over, so `AUDIT_QUEUE_DELIVERY_LIMIT` must be **> QUEUE_DELIVERY_LIMIT + 1** plus the desired audit crash budget. Proposed: `QUEUE_DELIVERY_LIMIT + 11` (= 21 by default), enforced by config validation. | Proposed, ready for approval | [RUN] E2, F1, F2, F3/F4, D3/D5; [DOC] reject-publish requirement | `declare_topology` additions; config field + validator; identical arguments in every service | Topology test (exact arguments, binding, parked unbound); integration: broker copy with carried count survives ≥ 10 audit crashes and then parks; parked copy has `x-death[1]` = work-queue death |
| **P4-D6** | Applied transition only: `error = {code: "DEAD_LETTERED_BY_BROKER", message: "<user-facing text>", reason: "delivery_limit" \| "rejected"}`. Unknown or inconsistent metadata **never** changes the job; it is visible only in `dead_letter_audit` (`outcome = inconsistent`, `death_reason = unknown`, `detail.kind`) and in a warning log. | Proposed, ready for approval | approved plan §4.2 | `docs/api.md` DEAD_LETTERED example; ADR-12 | API test: shape, no internals; integration: inconsistent → job unchanged |
| **P4-D7** | PostgreSQL unavailable (pool/connection/transaction error) → nothing recorded; sleep `LOCK_RETRY_DELAY_SEC`; `nack(requeue=True)`. Crashes during an outage are counted, but the parking queue protects against loss. | Proposed, ready for approval | [RUN] D2, F1/F2 | Same helper as D4 | Integration (hook): DB failure → message kept, applied after recovery |
| **P4-D8** | `dedup_key = "<message_id>:<reason>"`, or `"sha256:<hex(body)>:<reason>"` when `message_id` is absent; only the hash is stored. Upsert `ON CONFLICT (dedup_key) DO UPDATE SET seen_count = seen_count + 1, last_seen_at = now()`. Events and transitions are tied to the CAS transition, which can succeed only once (terminal rows are immutable), so duplicates can never create a second event. | Approved earlier (correction D); detail proposed | 003 schema; 001 trigger | `upsert_dead_letter_audit` returns `inserted` | Integration: same dead letter twice → 1 row `seen_count = 2`, 1 event |
| **P4-D9** | Job id: body `job_id` and header `x-job-id`, each valid only if it parses as a UUID. Both valid and equal → use it. Exactly one valid → use it (header fallback for malformed bodies; headers are set only by our publisher, which requires broker credentials). Both valid but **different** → `inconsistent` (`detail.kind = job_id_mismatch`), no transition. None valid → `unreadable`. | Proposed, ready for approval | [SRC] broker.publish_to; [RUN] header preservation | Parser `extract_job_id(body, headers)` | Unit: all combinations; integration: malformed body + valid header → DEAD_LETTERED `rejected`; mismatch → unchanged |
| **P4-D10** | `jobs.work` is **not** accepted as a residual. Evidence: Phase 2 code never declares it, publishes to it or binds it [SRC]; workers only drain it passively; it exists only in the dev vhost `/` with the Phase 1 binding `jobs → jobs.work` and 2 Phase 1 consumers, 0 messages [RUN]; it is absent in `notes_test` except where migration tests create it. **Recommendation:** run `scripts/migrate_work_queue.py` (ADR-09, already built and tested) as a mandatory step of the Part 4 dev deployment; Part 4 is not closed until `jobs.work` is verified deleted. Fallback if deployment is postponed: a documented exception, owner = the assistant under your approval, deadline = before Part 4 is closed. | Proposed, ready for approval | [SRC] grep; [RUN] `/` topology | Deployment step, no new code | Existing migration tests; deployment verification checklist |

## 5. Transactions and recovery
Per audit message (one channel, prefetch 1, manual ack):
1. **Parse** (no I/O): death (D1), origin (D2), job id (D9), dedup key (D8).
   - Worker copy → ack.
   - Unreadable / inconsistent → **T-a**: one transaction upserting the audit row → commit → ack.
2. **Lock:** `pg_try_advisory_lock(job)` on a pooled connection. Busy → sleep → `nack(requeue)` (D4).
3. **T-b (one transaction):** `SELECT … FOR UPDATE` on the job, then:
   - Unknown job → upsert `unknown_job`.
   - Terminal → upsert `already_terminal`.
   - Stale → upsert `stale`.
   - Otherwise: CAS transition → DEAD_LETTERED (error per D6, clears `retry`, writes the
     `dead_lettered_by_broker` event) + upsert `applied`.

   Commit.
4. After the commit: delete `cred.<job_id>`. This is idempotent; a failure is logged and tolerated,
   because `x-expires` removes the mailbox anyway.
5. **Ack.** Release the lock in `finally`.

Failure handling:
- Any DB error in steps 2–3 → sleep → `nack(requeue)` (D7).
- Crash before the commit → redelivered and processed normally.
- Crash after the commit but before the ack → redelivered → `already_terminal` with `seen_count + 1`;
  the credential is deleted again; no second event.
- Crash loop on an audit message → after `AUDIT_QUEUE_DELIVERY_LIMIT` it goes at-least-once to
  `jobs.dlq.audit.parked` (F1/F2).

**Parked messages:**
- Inspected later by the Part 6 read-only CLI using get + `nack(requeue)` (never ack, never
  `reject(requeue)`).
- **Replay is out of scope (Q5: inspection only).** If replay is ever approved, the safe procedure
  would be: confirmed republish of a copy with stripped death headers and the same `message_id` to
  `jobs.dlq.audit`, then ack the parked original. This is idempotent through D8 and the CAS.

## 6. Final architecture
```
jobs (direct) ──job.work──▶ jobs.work.v2 ──(delivery_limit / rejected; at-least-once)──┐
jobs.retry ──<N>s──▶ jobs.retry.<N>s ──(expired; at-least-once)──▶ jobs/job.work        │
outbox job.dead_lettered (Part 3) ─────────────────────────────────────────────────────▶ jobs.dlx (fanout)
                                                     ┌───────────────────────────────────┤
                                                     ▼                                   ▼
                                      jobs.dlq (inspection only)          jobs.dlq.audit (consumed)
                                                                                         │ delivery limit
                                                                                         ▼ at-least-once via ""
                                                                          jobs.dlq.audit.parked (inspection only)
```
- **Consumer lifecycle:** `JobWorker.run` starts the audit consumer as a second task on its own
  channel (prefetch 1) after `declare_topology`. On stop, cancel the consumer and finish or `nack` the
  in-flight message. Reconnects with the robust connection. Every worker runs one, competing
  consumers; correctness comes from the advisory lock, CAS and the dedup key.
- **Schema:** migration 004 (outcome CHECK only). No new tables or columns.
- **Configuration:** `AUDIT_QUEUE_DELIVERY_LIMIT` (validated > `QUEUE_DELIVERY_LIMIT + 1`), in `.env.example`
  and compose (later, with implementation).
- **Docs:** ADR-12, `docs/api.md`, ADR-11 note "only `nack` is uncounted".

## 7. Test plan (mandatory)
- **Unit:** parse_death (all D1 cases), origin (D2), extract_job_id (D9), dedup key (D8), state
  classification (D3), error document (D6), config validator (D5).
- **Topology:**
  - exact arguments of `jobs.dlq.audit` and `jobs.dlq.audit.parked`;
  - the fanout binding;
  - parked queue not bound to anything;
  - `jobs.work.v2` unchanged.
- **Migration:** 004 applied on 003 data; old values still accepted; new values accepted; invalid values
  rejected.
- **Integration:**
  - real crash loop (10 SIGKILLs) → DEAD_LETTERED / `delivery_limit`, mailbox deleted, `applied`,
    copy in `jobs.dlq`;
  - crash loop after a retry hop (reason must not be `expired`);
  - malformed body + valid header → `rejected`;
  - garbage → `unreadable`;
  - unknown job;
  - completed job not overwritten;
  - worker copy ignored;
  - duplicate dead letter (`seen_count = 2`, 1 event);
  - stale copy;
  - id mismatch;
  - lock held;
  - DB outage;
  - grep check that `reject(requeue=True)` is absent.
- **Crash:**
  - SIGKILL the audit consumer before the commit, and after the commit but before the ack → exactly
    one event;
  - audit crash loop with a carried-over count → parked, not lost, `x-death[1]` = work-queue death.
- **Regression:** full suite once.
- **Not mandatory** (a 3-minute broker property, verified here in F2): a missing parking target
  delays but does not lose the message. It is covered by the topology test asserting
  at-least-once + reject-publish.

## 8. Deployment sequence (each step needs separate approval)
1. Implement and test in `notes_test`.
2. Dev stack:
   1. build Phase 2 images;
   2. apply migrations 003 and 004;
   3. start the outbox publisher and workers (they declare v2, the delay queues, the audit queue and the
      parked queue);
   4. run `scripts/migrate_work_queue.py` until `jobs.work` is deleted;
   5. verify topology and `/healthz`.
3. Commit only when you approve.

## 9. Mandatory vs optional
- **Mandatory:** D1–D10 as above, migration 004, the audit and parked queues, the consumer, the tests in
  §7, ADR-12 and the API docs, and deleting `jobs.work` in dev (D10).
- **Optional / not Part 4:** DLQ alerts, `/healthz` depth, the CLI (Part 6), replay (rejected by Q5),
  `diag` on the new crash tests, retention limits on `jobs.dlq` / parked.

## 10. Risks and unresolved assumptions
1. **Carried-over delivery count** (F1): mitigated only if D5's limit rule is enforced. The same applies
   to any future queue that receives dead letters.
2. A missing parking target is retried every ~180 s; the message waits in the source queue's
   dead-letter state (F2).
3. Unbounded growth of `jobs.dlq` and the parked queue: no retention policy is invented. The safest
   default is no TTL or length limit; the trade-off is disk and memory, so occasional manual inspection
   is needed.
4. `jobs.dlq` (Part 1 arguments: quorum only) has the default limit 20 and no DLX. Inspection crashes
   could drop a message, so the CLI must use get + `nack`. Changing its arguments needs a redeclaration
   or policy decision [NV].
5. At-least-once duplicates on lost confirms [DOC/NV]: covered by D8 and the CAS.
6. Single-node broker; behaviour on leader change or restart was not tested [NV].
7. Why reject and nack differ in counting: not verified (observed only).

## 11. Files and git
- Before: HEAD = `main` = `phase1-baseline` = `49eed18`; 0 staged; 41 status entries.
- After: unchanged (41 entries, 0 staged, HEAD `49eed18`).
- Created: this log only. Temporary vhost `p4probe2-1795088176` created and deleted. No code, tests,
  migration, Compose, configuration or project topology changed.
