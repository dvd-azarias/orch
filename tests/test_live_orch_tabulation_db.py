from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.core.workspace import get_current_workspace_schema
from app.services.live_orch_tabulation_service import (
    build_live_tabulation_payloads,
    register_live_tabulation,
)
from app.services.migration_service import _run_migration_file


@pytest.mark.asyncio
async def test_live_tabulation_receipt_callback_and_replay_in_real_postgres() -> None:
    flow_uuid = str(uuid4())
    session_uuid = str(uuid4())
    conversation_id = str(uuid4())
    idempotency_key = f"live-db:{conversation_id}"
    raw_payload = {
        "type": "live.conversation.resolved",
        "workspace_id": str(uuid4()),
        "interaction_id": session_uuid,
        "conversation_id": conversation_id,
        "reason": "agent_closed",
        "idempotency_key": idempotency_key,
        "tabulation_event_id": f"event-{conversation_id}",
        "disposition_code": "RECUSA",
        "disposition_category": "negative",
        "polarity": "negative",
        "ends_session": True,
        "future_live_field": {"kept": True},
    }
    request_payload, callback_payload = build_live_tabulation_payloads(raw_payload)
    runtime = {
        "workflow_v2": {
            "blocking_stop_reason": "blocked_wait_for_event",
            "wait_for_event": {
                "event_source": "callback",
                "event_result": "tabulation",
            },
        }
    }

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            schema = get_current_workspace_schema()
            safe_schema = schema.replace('"', '""')
            await _run_migration_file(
                db_session,
                schema=schema,
                migration_path="sql/029_create_orch_live_tabulation_events.sql",
            )
            session_id = (
                await db_session.execute(
                    text(
                        f"""
                        INSERT INTO "{safe_schema}".orch_sessions (
                            uuid,
                            flow_uuid,
                            entity,
                            entity_type,
                            entity_address,
                            runtime_variables,
                            state
                        ) VALUES (
                            CAST(:session_uuid AS uuid),
                            CAST(:flow_uuid AS uuid),
                            'live-db-person',
                            'person',
                            '5511999999999',
                            CAST(:runtime AS jsonb),
                            1
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "session_uuid": session_uuid,
                        "flow_uuid": flow_uuid,
                        "runtime": json.dumps(runtime),
                    },
                )
            ).scalar_one()

            first = await register_live_tabulation(
                db_session,
                session_uuid=session_uuid,
                flow_uuid=flow_uuid,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
                callback_payload=callback_payload,
            )
            replay = await register_live_tabulation(
                db_session,
                session_uuid=session_uuid,
                flow_uuid=flow_uuid,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
                callback_payload=callback_payload,
            )
            conflict = await register_live_tabulation(
                db_session,
                session_uuid=session_uuid,
                flow_uuid=flow_uuid,
                idempotency_key=idempotency_key,
                request_payload={**request_payload, "notes": "different"},
                callback_payload={**callback_payload, "notes": "different"},
            )

            receipt = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            status,
                            request_payload,
                            callback_payload,
                            resume_required
                        FROM orch_live_tabulation_events
                        WHERE orch_session_uuid = CAST(:session_uuid AS uuid)
                          AND idempotency_key = :idempotency_key
                        """
                    ),
                    {
                        "session_uuid": session_uuid,
                        "idempotency_key": idempotency_key,
                    },
                )
            ).mappings().one()
            session_row = (
                await db_session.execute(
                    text(
                        """
                        SELECT state, ended_at, runtime_variables
                        FROM orch_sessions
                        WHERE id = :session_id
                        """
                    ),
                    {"session_id": session_id},
                )
            ).mappings().one()

            assert first.status == "applied"
            assert first.resume_required is True
            assert replay.status == "applied"
            assert replay.idempotent is True
            assert conflict.status == "conflict"
            assert receipt["status"] == "applied"
            assert receipt["resume_required"] is True
            assert "ends_session" not in receipt["request_payload"]
            assert "ends_session" not in receipt["callback_payload"]
            assert receipt["callback_payload"]["source"] == "atendimento_live_direct"
            assert receipt["callback_payload"]["outcome"] == "recusa"
            assert receipt["callback_payload"]["future_live_field"] == {"kept": True}
            assert session_row["state"] == 0
            assert session_row["ended_at"] is None
            callbacks = session_row["runtime_variables"]["callbacks_pending"]
            assert len(callbacks) == 1
            assert callbacks[0]["event_name"] == "callback"
            assert callbacks[0]["result"] == "tabulation"
            assert callbacks[0]["data"]["outcome"] == "recusa"
            assert "ends_session" not in callbacks[0]["data"]
        finally:
            await transaction.rollback()
