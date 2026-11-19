-- 002_result_integrity: the database itself guarantees that
--   * only COMPLETED / COMPLETED_WITH_WARNINGS jobs carry a result (failed jobs can never
--     expose a Notion URL), and every result has an https notion_page_url + frame counts;
--   * only FAILED / DEAD_LETTERED jobs carry an error.

ALTER TABLE jobs ADD CONSTRAINT jobs_result_only_on_success CHECK (
    (result IS NOT NULL) = (status IN ('COMPLETED', 'COMPLETED_WITH_WARNINGS')));

-- COALESCE(..., false): a CHECK that evaluates to NULL would PASS, and jsonb_typeof() of a
-- missing key is NULL — so every condition is forced to a definite true/false.
ALTER TABLE jobs ADD CONSTRAINT jobs_result_shape CHECK (
    result IS NULL OR COALESCE(
        jsonb_typeof(result) = 'object'
        AND jsonb_typeof(result -> 'notion_page_url') = 'string'
        AND (result ->> 'notion_page_url') ~ '^https://'
        AND jsonb_typeof(result -> 'frames_requested') = 'number'
        AND jsonb_typeof(result -> 'frames_published') = 'number', false));

ALTER TABLE jobs ADD CONSTRAINT jobs_error_only_on_failure CHECK (
    (error IS NOT NULL) = (status IN ('FAILED', 'DEAD_LETTERED')));
