CREATE TABLE IF NOT EXISTS orch_metrics_event_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL DEFAULT gen_random_uuid(),
    idempotency_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    flow_uuid UUID,
    session_uuid UUID,
    envelope JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at TIMESTAMPTZ,
    claim_token UUID,
    published_at TIMESTAMPTZ,
    last_http_status INTEGER,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_metrics_event_outbox_event_id UNIQUE (event_id),
    CONSTRAINT uq_orch_metrics_event_outbox_idempotency UNIQUE (idempotency_key),
    CONSTRAINT chk_orch_metrics_event_outbox_envelope
        CHECK (jsonb_typeof(envelope) = 'object'),
    CONSTRAINT chk_orch_metrics_event_outbox_status
        CHECK (status IN ('pending', 'publishing', 'published', 'dead')),
    CONSTRAINT chk_orch_metrics_event_outbox_attempts
        CHECK (attempts >= 0),
    CONSTRAINT chk_orch_metrics_event_outbox_http_status
        CHECK (last_http_status IS NULL OR last_http_status BETWEEN 100 AND 599),
    CONSTRAINT chk_orch_metrics_event_outbox_claim_pair
        CHECK ((claimed_at IS NULL) = (claim_token IS NULL)),
    CONSTRAINT chk_orch_metrics_event_outbox_published_state
        CHECK (
            (status = 'published' AND published_at IS NOT NULL)
            OR (status <> 'published' AND published_at IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_orch_metrics_event_outbox_due
    ON orch_metrics_event_outbox (next_attempt_at, created_at, id)
    WHERE status IN ('pending', 'publishing');

CREATE INDEX IF NOT EXISTS idx_orch_metrics_event_outbox_session
    ON orch_metrics_event_outbox (session_uuid, created_at, id)
    WHERE session_uuid IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_metrics_event_outbox_flow
    ON orch_metrics_event_outbox (flow_uuid, created_at, id)
    WHERE flow_uuid IS NOT NULL;
