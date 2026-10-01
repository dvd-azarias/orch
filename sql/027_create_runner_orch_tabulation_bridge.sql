CREATE TABLE IF NOT EXISTS orch_runner_session_links (
    runner_session_id UUID PRIMARY KEY,
    runner_flow_uuid UUID NOT NULL,
    orch_session_id BIGINT NOT NULL REFERENCES orch_sessions(id) ON DELETE CASCADE,
    provider_context_message_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_runner_session_links_provider_context
        UNIQUE (provider_context_message_id)
);

CREATE INDEX IF NOT EXISTS idx_orch_runner_session_links_orch_session
    ON orch_runner_session_links (orch_session_id, created_at);

CREATE TABLE IF NOT EXISTS orch_runner_tabulation_events (
    id BIGSERIAL PRIMARY KEY,
    event_key TEXT NOT NULL,
    runner_session_id UUID NOT NULL,
    runner_flow_uuid UUID NOT NULL,
    orch_session_id BIGINT REFERENCES orch_sessions(id) ON DELETE SET NULL,
    callback_payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_link',
    applied_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_runner_tabulation_events_session_event
        UNIQUE (runner_session_id, event_key),
    CONSTRAINT chk_orch_runner_tabulation_events_payload
        CHECK (jsonb_typeof(callback_payload) = 'object'),
    CONSTRAINT chk_orch_runner_tabulation_events_status
        CHECK (status IN ('pending_link', 'applied', 'ignored', 'conflict')),
    CONSTRAINT chk_orch_runner_tabulation_events_applied_state
        CHECK (
            (status = 'applied' AND applied_at IS NOT NULL)
            OR (status <> 'applied' AND applied_at IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_orch_runner_tabulation_events_pending
    ON orch_runner_tabulation_events (runner_session_id, created_at, id)
    WHERE status = 'pending_link';

CREATE INDEX IF NOT EXISTS idx_orch_channel_events_provider_message
    ON orch_channel_events (channel, event_id, session_id)
    WHERE event_id IS NOT NULL;
