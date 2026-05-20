ALTER TABLE orch_whatsapp_limits
    DROP CONSTRAINT IF EXISTS ck_orch_whatsapp_limits_allowed_limit;
ALTER TABLE orch_whatsapp_limits
    DROP CONSTRAINT IF EXISTS orch_whatsapp_limits_allowed_limit_check;
ALTER TABLE orch_whatsapp_limits
    ADD CONSTRAINT ck_orch_whatsapp_limits_allowed_limit
    CHECK (allowed_limit >= -1);
