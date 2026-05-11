ALTER TABLE orch_sessions
    ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ NULL;

ALTER TABLE orch_sessions
    ADD COLUMN IF NOT EXISTS unassigned_at TIMESTAMPTZ NULL;

ALTER TABLE orch_sessions
    DROP COLUMN IF EXISTS assigned;

ALTER TABLE orch_sessions
    DROP COLUMN IF EXISTS unassigned_in;
