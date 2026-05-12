CREATE INDEX IF NOT EXISTS idx_orch_sessions_flow_entity
    ON orch_sessions USING btree (flow_uuid, entity)
    TABLESPACE "__WORKSPACE_TABLESPACE__";
