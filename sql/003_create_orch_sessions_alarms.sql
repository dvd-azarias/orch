CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS orch_sessions_alarms (
    id BIGSERIAL PRIMARY KEY,
    uuid UUID NOT NULL DEFAULT gen_random_uuid(),
    session_uuid UUID,
    flow_uuid UUID,
    app_name TEXT,
    entity TEXT,
    entity_type TEXT,
    entity_address TEXT,
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_orch_sessions_alarms_level CHECK (level IN ('warning', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_orch_sessions_alarms_created_at
    ON orch_sessions_alarms (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_orch_sessions_alarms_session_uuid
    ON orch_sessions_alarms (session_uuid);

CREATE INDEX IF NOT EXISTS idx_orch_sessions_alarms_flow_uuid
    ON orch_sessions_alarms (flow_uuid);

CREATE INDEX IF NOT EXISTS idx_orch_sessions_alarms_level
    ON orch_sessions_alarms (level);

CREATE INDEX IF NOT EXISTS idx_orch_sessions_alarms_app_name
    ON orch_sessions_alarms (app_name);
