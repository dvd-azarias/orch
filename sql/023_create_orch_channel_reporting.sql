CREATE TABLE IF NOT EXISTS orch_channel_reporting_state (
    singleton_id SMALLINT PRIMARY KEY DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    coverage_started_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ,
    activated_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_orch_channel_reporting_state_singleton
        CHECK (singleton_id = 1),
    CONSTRAINT chk_orch_channel_reporting_state_status
        CHECK (status IN ('pending', 'active')),
    CONSTRAINT chk_orch_channel_reporting_state_coverage
        CHECK (
            (status = 'pending' AND coverage_started_at IS NULL AND activated_at IS NULL)
            OR
            (status = 'active' AND coverage_started_at IS NOT NULL AND activated_at IS NOT NULL)
        )
);

INSERT INTO orch_channel_reporting_state (
    singleton_id,
    status,
    coverage_started_at,
    activated_at,
    activated_by
) VALUES (
    1,
    'pending',
    NULL,
    NULL,
    NULL
)
ON CONFLICT (singleton_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS orch_channel_actions (
    id BIGSERIAL PRIMARY KEY,
    uuid UUID NOT NULL DEFAULT gen_random_uuid(),
    session_id BIGINT NOT NULL REFERENCES orch_sessions(id) ON DELETE CASCADE,
    session_uuid UUID NOT NULL,
    flow_uuid UUID NOT NULL,
    flow_revision_id UUID NOT NULL,
    component_ref_id TEXT NOT NULL,
    component_kind TEXT NOT NULL,
    channel TEXT NOT NULL,
    action_sequence INTEGER NOT NULL DEFAULT 1,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    person_uuid UUID,
    destination_masked TEXT,
    provider_reference TEXT,
    lifecycle_status TEXT NOT NULL DEFAULT 'prepared',
    native_outcome TEXT,
    native_outcome_at TIMESTAMPTZ,
    requested_at TIMESTAMPTZ NOT NULL,
    queued_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    engaged_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    terminal_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_channel_actions_uuid UNIQUE (uuid),
    CONSTRAINT uq_orch_channel_actions_source UNIQUE (source_kind, source_id),
    CONSTRAINT uq_orch_channel_actions_session_component_sequence
        UNIQUE (
            session_uuid,
            flow_revision_id,
            component_ref_id,
            channel,
            action_sequence
        ),
    CONSTRAINT chk_orch_channel_actions_channel
        CHECK (channel IN ('voice', 'whatsapp', 'sms', 'rcs', 'email')),
    CONSTRAINT chk_orch_channel_actions_sequence
        CHECK (action_sequence > 0),
    CONSTRAINT chk_orch_channel_actions_lifecycle
        CHECK (
            lifecycle_status IN (
                'prepared',
                'queued',
                'in_progress',
                'accepted',
                'completed',
                'failed',
                'uncertain',
                'cancelled',
                'unknown'
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_orch_channel_actions_flow_requested
    ON orch_channel_actions (flow_uuid, requested_at DESC, id DESC)
    TABLESPACE "__WORKSPACE_TABLESPACE__";

CREATE INDEX IF NOT EXISTS idx_orch_channel_actions_session_requested
    ON orch_channel_actions (session_id, requested_at, id)
    TABLESPACE "__WORKSPACE_TABLESPACE__";

CREATE INDEX IF NOT EXISTS idx_orch_channel_actions_channel_requested
    ON orch_channel_actions (channel, requested_at DESC, id DESC)
    TABLESPACE "__WORKSPACE_TABLESPACE__";

CREATE INDEX IF NOT EXISTS idx_orch_channel_actions_person_requested
    ON orch_channel_actions (person_uuid, requested_at DESC, id DESC)
    TABLESPACE "__WORKSPACE_TABLESPACE__"
    WHERE person_uuid IS NOT NULL;

ALTER TABLE orch_channel_events
    ADD COLUMN IF NOT EXISTS action_id BIGINT REFERENCES orch_channel_actions(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_orch_channel_events_action_timeline
    ON orch_channel_events (action_id, event_ts, received_at, id)
    TABLESPACE "__WORKSPACE_TABLESPACE__"
    WHERE action_id IS NOT NULL;
