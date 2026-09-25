CREATE TABLE IF NOT EXISTS orch_journey_settings (
    singleton_id SMALLINT PRIMARY KEY DEFAULT 1,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    retention_days SMALLINT NOT NULL DEFAULT 30,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_orch_journey_settings_singleton
        CHECK (singleton_id = 1),
    CONSTRAINT chk_orch_journey_settings_retention
        CHECK (retention_days BETWEEN 1 AND 30)
);

INSERT INTO orch_journey_settings (singleton_id, enabled, retention_days)
VALUES (1, TRUE, 30)
ON CONFLICT (singleton_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS orch_journey_flow_coverage (
    flow_uuid UUID PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    coverage_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_fact_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_orch_journey_flow_coverage_status
        CHECK (status IN ('active', 'paused'))
);

CREATE TABLE IF NOT EXISTS orch_journey_sessions (
    id BIGSERIAL PRIMARY KEY,
    source_session_id BIGINT NOT NULL REFERENCES orch_sessions(id) ON DELETE CASCADE,
    session_uuid UUID NOT NULL,
    flow_uuid UUID NOT NULL,
    flow_revision_id UUID,
    session_scope TEXT,
    person_key_hash TEXT,
    lifecycle_status TEXT NOT NULL DEFAULT 'in_progress',
    current_stage TEXT,
    current_stage_ordinal SMALLINT,
    highest_stage TEXT,
    highest_stage_ordinal SMALLINT,
    current_component_ref_id UUID,
    started_at TIMESTAMPTZ NOT NULL,
    last_progress_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    terminal_outcome TEXT,
    terminal_class TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_journey_sessions_source_session_id
        UNIQUE (source_session_id),
    CONSTRAINT uq_orch_journey_sessions_session_uuid
        UNIQUE (session_uuid),
    CONSTRAINT chk_orch_journey_sessions_lifecycle_status
        CHECK (lifecycle_status IN ('in_progress', 'waiting', 'completed', 'abandoned', 'failed')),
    CONSTRAINT chk_orch_journey_sessions_current_stage
        CHECK (
            current_stage IS NULL
            OR current_stage IN (
                'entrada', 'identificacao', 'qualificacao', 'abordagem',
                'proposta', 'decisao', 'desfecho'
            )
        ),
    CONSTRAINT chk_orch_journey_sessions_highest_stage
        CHECK (
            highest_stage IS NULL
            OR highest_stage IN (
                'entrada', 'identificacao', 'qualificacao', 'abordagem',
                'proposta', 'decisao', 'desfecho'
            )
        ),
    CONSTRAINT chk_orch_journey_sessions_current_stage_ordinal
        CHECK (current_stage_ordinal IS NULL OR current_stage_ordinal BETWEEN 1 AND 7),
    CONSTRAINT chk_orch_journey_sessions_highest_stage_ordinal
        CHECK (highest_stage_ordinal IS NULL OR highest_stage_ordinal BETWEEN 1 AND 7),
    CONSTRAINT chk_orch_journey_sessions_stage_pairs
        CHECK (
            (current_stage IS NULL) = (current_stage_ordinal IS NULL)
            AND (highest_stage IS NULL) = (highest_stage_ordinal IS NULL)
        ),
    CONSTRAINT chk_orch_journey_sessions_current_stage_mapping
        CHECK (
            current_stage IS NULL
            OR (current_stage = 'entrada' AND current_stage_ordinal = 1)
            OR (current_stage = 'identificacao' AND current_stage_ordinal = 2)
            OR (current_stage = 'qualificacao' AND current_stage_ordinal = 3)
            OR (current_stage = 'abordagem' AND current_stage_ordinal = 4)
            OR (current_stage = 'proposta' AND current_stage_ordinal = 5)
            OR (current_stage = 'decisao' AND current_stage_ordinal = 6)
            OR (current_stage = 'desfecho' AND current_stage_ordinal = 7)
        ),
    CONSTRAINT chk_orch_journey_sessions_highest_stage_mapping
        CHECK (
            highest_stage IS NULL
            OR (highest_stage = 'entrada' AND highest_stage_ordinal = 1)
            OR (highest_stage = 'identificacao' AND highest_stage_ordinal = 2)
            OR (highest_stage = 'qualificacao' AND highest_stage_ordinal = 3)
            OR (highest_stage = 'abordagem' AND highest_stage_ordinal = 4)
            OR (highest_stage = 'proposta' AND highest_stage_ordinal = 5)
            OR (highest_stage = 'decisao' AND highest_stage_ordinal = 6)
            OR (highest_stage = 'desfecho' AND highest_stage_ordinal = 7)
        ),
    CONSTRAINT chk_orch_journey_sessions_highest_stage_rank
        CHECK (
            current_stage_ordinal IS NULL
            OR (
                highest_stage_ordinal IS NOT NULL
                AND highest_stage_ordinal >= current_stage_ordinal
            )
        ),
    CONSTRAINT chk_orch_journey_sessions_time_order
        CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE TABLE IF NOT EXISTS orch_journey_stage_visits (
    id BIGSERIAL PRIMARY KEY,
    journey_session_id BIGINT NOT NULL REFERENCES orch_journey_sessions(id) ON DELETE CASCADE,
    session_uuid UUID NOT NULL,
    flow_uuid UUID NOT NULL,
    flow_revision_id UUID,
    component_ref_id UUID NOT NULL,
    component_kind TEXT NOT NULL,
    stage_id TEXT NOT NULL,
    stage_ordinal SMALLINT NOT NULL,
    previous_stage_id TEXT,
    previous_stage_ordinal SMALLINT,
    visit_number INTEGER NOT NULL,
    entered_at TIMESTAMPTZ NOT NULL,
    exited_at TIMESTAMPTZ,
    exit_kind TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_journey_stage_visits_component_visit
        UNIQUE (journey_session_id, component_ref_id, visit_number),
    CONSTRAINT chk_orch_journey_stage_visits_stage
        CHECK (
            stage_id IN (
                'entrada', 'identificacao', 'qualificacao', 'abordagem',
                'proposta', 'decisao', 'desfecho'
            )
        ),
    CONSTRAINT chk_orch_journey_stage_visits_previous_stage
        CHECK (
            previous_stage_id IS NULL
            OR previous_stage_id IN (
                'entrada', 'identificacao', 'qualificacao', 'abordagem',
                'proposta', 'decisao', 'desfecho'
            )
        ),
    CONSTRAINT chk_orch_journey_stage_visits_stage_ordinal
        CHECK (stage_ordinal BETWEEN 1 AND 7),
    CONSTRAINT chk_orch_journey_stage_visits_stage_mapping
        CHECK (
            (stage_id = 'entrada' AND stage_ordinal = 1)
            OR (stage_id = 'identificacao' AND stage_ordinal = 2)
            OR (stage_id = 'qualificacao' AND stage_ordinal = 3)
            OR (stage_id = 'abordagem' AND stage_ordinal = 4)
            OR (stage_id = 'proposta' AND stage_ordinal = 5)
            OR (stage_id = 'decisao' AND stage_ordinal = 6)
            OR (stage_id = 'desfecho' AND stage_ordinal = 7)
        ),
    CONSTRAINT chk_orch_journey_stage_visits_previous_stage_ordinal
        CHECK (previous_stage_ordinal IS NULL OR previous_stage_ordinal BETWEEN 1 AND 7),
    CONSTRAINT chk_orch_journey_stage_visits_previous_stage_pair
        CHECK ((previous_stage_id IS NULL) = (previous_stage_ordinal IS NULL)),
    CONSTRAINT chk_orch_journey_stage_visits_previous_stage_mapping
        CHECK (
            previous_stage_id IS NULL
            OR (previous_stage_id = 'entrada' AND previous_stage_ordinal = 1)
            OR (previous_stage_id = 'identificacao' AND previous_stage_ordinal = 2)
            OR (previous_stage_id = 'qualificacao' AND previous_stage_ordinal = 3)
            OR (previous_stage_id = 'abordagem' AND previous_stage_ordinal = 4)
            OR (previous_stage_id = 'proposta' AND previous_stage_ordinal = 5)
            OR (previous_stage_id = 'decisao' AND previous_stage_ordinal = 6)
            OR (previous_stage_id = 'desfecho' AND previous_stage_ordinal = 7)
        ),
    CONSTRAINT chk_orch_journey_stage_visits_visit_number
        CHECK (visit_number > 0),
    CONSTRAINT chk_orch_journey_stage_visits_time_order
        CHECK (exited_at IS NULL OR exited_at >= entered_at)
);

CREATE TABLE IF NOT EXISTS orch_journey_channel_actions (
    action_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    journey_session_id BIGINT NOT NULL REFERENCES orch_journey_sessions(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    flow_uuid UUID NOT NULL,
    flow_revision_id UUID,
    component_ref_id UUID NOT NULL,
    component_kind TEXT NOT NULL,
    channel TEXT NOT NULL,
    action_sequence INTEGER NOT NULL,
    person_key_hash TEXT,
    lifecycle_status TEXT NOT NULL DEFAULT 'requested',
    native_outcome TEXT,
    normalized_outcome TEXT,
    requested_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    engaged_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    provider_reference_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_journey_channel_actions_source
        UNIQUE (source_kind, source_id),
    CONSTRAINT uq_orch_journey_channel_actions_sequence
        UNIQUE (journey_session_id, component_ref_id, channel, action_sequence),
    CONSTRAINT chk_orch_journey_channel_actions_sequence
        CHECK (action_sequence > 0),
    CONSTRAINT chk_orch_journey_channel_actions_status
        CHECK (lifecycle_status IN ('requested', 'accepted', 'sent', 'delivered', 'engaged', 'completed', 'failed', 'unknown'))
);

CREATE TABLE IF NOT EXISTS orch_journey_channel_action_events (
    id BIGSERIAL PRIMARY KEY,
    action_id UUID NOT NULL REFERENCES orch_journey_channel_actions(action_id) ON DELETE CASCADE,
    event_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    native_status TEXT,
    normalized_status TEXT,
    occurred_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_journey_channel_action_events_key
        UNIQUE (action_id, event_key),
    CONSTRAINT chk_orch_journey_channel_action_events_metadata
        CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE TABLE IF NOT EXISTS orch_journey_snapshot_delivery (
    flow_uuid UUID NOT NULL,
    window_kind TEXT NOT NULL DEFAULT 'today',
    timezone TEXT NOT NULL DEFAULT 'America/Sao_Paulo',
    snapshot_sequence BIGINT NOT NULL DEFAULT 0,
    last_socket_sequence BIGINT NOT NULL DEFAULT 0,
    snapshot_id UUID,
    payload JSONB,
    payload_bytes INTEGER,
    dirty_since TIMESTAMPTZ DEFAULT NOW(),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    claimed_at TIMESTAMPTZ,
    claim_token UUID,
    last_built_at TIMESTAMPTZ,
    last_socket_send_at TIMESTAMPTZ,
    heartbeat_due_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (flow_uuid, window_kind, timezone),
    CONSTRAINT chk_orch_journey_snapshot_delivery_sequence
        CHECK (snapshot_sequence >= 0 AND last_socket_sequence >= 0),
    CONSTRAINT chk_orch_journey_snapshot_delivery_socket_sequence
        CHECK (last_socket_sequence <= snapshot_sequence),
    CONSTRAINT chk_orch_journey_snapshot_delivery_attempt_count
        CHECK (attempt_count >= 0),
    CONSTRAINT chk_orch_journey_snapshot_delivery_payload
        CHECK (payload IS NULL OR jsonb_typeof(payload) = 'object'),
    CONSTRAINT chk_orch_journey_snapshot_delivery_payload_bytes
        CHECK (payload_bytes IS NULL OR payload_bytes >= 0),
    CONSTRAINT chk_orch_journey_snapshot_delivery_payload_pair
        CHECK ((payload IS NULL) = (payload_bytes IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_orch_journey_flow_coverage_status
    ON orch_journey_flow_coverage (status, coverage_started_at, flow_uuid);

CREATE INDEX IF NOT EXISTS idx_orch_journey_sessions_flow_started
    ON orch_journey_sessions (flow_uuid, started_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_orch_journey_sessions_lifecycle_progress
    ON orch_journey_sessions (lifecycle_status, last_progress_at, id);

CREATE INDEX IF NOT EXISTS idx_orch_journey_sessions_retention
    ON orch_journey_sessions (started_at, id);

CREATE INDEX IF NOT EXISTS idx_orch_journey_stage_visits_flow_stage_entered
    ON orch_journey_stage_visits (flow_uuid, stage_id, entered_at, id);

CREATE INDEX IF NOT EXISTS idx_orch_journey_stage_visits_session_entered
    ON orch_journey_stage_visits (journey_session_id, entered_at, id);

CREATE INDEX IF NOT EXISTS idx_orch_journey_channel_actions_flow_requested
    ON orch_journey_channel_actions (flow_uuid, requested_at, action_id);

CREATE INDEX IF NOT EXISTS idx_orch_journey_channel_actions_channel_requested
    ON orch_journey_channel_actions (channel, requested_at, action_id);

CREATE INDEX IF NOT EXISTS idx_orch_journey_channel_action_events_action_occurred
    ON orch_journey_channel_action_events (action_id, occurred_at, id);

CREATE INDEX IF NOT EXISTS idx_orch_journey_snapshot_delivery_due
    ON orch_journey_snapshot_delivery (next_attempt_at, dirty_since, flow_uuid)
    WHERE dirty_since IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_journey_snapshot_delivery_lease
    ON orch_journey_snapshot_delivery (claimed_at, flow_uuid)
    WHERE claimed_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_journey_snapshot_delivery_heartbeat
    ON orch_journey_snapshot_delivery (heartbeat_due_at, flow_uuid);
