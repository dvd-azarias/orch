from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.repositories.orch_channel_events_repository import (
    list_stale_pending_channel_event_sessions,
)


@pytest.mark.asyncio
async def test_reconcile_excludes_dialer_cdr_owned_by_generic_wait() -> None:
    flow_uuid = str(uuid4())
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_sessions (
                        id BIGINT PRIMARY KEY,
                        flow_uuid UUID NOT NULL,
                        state SMALLINT NOT NULL,
                        ended_at TIMESTAMPTZ NULL,
                        unassigned_at TIMESTAMPTZ NULL,
                        runtime_variables JSONB NOT NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_channel_events (
                        session_id BIGINT NOT NULL,
                        channel TEXT NOT NULL,
                        event_ts TIMESTAMPTZ NULL,
                        received_at TIMESTAMPTZ NOT NULL,
                        processed_at TIMESTAMPTZ NULL
                    ) ON COMMIT DROP
                    """
                )
            )

            blocked_wait_runtime = json.dumps(
                {
                    "workflow_v2": {
                        "blocking_stop_reason": "blocked_wait_for_event",
                    }
                }
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (
                        id, flow_uuid, state, runtime_variables
                    ) VALUES
                        (101, CAST(:flow_uuid AS uuid), 2, CAST(:blocked_wait AS jsonb)),
                        (102, CAST(:flow_uuid AS uuid), 2, CAST(:blocked_wait AS jsonb)),
                        (103, CAST(:flow_uuid AS uuid), 2, '{}'::jsonb)
                    """
                ),
                {
                    "flow_uuid": flow_uuid,
                    "blocked_wait": blocked_wait_runtime,
                },
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_channel_events (
                        session_id, channel, event_ts, received_at
                    ) VALUES
                        (101, 'dialer', NOW() - INTERVAL '2 minutes', NOW()),
                        (102, 'whatsapp', NOW() - INTERVAL '2 minutes', NOW()),
                        (103, 'dialer', NOW() - INTERVAL '2 minutes', NOW())
                    """
                )
            )

            candidates = await list_stale_pending_channel_event_sessions(
                db_session,
                stale_seconds=30,
                limit=200,
            )

            assert {row["session_id"] for row in candidates} == {102, 103}
            pending_dialer_cdr = (
                await db_session.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM orch_channel_events
                        WHERE session_id = 101
                          AND channel = 'dialer'
                          AND processed_at IS NULL
                        """
                    )
                )
            ).scalar_one()
            assert pending_dialer_cdr == 1
