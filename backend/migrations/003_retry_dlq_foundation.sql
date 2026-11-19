-- 003_retry_dlq_foundation (Phase 2, Part 1): schema preparation only.
--   * outbox rows get an explicit destination (exchange + routing key) so the same
--     transactional outbox can later deliver retries (delay queues) and DLQ copies;
--   * jobs get a separate infrastructure-retry counter (limiter unavailable etc.);
--   * dead_letter_audit records every outcome of the (Part 4) DLQ audit consumer.
-- Safe on the Phase 1 schema: only ADD COLUMN with constant defaults (no table rewrite,
-- no UPDATE of existing rows, so the jobs status trigger is not fired), new constraints
-- that existing rows satisfy, and a new table.

-- ---------------------------------------------------------------- outbox destination
ALTER TABLE outbox
    ADD COLUMN destination_exchange    text NOT NULL DEFAULT 'jobs',
    ADD COLUMN destination_routing_key text NOT NULL DEFAULT 'job.work';

ALTER TABLE outbox ADD CONSTRAINT outbox_destination_exchange_allowed
    CHECK (destination_exchange IN ('jobs', 'jobs.retry', 'jobs.dlx'));

ALTER TABLE outbox ADD CONSTRAINT outbox_destination_routing_key_valid
    CHECK (destination_routing_key ~ '^[A-Za-z0-9._-]{1,255}$');

-- ---------------------------------------------------------------- infrastructure retries
ALTER TABLE jobs
    ADD COLUMN infra_attempts integer NOT NULL DEFAULT 0;

ALTER TABLE jobs ADD CONSTRAINT jobs_infra_attempts_non_negative CHECK (infra_attempts >= 0);

-- ---------------------------------------------------------------- dead-letter audit trail
-- job_id has NO foreign key on purpose: dead-lettered messages may reference unknown jobs,
-- and the audit row must still be recorded. No message bodies are stored.
CREATE TABLE dead_letter_audit (
    id            bigserial   PRIMARY KEY,
    dedup_key     text        NOT NULL UNIQUE CHECK (length(dedup_key) BETWEEN 1 AND 300),
    message_id    text        NULL,
    job_id        uuid        NULL,
    death_reason  text        NOT NULL CHECK (death_reason IN
                                  ('delivery_limit', 'rejected', 'expired', 'maxlen', 'unknown')),
    source_queue  text        NULL,
    outcome       text        NOT NULL CHECK (outcome IN
                                  ('applied', 'already_terminal', 'unknown_job', 'unreadable')),
    detail        jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(detail) = 'object'),
    seen_count    integer     NOT NULL DEFAULT 1 CHECK (seen_count >= 1),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX dead_letter_audit_job_idx ON dead_letter_audit (job_id) WHERE job_id IS NOT NULL;
CREATE INDEX dead_letter_audit_outcome_idx ON dead_letter_audit (outcome, last_seen_at);
