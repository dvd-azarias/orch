ALTER TABLE orch_sessions
ADD COLUMN IF NOT EXISTS entity_origin_app TEXT;

CREATE INDEX IF NOT EXISTS idx_orch_sessions_entity_origin_app
    ON orch_sessions (entity_origin_app);
