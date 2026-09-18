from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.services.channel_supplier_v2_callback_service import (
    persist_channel_supplier_v2_callback,
)


FLOW_UUID = "c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152"
REVISION_UUID = "44444444-4444-4444-8444-444444444444"
COMPONENT_REF_ID = "send-sms-1"


def _claims(*, session_uuid: str, sequence: int = 1) -> dict:
    return {
        "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        "session_uuid": session_uuid,
        "flow_uuid": FLOW_UUID,
        "flow_revision_id": REVISION_UUID,
        "component_ref_id": COMPONENT_REF_ID,
        "channel": "sms",
        "dispatch_sequence": sequence,
    }


def _intent(claims: dict) -> dict:
    return {
        "session_uuid": claims["session_uuid"],
        "flow_uuid": claims["flow_uuid"],
        "flow_revision_id": claims["flow_revision_id"],
        "component_ref_id": claims["component_ref_id"],
        "channel": claims["channel"],
        "dispatch_sequence": claims["dispatch_sequence"],
    }


async def _create_temp_callback_tables(db_session) -> None:
    await db_session.execute(
        text(
            """
            CREATE TEMP TABLE orch_sessions (
                id BIGINT PRIMARY KEY,
                uuid UUID NOT NULL UNIQUE,
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
                id BIGSERIAL PRIMARY KEY,
                session_id BIGINT NOT NULL,
                flow_uuid UUID NOT NULL,
                channel VARCHAR(32) NOT NULL,
                event_type VARCHAR(64) NOT NULL,
                event_id VARCHAR(255),
                event_ts TIMESTAMPTZ,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                processed_at TIMESTAMPTZ,
                discard_reason TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            ) ON COMMIT DROP
            """
        )
    )
    await db_session.execute(
        text(
            """
            CREATE UNIQUE INDEX callback_event_identity_test
                ON orch_channel_events (
                    session_id,
                    channel,
                    event_id,
                    event_type
                )
                WHERE event_id IS NOT NULL
            """
        )
    )


@pytest.mark.asyncio
async def test_callback_ledger_is_idempotent_and_scoped_per_exact_session() -> None:
    session_factory = get_session_factory()
    first_uuid = str(uuid4())
    second_uuid = str(uuid4())
    first_claims = _claims(session_uuid=first_uuid)
    second_claims = _claims(session_uuid=second_uuid)

    async with session_factory() as db_session:
        async with db_session.begin():
            await _create_temp_callback_tables(db_session)
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (
                        id,
                        uuid,
                        flow_uuid,
                        state,
                        runtime_variables
                    ) VALUES
                        (
                            7001,
                            CAST(:first_uuid AS uuid),
                            CAST(:flow_uuid AS uuid),
                            1,
                            CAST(:first_runtime AS jsonb)
                        ),
                        (
                            7002,
                            CAST(:second_uuid AS uuid),
                            CAST(:flow_uuid AS uuid),
                            1,
                            CAST(:second_runtime AS jsonb)
                        )
                    """
                ),
                {
                    "first_uuid": first_uuid,
                    "second_uuid": second_uuid,
                    "flow_uuid": FLOW_UUID,
                    "first_runtime": json.dumps(
                        {"workflow_v2": {"channel_dispatch_v2": _intent(first_claims)}}
                    ),
                    "second_runtime": json.dumps(
                        {"workflow_v2": {"channel_dispatch_v2": _intent(second_claims)}}
                    ),
                },
            )
            payload = {"message_id": "provider-message-1", "codigo_status": 4}

            first = await persist_channel_supplier_v2_callback(
                db_session,
                claims=first_claims,
                channel="sms",
                event_kind="status",
                payload=payload,
            )
            duplicate = await persist_channel_supplier_v2_callback(
                db_session,
                claims=first_claims,
                channel="sms",
                event_kind="status",
                payload=payload,
            )
            second = await persist_channel_supplier_v2_callback(
                db_session,
                claims=second_claims,
                channel="sms",
                event_kind="status",
                payload=payload,
            )

            assert first is not None
            assert first.inserted_count == 1
            assert first.resume_required is True
            assert duplicate is not None
            assert duplicate.inserted_count == 0
            assert duplicate.idempotent_count == 1
            assert second is not None
            assert second.inserted_count == 1
            rows = (
                await db_session.execute(
                    text(
                        """
                        SELECT session_id, COUNT(*) AS event_count
                        FROM orch_channel_events
                        GROUP BY session_id
                        ORDER BY session_id
                        """
                    )
                )
            ).mappings().all()
            assert [dict(row) for row in rows] == [
                {"session_id": 7001, "event_count": 1},
                {"session_id": 7002, "event_count": 1},
            ]


@pytest.mark.asyncio
async def test_historical_callback_is_audited_without_reopening_session() -> None:
    session_factory = get_session_factory()
    session_uuid = str(uuid4())
    historical_claims = _claims(session_uuid=session_uuid, sequence=1)
    current_claims = _claims(session_uuid=session_uuid, sequence=2)

    async with session_factory() as db_session:
        async with db_session.begin():
            await _create_temp_callback_tables(db_session)
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (
                        id,
                        uuid,
                        flow_uuid,
                        state,
                        runtime_variables
                    ) VALUES (
                        7101,
                        CAST(:session_uuid AS uuid),
                        CAST(:flow_uuid AS uuid),
                        1,
                        CAST(:runtime_variables AS jsonb)
                    )
                    """
                ),
                {
                    "session_uuid": session_uuid,
                    "flow_uuid": FLOW_UUID,
                    "runtime_variables": json.dumps(
                        {
                            "workflow_v2": {
                                "channel_dispatch_v2": _intent(current_claims),
                                "channel_dispatch_v2_history": [
                                    _intent(historical_claims)
                                ],
                            }
                        }
                    ),
                },
            )

            result = await persist_channel_supplier_v2_callback(
                db_session,
                claims=historical_claims,
                channel="sms",
                event_kind="dlr",
                payload={"message_id": "late-message", "codigo_status": 1},
            )

            assert result is not None
            assert result.late_callback is True
            assert result.resume_required is False
            event = (
                await db_session.execute(
                    text(
                        """
                        SELECT processed_at, discard_reason
                        FROM orch_channel_events
                        WHERE session_id = 7101
                        """
                    )
                )
            ).mappings().one()
            assert event["processed_at"] is not None
            assert event["discard_reason"] == (
                "channel_supplier_v2_late_callback"
            )
