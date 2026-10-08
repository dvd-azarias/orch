CREATE TABLE IF NOT EXISTS orch_live_tabulation_events (
    id BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    orch_session_id BIGINT REFERENCES orch_sessions(id) ON DELETE SET NULL,
    orch_session_uuid UUID NOT NULL,
    flow_uuid UUID NOT NULL,
    request_payload JSONB NOT NULL,
    callback_payload JSONB NOT NULL,
    status TEXT NOT NULL,
    resume_required BOOLEAN NOT NULL DEFAULT FALSE,
    applied_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_live_tabulation_events_session_event
        UNIQUE (orch_session_uuid, idempotency_key),
    CONSTRAINT chk_orch_live_tabulation_events_idempotency_key
        CHECK (char_length(idempotency_key) BETWEEN 1 AND 512),
    CONSTRAINT chk_orch_live_tabulation_events_request_payload
        CHECK (jsonb_typeof(request_payload) = 'object'),
    CONSTRAINT chk_orch_live_tabulation_events_callback_payload
        CHECK (jsonb_typeof(callback_payload) = 'object'),
    CONSTRAINT chk_orch_live_tabulation_events_status
        CHECK (status IN ('applied', 'ignored')),
    CONSTRAINT chk_orch_live_tabulation_events_applied_state
        CHECK (
            (status = 'applied' AND applied_at IS NOT NULL AND last_error IS NULL)
            OR (status = 'ignored' AND applied_at IS NULL AND last_error IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_orch_live_tabulation_events_session_created
    ON orch_live_tabulation_events (orch_session_uuid, created_at, id);
