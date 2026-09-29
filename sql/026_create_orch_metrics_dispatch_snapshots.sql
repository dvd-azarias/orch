CREATE TABLE IF NOT EXISTS orch_metrics_dispatch_snapshots (
    action_id UUID PRIMARY KEY
        REFERENCES orch_journey_channel_actions(action_id) ON DELETE CASCADE,
    dispatch_id UUID NOT NULL,
    dispatched_at TIMESTAMPTZ NOT NULL,
    snapshot JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_metrics_dispatch_snapshots_dispatch_id
        UNIQUE (dispatch_id),
    CONSTRAINT chk_orch_metrics_dispatch_snapshots_snapshot
        CHECK (jsonb_typeof(snapshot) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_orch_metrics_dispatch_snapshots_dispatched_at
    ON orch_metrics_dispatch_snapshots (dispatched_at, action_id);
