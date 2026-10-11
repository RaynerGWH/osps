-- Only needed if an existing installation denies DELETE to the backend.
-- This grants a permission; it does not delete any sessions.
BEGIN;
DO $migration$
DECLARE
    target_schema text := 'public';
BEGIN
    EXECUTE format('GRANT DELETE ON TABLE %I.poker_sessions TO service_role', target_schema);
END
$migration$;
COMMIT;
