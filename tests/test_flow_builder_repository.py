from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.core.request_context import set_workspace_context
from app.repositories.orch_flow_builder_repository import (
    FlowBuilderVersionConflictError,
    apply_flow_builder_assistant_turn,
    append_flow_builder_message,
    create_flow_builder_session,
    fetch_flow_builder_session,
    lock_flow_builder_session_for_draft,
    store_flow_builder_draft_result,
    store_flow_builder_compilation,
)
from app.services.migration_service import _run_migration_file


@pytest.mark.asyncio
async def test_flow_builder_repository_persists_order_and_rejects_stale_version() -> None:
    workspace_uuid = str(uuid4())
    schema = f"orch_flow_builder_repository_test_{uuid4().hex}"
    safe_schema = schema.replace('"', '""')
    set_workspace_context(workspace_uuid=workspace_uuid, workspace_schema=schema)
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            await db_session.execute(text(f'CREATE SCHEMA "{safe_schema}"'))
            await _run_migration_file(
                db_session,
                schema=schema,
                migration_path="sql/028_create_orch_flow_builder_tables.sql",
            )
            created = await create_flow_builder_session(
                db_session,
                workspace_uuid=workspace_uuid,
                intent="create",
                flow_uuid=None,
                created_by="user-123",
                initial_message="Crie um fluxo de cobrança.",
            )
            session_id = str(created["id"])

            first = await fetch_flow_builder_session(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=session_id,
            )
            assert first["version"] == 1
            assert [message["sequence"] for message in first["messages"]] == [1]

            await append_flow_builder_message(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=session_id,
                role="assistant",
                content="Qual deve ser o desfecho?",
            )
            second = await fetch_flow_builder_session(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=session_id,
            )
            assert second["version"] == 2
            assert [message["sequence"] for message in second["messages"]] == [1, 2]

            await store_flow_builder_compilation(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=session_id,
                expected_version=2,
                plan={"name": "Cobrança"},
                compiled_definition={"mode": "orchestration"},
                issues=[],
            )
            ready = await fetch_flow_builder_session(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=session_id,
            )
            assert ready["status"] == "ready"
            assert ready["version"] == 3

            with pytest.raises(FlowBuilderVersionConflictError):
                await store_flow_builder_compilation(
                    db_session,
                    workspace_uuid=workspace_uuid,
                    session_id=session_id,
                    expected_version=2,
                    plan={"name": "Versão obsoleta"},
                    compiled_definition=None,
                    issues=[],
                )

            await apply_flow_builder_assistant_turn(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=session_id,
                expected_version=3,
                user_content="Inclua uma pergunta.",
                assistant_content="Qual resultado deseja?",
                assistant_payload={"planner_outcome": {"status": "needs_input"}},
                plan={"name": "Cobrança parcial"},
                compiled_definition=None,
                issues=[{"severity": "warning", "code": "pending"}],
            )
            planning = await fetch_flow_builder_session(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=session_id,
            )
            assert planning["version"] == 4
            assert planning["status"] == "planning"
            assert [message["sequence"] for message in planning["messages"]] == [1, 2, 3, 4]
            assert planning["messages"][-1]["structured_payload"]["planner_outcome"]["status"] == (
                "needs_input"
            )

            await store_flow_builder_compilation(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=session_id,
                expected_version=4,
                plan={"name": "Cobrança final"},
                compiled_definition={"mode": "orchestration"},
                issues=[],
            )
            locked = await lock_flow_builder_session_for_draft(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=session_id,
            )
            assert locked["version"] == 5
            await store_flow_builder_draft_result(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=session_id,
                expected_version=5,
                flow_uuid=str(uuid4()),
                draft_checksum="c" * 64,
            )
            saved = await fetch_flow_builder_session(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=session_id,
            )
            assert saved["status"] == "saved"
            assert saved["version"] == 6
            assert saved["draft_checksum"] == "c" * 64
        finally:
            await transaction.rollback()
