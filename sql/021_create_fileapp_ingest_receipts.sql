CREATE TABLE IF NOT EXISTS orch_fileapp_ingest_receipts (
    id BIGSERIAL PRIMARY KEY,
    flow_uuid UUID NOT NULL,
    file_id UUID NOT NULL,
    folder_path TEXT NOT NULL,
    file_name TEXT,
    ingest_origin TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted',
    task_id UUID,
    first_received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enqueued_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_orch_fileapp_ingest_receipts_status
        CHECK (status IN ('accepted', 'enqueued', 'processing', 'completed', 'failed', 'enqueue_failed')),
    UNIQUE (flow_uuid, file_id)
) TABLESPACE "__WORKSPACE_TABLESPACE__";

CREATE INDEX IF NOT EXISTS idx_orch_fileapp_ingest_receipts_status_updated
    ON orch_fileapp_ingest_receipts (status, updated_at DESC)
    TABLESPACE "__WORKSPACE_TABLESPACE__";
