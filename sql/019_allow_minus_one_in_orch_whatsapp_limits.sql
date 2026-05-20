DO $$
DECLARE
    _constraint_name text;
BEGIN
    FOR _constraint_name IN
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        WHERE rel.relname = 'orch_whatsapp_limits'
          AND con.contype = 'c'
          AND pg_get_constraintdef(con.oid) ILIKE '%allowed_limit%'
    LOOP
        EXECUTE format('ALTER TABLE orch_whatsapp_limits DROP CONSTRAINT IF EXISTS %I', _constraint_name);
    END LOOP;
END $$;

ALTER TABLE orch_whatsapp_limits
    ADD CONSTRAINT ck_orch_whatsapp_limits_allowed_limit
    CHECK (allowed_limit >= -1);
