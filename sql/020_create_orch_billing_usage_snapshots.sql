CREATE TABLE IF NOT EXISTS orch_billing_usage_snapshots (
    id BIGSERIAL PRIMARY KEY,
    snapshot_id TEXT NOT NULL UNIQUE,
    session_id BIGINT NOT NULL REFERENCES orch_sessions(id) ON DELETE CASCADE,
    session_uuid UUID NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    publish_started_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_orch_billing_usage_snapshots_status
        CHECK (status IN ('pending', 'publishing', 'published'))
);

CREATE INDEX IF NOT EXISTS idx_orch_billing_usage_snapshots_pending
    ON orch_billing_usage_snapshots (status, created_at)
    WHERE status <> 'published';
