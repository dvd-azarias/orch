from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.api.v1.orch import _trigger_orch_for_workspace
from app.core.database import get_session_factory
from app.core.workspace import workspace_schema_from_uuid

WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"


async def _ensure_flow_tables(schema: str) -> None:
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        flow_table = (
            await db_session.execute(text("SELECT to_regclass(:full_name)"), {"full_name": f"{schema}.flow_v2"})
        ).scalar_one()
        revision_table = (
            await db_session.execute(text("SELECT to_regclass(:full_name)"), {"full_name": f"{schema}.flow_v2_revision"})
        ).scalar_one()
    if flow_table is None or revision_table is None:
        pytest.skip(f"Tabelas flow_v2/flow_v2_revision não disponíveis no schema {schema}.")


async def _insert_flow_with_revision(*, schema: str, flow_uuid: str, definition: dict) -> str:
    safe_schema = schema.replace('"', '""')
    session_factory = get_session_factory()
    revision_uuid = str(uuid4())
    slug = f"test-fileapp-folder-{flow_uuid[:8]}-{uuid4().hex[:6]}"
    checksum = f"chk-{uuid4().hex}"

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    f"""
                    INSERT INTO "{safe_schema}".flow_v2 (id, slug, display_name, status, is_active)
                    VALUES (CAST(:id AS uuid), :slug, :display_name, 'draft', TRUE)
                    """
                ),
                {
                    "id": flow_uuid,
                    "slug": slug,
                    "display_name": f"Flow FileApp Folder Guard {flow_uuid[:8]}",
                },
            )
            await db_session.execute(
                text(
                    f"""
                    INSERT INTO "{safe_schema}".flow_v2_revision (
                        id,
                        flow_id,
                        version,
                        definition,
                        checksum,
                        authored_by,
                        published_at,
                        is_draft
                    )
                    VALUES (
                        CAST(:id AS uuid),
                        CAST(:flow_id AS uuid),
                        1,
                        CAST(:definition AS jsonb),
                        :checksum,
                        'tests',
                        NOW(),
                        FALSE
                    )
                    """
                ),
                {
                    "id": revision_uuid,
                    "flow_id": flow_uuid,
                    "definition": json.dumps(definition, ensure_ascii=False),
                    "checksum": checksum,
                },
            )
            await db_session.execute(
                text(
                    f"""
                    UPDATE "{safe_schema}".flow_v2
                    SET current_revision_id = CAST(:revision_id AS uuid), updated_at = NOW()
                    WHERE id = CAST(:flow_id AS uuid)
                    """
                ),
                {
                    "flow_id": flow_uuid,
                    "revision_id": revision_uuid,
                },
            )
    return revision_uuid


async def _count_sessions_for_flow(*, schema: str, flow_uuid: str) -> int:
    safe_schema = schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        value = (
            await db_session.execute(
                text(
                    f"""
                    SELECT COUNT(*)
                    FROM "{safe_schema}".orch_sessions
                    WHERE flow_uuid = CAST(:flow_uuid AS uuid)
                    """
                ),
                {"flow_uuid": flow_uuid},
            )
        ).scalar_one()
    return int(value)


async def _cleanup_flow_with_revision(*, schema: str, flow_uuid: str, revision_uuid: str) -> None:
    safe_schema = schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(f'DELETE FROM "{safe_schema}".flow_v2_revision WHERE id = CAST(:id AS uuid)'),
                {"id": revision_uuid},
            )
            await db_session.execute(
                text(f'DELETE FROM "{safe_schema}".flow_v2 WHERE id = CAST(:id AS uuid)'),
                {"id": flow_uuid},
            )


@pytest.mark.asyncio
async def test_fileapp_deve_descartar_evento_fora_da_pasta_monitorada() -> None:
    schema = workspace_schema_from_uuid(WORKSPACE_UUID)
    await _ensure_flow_tables(schema)

    flow_uuid = str(uuid4())
    definition = {
        "canvas_properties": {
            "orchestration_trigger": {
                "folder_paths": ["dev-orch/mailing/demo06"],
            }
        }
    }
    revision_uuid = await _insert_flow_with_revision(schema=schema, flow_uuid=flow_uuid, definition=definition)

    payload = {
        "EventName": "s3:ObjectCreated:Put",
        "file": {
            "id": "file-guard-01",
            "folder_path": "system/mailings",
            "original_name": "mailing.csv",
        },
    }

    try:
        before = await _count_sessions_for_flow(schema=schema, flow_uuid=flow_uuid)
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            response = await _trigger_orch_for_workspace(
                workspace_uuid=WORKSPACE_UUID,
                flow_uuid=UUID(flow_uuid),
                payload=payload,
                db_session=db_session,
                validate_workspace=False,
            )
        after = await _count_sessions_for_flow(schema=schema, flow_uuid=flow_uuid)

        assert response.accepted is False
        assert response.status == "ignored"
        assert response.persistence == "ignored"
        assert response.workflow_execution is not None
        assert response.workflow_execution["reason"] == "unmonitored_folder"
        assert before == after == 0
    finally:
        await _cleanup_flow_with_revision(schema=schema, flow_uuid=flow_uuid, revision_uuid=revision_uuid)
