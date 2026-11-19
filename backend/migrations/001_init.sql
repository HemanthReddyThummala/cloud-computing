-- 001_init: core job state, audit trail, stage checkpoints, transactional outbox,
-- idempotency keys. NOTE: no column anywhere stores a Notion token (ADR-03).

-- ---------------------------------------------------------------- jobs
CREATE TABLE jobs (
    id               uuid        PRIMARY KEY,
    video_id         text        NOT NULL CHECK (video_id ~ '^[A-Za-z0-9_-]{11}$'),
    youtube_url      text        NOT NULL CHECK (length(youtube_url) <= 2048),
    notion_parent_id uuid        NULL,
    status           text        NOT NULL DEFAULT 'QUEUED' CHECK (status IN (
                         'QUEUED', 'TRANSCRIBING', 'SUMMARIZING', 'EXTRACTING_FRAMES', 'PUBLISHING',
                         'COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'DEAD_LETTERED')),
    progress         smallint    NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    video_title      text        NULL,
    duration_sec     integer     NULL CHECK (duration_sec IS NULL OR duration_sec >= 0),
    attempts         integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    warnings         jsonb       NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(warnings) = 'array'),
    error            jsonb       NULL,
    result           jsonb       NULL,
    retry            jsonb       NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz NULL,
    CONSTRAINT jobs_finished_iff_terminal CHECK (
        (finished_at IS NOT NULL) =
        (status IN ('COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'DEAD_LETTERED')))
);
CREATE INDEX jobs_status_idx ON jobs (status);

-- ---------------------------------------------------------------- state machine (spec §4)
CREATE TABLE job_status_transitions (
    from_status text NOT NULL,
    to_status   text NOT NULL,
    PRIMARY KEY (from_status, to_status)
);
INSERT INTO job_status_transitions (from_status, to_status) VALUES
    ('QUEUED', 'TRANSCRIBING'),
    ('TRANSCRIBING', 'SUMMARIZING'),
    ('SUMMARIZING', 'EXTRACTING_FRAMES'),
    ('EXTRACTING_FRAMES', 'PUBLISHING'),
    ('PUBLISHING', 'COMPLETED'),
    ('PUBLISHING', 'COMPLETED_WITH_WARNINGS'),
    ('QUEUED', 'FAILED'),            ('QUEUED', 'DEAD_LETTERED'),
    ('TRANSCRIBING', 'FAILED'),      ('TRANSCRIBING', 'DEAD_LETTERED'),
    ('SUMMARIZING', 'FAILED'),       ('SUMMARIZING', 'DEAD_LETTERED'),
    ('EXTRACTING_FRAMES', 'FAILED'), ('EXTRACTING_FRAMES', 'DEAD_LETTERED'),
    ('PUBLISHING', 'FAILED'),        ('PUBLISHING', 'DEAD_LETTERED');

-- Defence in depth: even a buggy UPDATE cannot make an illegal transition or
-- modify a job that already reached a terminal state.
CREATE FUNCTION enforce_job_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IN ('COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'DEAD_LETTERED') THEN
        RAISE EXCEPTION 'job % is in terminal state % and cannot be modified', OLD.id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status <> OLD.status AND NOT EXISTS (
        SELECT 1 FROM job_status_transitions t
        WHERE t.from_status = OLD.status AND t.to_status = NEW.status) THEN
        RAISE EXCEPTION 'illegal job status transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;

CREATE TRIGGER jobs_enforce_transition
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION enforce_job_transition();

-- ---------------------------------------------------------------- audit trail
CREATE TABLE job_events (
    id          bigserial   PRIMARY KEY,
    job_id      uuid        NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    event_type  text        NOT NULL,
    from_status text        NULL,
    to_status   text        NULL,
    worker_id   text        NULL,
    detail      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX job_events_job_idx ON job_events (job_id, id);

-- ---------------------------------------------------------------- durable stage checkpoints
CREATE TABLE job_checkpoints (
    job_id     uuid        NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    stage      text        NOT NULL CHECK (stage IN ('TRANSCRIBING', 'SUMMARIZING', 'EXTRACTING_FRAMES', 'PUBLISHING')),
    data       jsonb       NOT NULL,
    worker_id  text        NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, stage)
);

-- ---------------------------------------------------------------- transactional outbox (ADR-02)
CREATE TABLE outbox (
    id               bigserial   PRIMARY KEY,
    event_id         uuid        NOT NULL UNIQUE,
    job_id           uuid        NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    event_type       text        NOT NULL,
    payload          jsonb       NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    published_at     timestamptz NULL,
    publish_attempts integer     NOT NULL DEFAULT 0,
    last_error       text        NULL
);
CREATE INDEX outbox_pending_idx ON outbox (id) WHERE published_at IS NULL;

-- ---------------------------------------------------------------- idempotency keys (ADR-04)
CREATE TABLE idempotency_keys (
    key         text        PRIMARY KEY CHECK (length(key) BETWEEN 8 AND 128),
    fingerprint text        NOT NULL,          -- keyed HMAC of the request; token not recoverable
    job_id      uuid        NOT NULL REFERENCES jobs (id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
    response    jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idempotency_keys_created_idx ON idempotency_keys (created_at);
