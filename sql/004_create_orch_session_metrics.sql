CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS orch_session_metrics (
    id BIGSERIAL PRIMARY KEY,
    uuid UUID NOT NULL DEFAULT gen_random_uuid(),
    session_id BIGINT NOT NULL,
    session_uuid UUID,
    flow_uuid UUID,
    revision_id UUID,
    metric_type TEXT NOT NULL,
    step_index INTEGER,
    card_uuid UUID,
    card_cursor TEXT,
    component_kind TEXT,
    status TEXT NOT NULL,
    stopped_reason TEXT,
    latency_ms DOUBLE PRECISION NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_orch_session_metrics_type CHECK (metric_type IN ('card', 'workflow')),
    CONSTRAINT ck_orch_session_metrics_status CHECK (status IN ('success', 'error', 'stopped', 'locked'))
);

CREATE INDEX IF NOT EXISTS idx_orch_session_metrics_session_created
    ON orch_session_metrics (session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_orch_session_metrics_flow_created
    ON orch_session_metrics (flow_uuid, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_orch_session_metrics_type
    ON orch_session_metrics (metric_type);

CREATE INDEX IF NOT EXISTS idx_orch_session_metrics_stopped_reason
    ON orch_session_metrics (stopped_reason);
