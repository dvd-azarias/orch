CREATE TABLE IF NOT EXISTS orch_whatsapp_limits (
    id BIGSERIAL PRIMARY KEY,
    phone TEXT NOT NULL,
    allowed_limit INTEGER NOT NULL CHECK (allowed_limit >= -1),
    received_from_meta_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    in_use BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orch_whatsapp_limits_phone
    ON orch_whatsapp_limits (phone);

CREATE UNIQUE INDEX IF NOT EXISTS uq_orch_whatsapp_limits_phone_in_use
    ON orch_whatsapp_limits (phone)
    WHERE in_use = TRUE;

CREATE TABLE IF NOT EXISTS orch_whatsapp_rate_limit_per_flow (
    id BIGSERIAL PRIMARY KEY,
    flow_uuid UUID NOT NULL,
    phone TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed >= 0),
    day DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orch_whatsapp_rate_limit_per_flow UNIQUE (flow_uuid, phone, day)
);

CREATE INDEX IF NOT EXISTS idx_orch_whatsapp_rate_limit_per_flow_day
    ON orch_whatsapp_rate_limit_per_flow (day, flow_uuid);
