ALTER TABLE orch_session_metrics
    DROP CONSTRAINT IF EXISTS ck_orch_session_metrics_type;

ALTER TABLE orch_session_metrics
    ADD CONSTRAINT ck_orch_session_metrics_type
    CHECK (metric_type IN ('card', 'workflow', 'dispatch', 'executor'));

ALTER TABLE orch_session_metrics
    DROP CONSTRAINT IF EXISTS ck_orch_session_metrics_status;

ALTER TABLE orch_session_metrics
    ADD CONSTRAINT ck_orch_session_metrics_status
    CHECK (status IN ('success', 'error', 'stopped', 'locked'));
