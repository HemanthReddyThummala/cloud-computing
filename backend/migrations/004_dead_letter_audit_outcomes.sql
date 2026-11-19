-- 004_dead_letter_audit_outcomes (Phase 2 Part 4 / ADR-12): two more audit outcomes.
--   stale        the dead-lettered message is not the job's current retry message (Part 3 identity);
--                the newer scheduled retry is left untouched.
--   inconsistent missing/contradictory broker metadata, missing message id, or conflicting job ids;
--                nothing is changed on the job.
-- 003 declared the outcome CHECK inline (unnamed); look its generated name up, drop it, and add a
-- named constraint. Existing rows only use the old values, so they satisfy the new constraint.
DO $$
DECLARE
    names text[];
BEGIN
    SELECT array_agg(conname) INTO names
      FROM pg_constraint
     WHERE conrelid = 'dead_letter_audit'::regclass
       AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%outcome%';
    IF names IS NULL OR array_length(names, 1) <> 1 THEN
        RAISE EXCEPTION 'expected exactly one outcome CHECK on dead_letter_audit, found %', names;
    END IF;
    EXECUTE format('ALTER TABLE dead_letter_audit DROP CONSTRAINT %I', names[1]);
END $$;

ALTER TABLE dead_letter_audit ADD CONSTRAINT dead_letter_audit_outcome_allowed
    CHECK (outcome IN ('applied', 'already_terminal', 'unknown_job', 'unreadable', 'stale', 'inconsistent'));
