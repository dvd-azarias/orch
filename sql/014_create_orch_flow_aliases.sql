CREATE TABLE IF NOT EXISTS target.orch_flow_aliases (
    id BIGSERIAL PRIMARY KEY,
    alias VARCHAR(14) NOT NULL,
    workspace_uuid UUID NOT NULL,
    flow_uuid UUID NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (alias),
    UNIQUE (workspace_uuid, flow_uuid)
);

CREATE INDEX IF NOT EXISTS idx_orch_flow_aliases_active
    ON target.orch_flow_aliases (is_active, alias);
