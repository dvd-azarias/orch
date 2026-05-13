CREATE TABLE IF NOT EXISTS orch_channel_events (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES orch_sessions(id) ON DELETE CASCADE,
    flow_uuid UUID NOT NULL,
    channel VARCHAR(32) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    event_id VARCHAR(255),
    event_ts TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    discard_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_orch_channel_events_channel_event
    ON orch_channel_events (channel, event_id, event_type)
    TABLESPACE "__WORKSPACE_TABLESPACE__"
    WHERE event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_channel_events_session_pending
    ON orch_channel_events (session_id, channel, processed_at, event_ts, created_at)
    TABLESPACE "__WORKSPACE_TABLESPACE__";
