CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS orch_sessions (
    id BIGSERIAL PRIMARY KEY,
    uuid UUID NOT NULL DEFAULT gen_random_uuid(),
    flow_uuid UUID NOT NULL,
    state SMALLINT NOT NULL DEFAULT 0,
    entity_origin_app TEXT,
    entity TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_address TEXT NOT NULL,
    entity_session_id TEXT,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    abandoned_at TIMESTAMPTZ,
    frozen_until TIMESTAMPTZ,
    last_card_uuid UUID,
    next_card_uuid UUID,
    runtime_variables JSONB NOT NULL DEFAULT '{}'::jsonb,
    dialer_answered_at TIMESTAMPTZ,
    dialer_busy_at TIMESTAMPTZ,
    dialer_rejected_at TIMESTAMPTZ,
    dialer_invalid_number_at TIMESTAMPTZ,
    dialer_not_answered_at TIMESTAMPTZ,
    dialer_failed_at TIMESTAMPTZ,
    whatsapp_sent_at TIMESTAMPTZ,
    whatsapp_delivered_at TIMESTAMPTZ,
    whatsapp_read_at TIMESTAMPTZ,
    whatsapp_failed_at TIMESTAMPTZ,
    sms_sent_at TIMESTAMPTZ,
    sms_failed_at TIMESTAMPTZ,
    sms_delivered_at TIMESTAMPTZ,
    rcs_sent_at TIMESTAMPTZ,
    rcs_delivered_at TIMESTAMPTZ,
    rcs_read_at TIMESTAMPTZ,
    agent_interactions JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_orch_sessions_state CHECK (state IN (0, 1, 2, 3))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_orch_sessions_uuid
    ON orch_sessions (uuid);

CREATE INDEX IF NOT EXISTS idx_orch_sessions_flow_uuid
    ON orch_sessions (flow_uuid);

CREATE INDEX IF NOT EXISTS idx_orch_sessions_entity_keys
    ON orch_sessions (entity, entity_type, entity_address);

CREATE INDEX IF NOT EXISTS idx_orch_sessions_active_lookup
    ON orch_sessions (flow_uuid, entity, entity_type, entity_address)
    WHERE state <> 3;

CREATE INDEX IF NOT EXISTS idx_orch_sessions_entity_session_id
    ON orch_sessions (entity_session_id);

CREATE INDEX IF NOT EXISTS idx_orch_sessions_entity_origin_app
    ON orch_sessions (entity_origin_app);

CREATE INDEX IF NOT EXISTS idx_orch_sessions_state
    ON orch_sessions (state);

CREATE INDEX IF NOT EXISTS idx_orch_sessions_started_at
    ON orch_sessions (started_at);

CREATE INDEX IF NOT EXISTS idx_orch_sessions_whatsapp_sent_at
    ON orch_sessions (whatsapp_sent_at)
    WHERE whatsapp_sent_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_sessions_whatsapp_delivered_at
    ON orch_sessions (whatsapp_delivered_at)
    WHERE whatsapp_delivered_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_sessions_whatsapp_read_at
    ON orch_sessions (whatsapp_read_at)
    WHERE whatsapp_read_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_sessions_whatsapp_failed_at
    ON orch_sessions (whatsapp_failed_at)
    WHERE whatsapp_failed_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_sessions_dialer_answered_at
    ON orch_sessions (dialer_answered_at)
    WHERE dialer_answered_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_sessions_dialer_busy_at
    ON orch_sessions (dialer_busy_at)
    WHERE dialer_busy_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_sessions_dialer_rejected_at
    ON orch_sessions (dialer_rejected_at)
    WHERE dialer_rejected_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_sessions_dialer_invalid_number_at
    ON orch_sessions (dialer_invalid_number_at)
    WHERE dialer_invalid_number_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_sessions_dialer_not_answered_at
    ON orch_sessions (dialer_not_answered_at)
    WHERE dialer_not_answered_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_sessions_dialer_failed_at
    ON orch_sessions (dialer_failed_at)
    WHERE dialer_failed_at IS NOT NULL;
