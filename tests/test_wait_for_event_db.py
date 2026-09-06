from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text

import app.services.workflow_m2_service as workflow
from app.core.database import get_session_factory
from app.core.workspace import get_current_workspace_schema
from app.repositories.orch_sessions_repository import persist_callback_event_for_active_entity
from app.services.workflow_revision_service import WorkflowRevisionResolution


@pytest.mark.asyncio
async def test_wait_for_event_callback_resumes_in_real_postgres_without_shared_residue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_uuid = str(uuid4())
    revision_uuid = str(uuid4())
    session_uuid = str(uuid4())
    wait_ref = str(uuid4())
    finish_ref = str(uuid4())
    definition = {
        "components": [
            {
                "ref_id": wait_ref,
                "component_id": "wait_for_event",
                "parameters": {
                    "event_source": "callback",
                    "event_result": "approved",
                    "timeout_seconds": 3600,
                    "output_var": "approval_event",
                },
            },
            {"ref_id": finish_ref, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [{"from": wait_ref, "to": finish_ref, "branch": "received"}],
    }
    runtime = {
        "workflow_v2": {
            "flow_id": flow_uuid,
            "revision_id": revision_uuid,
            "revision_version": 1,
            "revision_mode": "published",
            "last_card_cursor": None,
            "next_card_cursor": wait_ref,
        },
        "input_payload": {"external_id": "wait-db-person"},
        "variables": {"payload": {}, "customs": {}},
    }

    monkeypatch.setattr(workflow, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(workflow, "fetch_flow_row", AsyncMock(return_value={"id": flow_uuid}))
    monkeypatch.setattr(
        workflow,
        "resolve_workflow_revision_for_session",
        AsyncMock(
            return_value=WorkflowRevisionResolution(
                revision={"id": revision_uuid, "definition": definition},
                source="pinned",
                requested_revision_id=revision_uuid,
                failure_reason=None,
            )
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_contact_runtime_context_for_session",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            safe_schema = get_current_workspace_schema().replace('"', '""')
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            session_id = (
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_sessions (
                            uuid,
                            flow_uuid,
                            entity,
                            entity_type,
                            entity_address,
                            runtime_variables,
                            next_card_uuid,
                            state
                        ) VALUES (
                            CAST(:session_uuid AS uuid),
                            CAST(:flow_uuid AS uuid),
                            'wait-db-person',
                            'person',
                            'wait-db-person',
                            CAST(:runtime AS jsonb),
                            CAST(:next_card_uuid AS uuid),
                            0
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "session_uuid": session_uuid,
                        "flow_uuid": flow_uuid,
                        "runtime": json.dumps(runtime),
                        "next_card_uuid": wait_ref,
                    },
                )
            ).scalar_one()

            armed = await workflow.execute_workflow_m2_for_session(
                db_session,
                flow_uuid=flow_uuid,
                session_id=session_id,
            )
            assert armed.stopped_reason == "blocked_wait_for_event"

            callback = await persist_callback_event_for_active_entity(
                db_session,
                flow_uuid=flow_uuid,
                app_name="GenericApp",
                entity="wait-db-person",
                payload={
                    "event_name": "callback",
                    "entity": "wait-db-person",
                    "result": "APPROVED",
                    "data": {"approval_id": "db-1"},
                },
                extracted={
                    "entity": "wait-db-person",
                    "entity_type": "person",
                    "entity_address": "wait-db-person",
                    "entity_session_id": "wait-db-person",
                },
            )
            assert callback is not None
            assert callback.state == 0

            resumed = await workflow.execute_workflow_m2_for_session(
                db_session,
                flow_uuid=flow_uuid,
                session_id=session_id,
            )
            row = (
                await db_session.execute(
                    text(
                        """
                        SELECT state, ended_at, frozen_until, runtime_variables
                        FROM orch_sessions
                        WHERE id = :session_id
                        """
                    ),
                    {"session_id": session_id},
                )
            ).mappings().one()

            assert resumed.stopped_reason == "finished_by_component"
            assert row["state"] == 3
            assert row["ended_at"] is not None
            assert row["frozen_until"] is None
            assert row["runtime_variables"]["variables"]["customs"]["approval_event"] == {
                "status": "received",
                "event_source": "callback",
                "event_result": "approved",
                "received_at": row["runtime_variables"]["callback"]["received_at"],
                "data": {"approval_id": "db-1"},
            }
        finally:
            await transaction.rollback()

        remaining = (
            await db_session.execute(
                text(
                    f"""
                    SELECT COUNT(*)
                    FROM "{safe_schema}".orch_sessions
                    WHERE uuid = CAST(:session_uuid AS uuid)
                    """
                ),
                {"session_uuid": session_uuid},
            )
        ).scalar_one()
        assert remaining == 0
        await db_session.rollback()
