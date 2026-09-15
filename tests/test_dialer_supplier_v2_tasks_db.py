from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.tasks import dialer_supplier_v2_tasks as tasks


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
FLOW_UUID = "4e163399-e9a0-4335-895f-316c6a161299"


@pytest.mark.asyncio
async def test_reconciler_query_selects_pending_and_expired_lease_in_postgres(
) -> None:
    real_factory = get_session_factory()

    async with real_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    "DROP TABLE IF EXISTS "
                    "pg_temp.dialer_supplier_v2_reconcile_test_sessions"
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE dialer_supplier_v2_reconcile_test_sessions (
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
                (1, "pending", None, now - timedelta(hours=1)),
                (
                    2,
                    "registering",
                    (now - timedelta(hours=2)).isoformat(),
                    now - timedelta(hours=2),
                ),
                (3, "registering", now.isoformat(), now),
                (4, "pending_retry", None, now - timedelta(hours=1)),
                (5, "pending_retry", None, now),
            ]
            for session_id, status, started_at, updated_at in rows:
                registration = {"status": status}
                if started_at is not None:
                    registration["registration_started_at"] = started_at
                await db_session.execute(
                    text(
                        """
                        INSERT INTO dialer_supplier_v2_reconcile_test_sessions (
                            id,
                            flow_uuid,
                            state,
                            ended_at,
                            runtime_variables,
                            updated_at
                        )
                        VALUES (
                            :id,
                            CAST(:flow_uuid AS uuid),
                            1,
                            NULL,
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
                                    "dialer_supplier_v2": registration
                                }
                            }
                        ),
                        "updated_at": updated_at,
                    },
                )
            result = await tasks._list_reconcilable_dialer_supplier_v2_cycles(
                db_session,
                flow_allowlist=[FLOW_UUID],
                registration_lease_seconds=120,
                limit=100,
                _table_name="dialer_supplier_v2_reconcile_test_sessions",
            )

            assert result == [
                {"id": 2, "flow_uuid": FLOW_UUID, "attempts": 0},
                {"id": 1, "flow_uuid": FLOW_UUID, "attempts": 0},
                {"id": 4, "flow_uuid": FLOW_UUID, "attempts": 0},
            ]
