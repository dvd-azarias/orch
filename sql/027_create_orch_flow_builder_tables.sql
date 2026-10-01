CREATE TABLE IF NOT EXISTS orch_flow_builder_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_uuid UUID NOT NULL,
    mode TEXT NOT NULL DEFAULT 'orchestration',
    intent TEXT NOT NULL DEFAULT 'create',
    status TEXT NOT NULL DEFAULT 'planning',
    flow_uuid UUID,
    draft_checksum TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    plan JSONB NOT NULL DEFAULT '{}'::jsonb,
    compiled_definition JSONB,
    issues JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_orch_flow_builder_mode
        CHECK (mode = 'orchestration'),
    CONSTRAINT chk_orch_flow_builder_intent
        CHECK (intent IN ('create', 'edit')),
    CONSTRAINT chk_orch_flow_builder_status
        CHECK (status IN ('planning', 'ready', 'saved', 'abandoned', 'error')),
    CONSTRAINT chk_orch_flow_builder_version
        CHECK (version > 0),
    CONSTRAINT chk_orch_flow_builder_plan
        CHECK (jsonb_typeof(plan) = 'object'),
    CONSTRAINT chk_orch_flow_builder_definition
        CHECK (compiled_definition IS NULL OR jsonb_typeof(compiled_definition) = 'object'),
    CONSTRAINT chk_orch_flow_builder_issues
        CHECK (jsonb_typeof(issues) = 'array'),
    CONSTRAINT chk_orch_flow_builder_edit_target
        CHECK (intent = 'create' OR flow_uuid IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_orch_flow_builder_sessions_workspace_updated
    ON orch_flow_builder_sessions (workspace_uuid, updated_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_orch_flow_builder_sessions_flow
    ON orch_flow_builder_sessions (flow_uuid, updated_at DESC)
    WHERE flow_uuid IS NOT NULL;

CREATE TABLE IF NOT EXISTS orch_flow_builder_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL
        REFERENCES orch_flow_builder_sessions(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    attachment_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    structured_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_flow_builder_messages_sequence
        UNIQUE (session_id, sequence),
    CONSTRAINT chk_orch_flow_builder_messages_sequence
        CHECK (sequence > 0),
    CONSTRAINT chk_orch_flow_builder_messages_role
        CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    CONSTRAINT chk_orch_flow_builder_messages_content
        CHECK (length(content) BETWEEN 1 AND 20000),
    CONSTRAINT chk_orch_flow_builder_messages_attachment
        CHECK (jsonb_typeof(attachment_metadata) = 'object'),
    CONSTRAINT chk_orch_flow_builder_messages_payload
        CHECK (jsonb_typeof(structured_payload) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_orch_flow_builder_messages_session_sequence
    ON orch_flow_builder_messages (session_id, sequence);
