# Session log — Phase 2, Part 3: final implementation and completion audit

- **Date:** 2026-11-19
- **Branch:** `phase-2`; `main` = `phase1-baseline` = `49eed18`; no commits; no staged changes
- **Nature:** audit only. No application code, tests, configuration, assertions or documentation
  modified — the only file written is this log. Part 3 is **not** declared complete; no commits; Part 4
  not started.
- Earlier logs (unchanged): `phase2_part3_crash_diagnostics.md`, `phase2_part3_crash_reliability_10_runs.md`.

## 1. Audit objectives
Review the Part 3 implementation (classified retries via the transactional outbox, backoff, dead-lettering
on exhaustion, crash consistency) against the approved Part 3 design (P3-D1…D6 and clarifications A–C),
the project specification (§4, §5, §6), ADR-05 / ADR-06 / ADR-11 and ADR-10, `docs/api.md`, and the
retry/failure tests — by inspecting source code and tests, not previous reports.

Evidence labels used below: **[SRC]** verified by source inspection · **[TEST]** verified by automated
tests · **[RUN]** supported by runtime evidence · **[NV]** not independently verified.

## 2. Work log
1. Searched for the removed interim settings (`LOCK_BUSY_RETRY_DELAY_SEC`, `UNEXPECTED_ERROR_RETRY_DELAY_SEC`,
   `x-processing-attempts`): no references anywhere.
2. Read `worker/retry_policy.py`, `worker/runner.py` (`_process_message`, `_delay_without_counting`,
   `_handle`, `_defer_duplicate`, `_process`, `_run_stage`, `_handle_stage_failure`),
   `common/jobs_repo.py` (`_bump_attempts`, `schedule_retry`, `dead_letter`, `transition`),
   `common/broker.py` (`republish`), `gateway/routes.py`, `common/config.py`, `docker-compose.yml`,
   `.env.example`, ADR-05 / ADR-11, `docs/api.md`, and the list of Part 3 tests.
3. One verification run (16:34:22–16:37:39 IST), no reruns: `test_retries`, `test_unit_retry_policy`,
   `test_unit_errors`, `test_unit_http_classifier`, `test_results_and_dlq`, `test_topology_v2`,
   `test_migration_003`, `test_api` → **352 passed in 194.19 s**, 0 failed, 0 errors. RabbitMQ,
   PostgreSQL and Redis were healthy with 0 restarts before and after the run. No new diagnostics files
   (still only the 5 intentional ones).
4. Git: HEAD = `main` = `phase1-baseline` = `49eed18`; 0 staged files.

## 3. Findings by area

**A. Classification.** [SRC] Stage exceptions go through `classify_exception(Provider.GENERIC, exc)`
(`runner.py:381`). A fatal error becomes `FAILED` via a CAS transition, with no retry and no DLQ copy.
[TEST] `test_fatal_error_fails_once_without_retry` and the unit error/classifier suites. Note: unknown
exceptions are classified as `Provider.GENERIC`. The Phase 3 stages must raise classified errors so that
provider-specific codes and budgets apply.

**B. Scheduling / backoff.** [SRC] `backoff_step` = `schedule[min(n,len)-1]` (the last step repeats).
RateLimited uses `max(step, retry_after)`, with retry_after capped at 3600 s by ADR-10. QuotaExhausted
uses the normal step and keeps `resets_at`. `choose_bucket` picks the largest bucket ≤ remaining, else the
smallest. Early deliveries are delayed again without counting (`runner.py:298-306`). [TEST]
`test_backoff_schedule_last_step_repeats_then_dead_letter`,
`test_rate_limited_retry_after_is_honoured_with_uncounted_hops`,
`test_quota_exhausted_uses_normal_budget_and_keeps_reset_time`, and the unit tests. `next_attempt_at` uses
the worker's clock, which assumes all clocks agree; that holds on a single Docker host.
- **Deviation (documentation and config only):** ADR-05 (`decisions.md:115`) says
  "`BACKOFF_SCHEDULE_SEC` (+ jitter)" and `.env.example:95` defines `BACKOFF_JITTER=0.2`, but **no code
  implements jitter**: no reference in `notesagent/` or compose, and ADR-11 does not mention it. This
  needs a decision: remove the mention and the variable, or approve jitter as a later change.

**C. Limits / exhaustion.** [SRC] `MAX_ATTEMPTS` counts total executions (P3-D6). Provider errors use
`attempts`; RateLimiterUnavailable uses `infra_attempts`/`INFRA_MAX_ATTEMPTS`. When `attempt ≥ limit`, the
plan is a `DeadLetterPlan` with `{code: RETRIES_EXHAUSTED, message, last_error_code, attempts[, resets_at]}`
(P3-D3). [TEST] `test_five_attempts_means_four_retries_then_dead_letter`, `test_max_attempts_one_…`,
`test_infra_budget_is_separate`, `test_rate_limiter_unavailable_uses_only_the_infra_budget`,
`test_unexpected_errors_are_bounded_and_dead_lettered`.

**D. DB / outbox.** [SRC] `schedule_retry` and `dead_letter` each run in ONE transaction containing:
- a CAS on status and on the counter (`counter = attempt-1`, otherwise `StaleJobState`)
- an event
- an outbox row with its destination (`jobs.retry`/`<N>s` or `jobs.dlx`/`dead`)
- `pg_notify`

`transition` clears `retry`. The trigger and constraints from migrations 001/002/003 are unchanged. [TEST]
`test_migration_003`, `test_db_failure_in_retry_transaction_counts_nothing`.
- Wording: ADR-05 (`decisions.md:110-111`) still says "ack … after a retry / DLQ message has been
  confirmed by the broker". Since Part 3 the ack happens after the **outbox commit**, as ADR-11 correctly
  says. The code is right; only the documentation is inconsistent.

**E. Crash consistency (5 windows).** [SRC]
1. Crash before the retry transaction: nothing is committed, and redelivery resumes the job.
2. Crash after the commit, before the ack: the redelivered original is stale (message_id ≠
   `retry.event_id`) and is dropped.
3. Duplicate retry copies after a publisher crash: the advisory lock serialises them, and copies are
   dropped once the state has moved on.
4. Crash during the retried stage: the redelivered retry copy runs again; the counter CAS prevents
   double counting.
5. Crash after the dead-letter commit: the job is terminal, so the message is dropped and the credential
   is cleaned up.

[TEST] the five `test_crash_*` / `test_duplicate_retry_copies_*` tests. [RUN] the 10-run series (160/160)
and today's run.

**F. Message identity.** [SRC] Each plan gets a new `event_id`, which is also the outbox `event_id` and the
AMQP `message_id`. `broker.republish` keeps `message_id` and strips the `x-delivery*`/`x-death*` headers.
Hops, deferrals and infrastructure delays keep the id. [TEST] `test_retry_event_identity_end_to_end`,
`test_every_plan_gets_a_new_event_id…`.

**G. API.** [SRC] `routes.py:40` uses `public_retry_view`, which returns `reason, attempt, max_attempts,
next_attempt_at` and never `event_id` or `budget`. This matches `docs/api.md` lines 66-75 and the
DEAD_LETTERED example on lines 112-117. [TEST] `test_retry_state_is_visible_while_waiting_without_internals`,
`test_public_retry_view`, `test_api`. Minor: the gateway imports `worker.retry_policy`; the user earlier
decided not to move it.

**H. Configuration / deployment.** [SRC] The config defaults (5, 20, `10,30,90`, 5 s, 30 s, 3600 s) match
the compose defaults for gateway, publisher, worker and tests, and `.env.example`. `delay_queue_seconds` is
the union of the backoff, lock and infra delays. `rabbitmq.conf` sets a 512 MiB override with 0.6, giving
a 307 MiB watermark. [NV] The dev stack still runs Phase 1 images, and the work-queue migration has not
been run there. The local `.env` (from Phase 0) lacks the new variables, so compose defaults apply.

**I. Tests / diagnostics.** [TEST] 352/352 passed today; the earlier full suite had 555 passed. `diag`
covers only 2 of the 16 crash tests.

## 4. Historical repetition 2 (recorded as is)
Crash set, repetition 2 (after the RabbitMQ memory fix): 2 failed
(`test_crash_after_dead_letter_commit_before_ack`, `test_lock_unavailable_delays_without_counting`) and
1 setup/teardown error, taking 502 s instead of ~85 s. The output was lost; **the cause is unknown and was
not determined**. It did not reproduce in any of these:
- 5×5 individual runs
- reps 3–5
- the diagnostic-validation run
- 10 consecutive repetitions (160/160)
- today's run

Assessment: **documented residual uncertainty, not a blocker.** There is no evidence of a logic defect;
the logic of both tests is verified by source inspection [SRC], and both have been stable over 15+ runs.
The cause cannot be ruled out, however, and must stay in the record.

## 5. Residual risks (none block Part 3; owners noted)
1. Broker-initiated dead letters (delivery-limit, malformed messages) do not update PostgreSQL, so a job
   can stay non-terminal → **Part 4**.
2. P3-D4 delay-without-counting has no upper bound during a long DB outage (approved, clarification A).
3. The `nack(requeue)` fallback when the delay publish fails is not counted by `x-delivery-limit`
   (documented in ADR-11).
4. Worker DLQ copies (`jobs.dlx`) will also reach the Part 4 audit consumer, so that consumer must be
   idempotent.
5. Unclassified stage exceptions are treated as `Provider.GENERIC`; the Phase 3 stages must raise
   classified errors.
6. Historical rep 2 (§4) and the Phase 1 mailbox intermittent failure are both unexplained.

## 6. Recommendation
The implementation matches the approved Part 3 design and ADR-11 [SRC][TEST]. Before Part 3 is closed,
two documentation inconsistencies should be fixed: the jitter mention plus `BACKOFF_JITTER`, and the ack
wording in ADR-05. Fixing them touches documentation and config, so it needs the user's approval. Treat
rep 2 as documented residual uncertainty. **Part 3 is not declared complete; awaiting the user's
decision.** No commits; Part 4 not started.

## 7. Git and scope at end of session
The only file written was this log (untracked). HEAD = `main` = `phase1-baseline` = `49eed18`; 0 staged.
