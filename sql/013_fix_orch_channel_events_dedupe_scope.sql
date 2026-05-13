DROP INDEX IF EXISTS uq_orch_channel_events_channel_event;

CREATE UNIQUE INDEX IF NOT EXISTS uq_orch_channel_events_session_channel_event
    ON orch_channel_events (session_id, channel, event_id, event_type)
    TABLESPACE "__WORKSPACE_TABLESPACE__"
    WHERE event_id IS NOT NULL;
