CREATE TABLE IF NOT EXISTS orch_discarded_events (
    id BIGSERIAL PRIMARY KEY,
    uuid UUID NOT NULL DEFAULT gen_random_uuid(),
    flow_uuid UUID NOT NULL,
    app_name TEXT NOT NULL,
    entity TEXT,
    entity_type TEXT,
    entity_address TEXT,
    entity_session_id TEXT,
    discard_reason TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_orch_discarded_events_uuid
    ON orch_discarded_events (uuid);

CREATE INDEX IF NOT EXISTS idx_orch_discarded_events_flow_created
    ON orch_discarded_events (flow_uuid, created_at DESC)
    TABLESPACE "__WORKSPACE_TABLESPACE__";

CREATE INDEX IF NOT EXISTS idx_orch_discarded_events_entity_address_created
    ON orch_discarded_events (entity_address, created_at DESC)
    TABLESPACE "__WORKSPACE_TABLESPACE__";
