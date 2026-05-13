ALTER TABLE orch_sessions
    DROP CONSTRAINT IF EXISTS ck_orch_sessions_state;

ALTER TABLE orch_sessions
    ADD CONSTRAINT ck_orch_sessions_state CHECK (state IN (0, 1, 2, 3, 5));
