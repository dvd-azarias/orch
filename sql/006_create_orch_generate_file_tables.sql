CREATE TABLE IF NOT EXISTS orch_generate_file_job (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flow_id UUID NULL,
    component_ref_id TEXT NOT NULL,
    destination_type TEXT NOT NULL,
    destination_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    format_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    scheduling_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    mode TEXT NOT NULL DEFAULT 'imediato',
    next_run_at TIMESTAMPTZ NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT orch_generate_file_job_uniq_flow_component UNIQUE (flow_id, component_ref_id)
);

CREATE INDEX IF NOT EXISTS idx_orch_generate_file_job_due
    ON orch_generate_file_job (active, next_run_at);

CREATE TABLE IF NOT EXISTS orch_generate_file_row_buffer (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES orch_generate_file_job (id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    payload_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
    row_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NULL,
    sent_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT orch_generate_file_row_buffer_status_chk
        CHECK (status IN ('pending', 'processing', 'sent', 'failed')),
    CONSTRAINT orch_generate_file_row_buffer_uniq_job_session_hash
        UNIQUE (job_id, session_id, row_hash)
);

CREATE INDEX IF NOT EXISTS idx_orch_generate_file_row_buffer_pick
    ON orch_generate_file_row_buffer (job_id, status, created_at);

CREATE TABLE IF NOT EXISTS orch_generate_file_dispatch_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES orch_generate_file_job (id) ON DELETE CASCADE,
    file_target TEXT NULL,
    rows_selected INTEGER NOT NULL DEFAULT 0,
    rows_sent INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL,
    error_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orch_generate_file_dispatch_audit_job
    ON orch_generate_file_dispatch_audit (job_id, finished_at DESC);
