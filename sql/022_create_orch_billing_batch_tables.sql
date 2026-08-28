CREATE TABLE IF NOT EXISTS orch_billing_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    workspace_uuid UUID NOT NULL,
    billing_period DATE NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    quantity INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_attempt_at TIMESTAMPTZ,
    claimed_at TIMESTAMPTZ,
    claim_token UUID,
    sent_at TIMESTAMPTZ,
    last_error TEXT,
    reprocess_count INTEGER NOT NULL DEFAULT 0,
    reprocess_requested BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_orch_billing_snapshots_period
        CHECK (billing_period = date_trunc('month', billing_period)::date),
    CONSTRAINT chk_orch_billing_snapshots_quantity
        CHECK (quantity > 0),
    CONSTRAINT chk_orch_billing_snapshots_payload_object
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT chk_orch_billing_snapshots_status
        CHECK (status IN ('pending', 'processing', 'sent', 'failed', 'blocked'))
);

CREATE TABLE IF NOT EXISTS orch_billing_events (
    id BIGSERIAL PRIMARY KEY,
    workspace_uuid UUID NOT NULL,
    source_session_id BIGINT NOT NULL REFERENCES orch_sessions(id) ON DELETE RESTRICT,
    source_session_uuid UUID NOT NULL,
    billing_period DATE NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    metric_code TEXT NOT NULL,
    service_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    snapshot_id TEXT REFERENCES orch_billing_snapshots(snapshot_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_billing_events_source
        UNIQUE (workspace_uuid, source_session_uuid, billing_period, metric_code),
    CONSTRAINT chk_orch_billing_events_period
        CHECK (billing_period = date_trunc('month', billing_period)::date),
    CONSTRAINT chk_orch_billing_events_status
        CHECK (status IN ('pending', 'batched', 'sent')),
    CONSTRAINT chk_orch_billing_events_snapshot_state
        CHECK (
            (status = 'pending' AND snapshot_id IS NULL)
            OR (status IN ('batched', 'sent') AND snapshot_id IS NOT NULL)
        )
);

CREATE TABLE IF NOT EXISTS orch_billing_reprocess_requests (
    request_id UUID PRIMARY KEY,
    idempotency_key UUID NOT NULL,
    workspace_uuid UUID NOT NULL,
    billing_period DATE NOT NULL,
    requested_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted',
    source_sessions INTEGER NOT NULL DEFAULT 0,
    events_created INTEGER NOT NULL DEFAULT 0,
    snapshots_requeued INTEGER NOT NULL DEFAULT 0,
    processing_deferred INTEGER NOT NULL DEFAULT 0,
    cursor_session_id BIGINT NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_enqueued_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_billing_reprocess_idempotency
        UNIQUE (workspace_uuid, idempotency_key),
    CONSTRAINT chk_orch_billing_reprocess_period
        CHECK (billing_period = date_trunc('month', billing_period)::date),
    CONSTRAINT chk_orch_billing_reprocess_status
        CHECK (status IN ('accepted', 'running', 'completed', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_orch_billing_snapshots_due
    ON orch_billing_snapshots (next_attempt_at, created_at)
    WHERE status IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS idx_orch_billing_snapshots_processing_lease
    ON orch_billing_snapshots (claimed_at)
    WHERE status = 'processing';

CREATE INDEX IF NOT EXISTS idx_orch_billing_snapshots_workspace_period_status
    ON orch_billing_snapshots (workspace_uuid, billing_period, status);

CREATE INDEX IF NOT EXISTS idx_orch_billing_events_pending
    ON orch_billing_events (occurred_at, id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_orch_billing_events_workspace_period_status
    ON orch_billing_events (workspace_uuid, billing_period, status);

CREATE INDEX IF NOT EXISTS idx_orch_sessions_billing_created_at
    ON orch_sessions (created_at, id);

CREATE INDEX IF NOT EXISTS idx_orch_billing_reprocess_scan
    ON orch_billing_reprocess_requests (status, last_enqueued_at, updated_at)
    WHERE status IN ('accepted', 'running');
