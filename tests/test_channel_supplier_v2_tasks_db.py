from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.tasks import channel_supplier_v2_tasks as tasks


FLOW_UUID = "c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152"


@pytest.mark.asyncio
async def test_reconciler_query_selects_pending_and_expired_lease_in_postgres(
) -> None:
    real_factory = get_session_factory()

    async with real_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    "DROP TABLE IF EXISTS "
                    "pg_temp.channel_supplier_v2_reconcile_test_sessions"
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE channel_supplier_v2_reconcile_test_sessions (
                        id BIGINT PRIMARY KEY,
                        flow_uuid UUID NOT NULL,
                        state SMALLINT NOT NULL,
                        ended_at TIMESTAMPTZ NULL,
                        runtime_variables JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            now = datetime.now(timezone.utc)
            rows = [
                (1, "pending", None, None, now - timedelta(hours=1)),
                (2, "registering", None, None, now - timedelta(hours=2)),
                (3, "registering", None, None, now),
                (4, "pending_retry", None, None, now - timedelta(hours=1)),
                (5, "pending_retry", None, None, now),
                (
                    6,
                    "registered",
                    "sms",
                    "blocked_send_with_sms",
                    now - timedelta(minutes=30),
                ),
                (
                    7,
                    "registered",
                    "rcs",
                    "blocked_send_with_rcs",
                    now - timedelta(minutes=30),
                ),
                (8, "registered", "sms", None, now - timedelta(minutes=30)),
            ]
            for session_id, status, channel, blocking_reason, updated_at in rows:
                await db_session.execute(
                    text(
                        """
                        INSERT INTO channel_supplier_v2_reconcile_test_sessions (
                            id, flow_uuid, state, ended_at, runtime_variables, updated_at
                        )
                        VALUES (
                            :id, CAST(:flow_uuid AS uuid), 1, NULL,
                            CAST(:runtime_variables AS jsonb),
                            CAST(:updated_at AS timestamptz)
                        )
                        """
                    ),
                    {
                        "id": session_id,
                        "flow_uuid": FLOW_UUID,
                        "runtime_variables": json.dumps(
                            {
                                "workflow_v2": {
                                    "channel_dispatch_v2": {
                                        "status": status,
                                        "attempts": session_id,
                                        "channel": channel,
                                    },
                                    "blocking_stop_reason": blocking_reason,
                                }
                            }
                        ),
                        "updated_at": updated_at,
                    },
                )

            result = await tasks._list_reconcilable_channel_supplier_v2_dispatches(
                db_session,
                flow_allowlist=[FLOW_UUID],
                registration_lease_seconds=120,
                limit=100,
                _table_name="channel_supplier_v2_reconcile_test_sessions",
            )

            assert result == [
                {"id": 2, "flow_uuid": FLOW_UUID, "attempts": 2},
                {"id": 1, "flow_uuid": FLOW_UUID, "attempts": 1},
                {"id": 4, "flow_uuid": FLOW_UUID, "attempts": 4},
                {"id": 6, "flow_uuid": FLOW_UUID, "attempts": 6},
            ]
