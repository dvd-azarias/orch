from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from types import SimpleNamespace

import app.api.v1.orch as orch_api
import app.services.workflow_m2_service as workflow_m2_service
import app.services.workflow_runtime_service as workflow_runtime_service
from app.api.v1.orch import trigger_orch
from app.core.config import get_settings
from app.core.database import get_session_factory


async def _ensure_flow_tables() -> None:
    schema = get_settings().database_schema
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        flow_table = (
            await db_session.execute(text("SELECT to_regclass(:full_name)"), {"full_name": f"{schema}.flow_v2"})
        ).scalar_one()
        revision_table = (
            await db_session.execute(text("SELECT to_regclass(:full_name)"), {"full_name": f"{schema}.flow_v2_revision"})
        ).scalar_one()
    if flow_table is None or revision_table is None:
        pytest.skip("Tabelas flow_v2/flow_v2_revision não disponíveis no schema configurado.")


async def _ensure_alarms_table() -> None:
    schema = get_settings().database_schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            await db_session.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS "{schema}".orch_sessions_alarms (
                        id BIGSERIAL PRIMARY KEY,
                        uuid UUID NOT NULL DEFAULT gen_random_uuid(),
                        session_uuid UUID,
                        flow_uuid UUID,
                        app_name TEXT,
                        entity TEXT,
                        entity_type TEXT,
                        entity_address TEXT,
                        level TEXT NOT NULL,
                        code TEXT NOT NULL,
                        message TEXT NOT NULL,
                        details JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        request_id TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        CONSTRAINT ck_orch_sessions_alarms_level CHECK (level IN ('warning', 'error'))
                    )
                    """
                )
            )


async def _insert_flow_with_revision(*, flow_uuid: str, definition: dict) -> tuple[str, str]:
    schema = get_settings().database_schema.replace('"', '""')
    session_factory = get_session_factory()
    revision_uuid = str(uuid4())
    slug = f"test-alarm-{flow_uuid[:8]}-{uuid4().hex[:6]}"
    checksum = f"chk-{uuid4().hex}"

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".flow_v2 (id, slug, display_name, status, is_active)
                    VALUES (CAST(:id AS uuid), :slug, :display_name, 'draft', TRUE)
                    """
                ),
                {
                    "id": flow_uuid,
                    "slug": slug,
                    "display_name": f"Flow Alarm {flow_uuid[:8]}",
                },
            )
            await db_session.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".flow_v2_revision (
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
                    UPDATE "{schema}".flow_v2
                    SET current_revision_id = CAST(:revision_id AS uuid), updated_at = NOW()
                    WHERE id = CAST(:flow_id AS uuid)
                    """
                ),
                {
                    "flow_id": flow_uuid,
                    "revision_id": revision_uuid,
                },
            )

    return flow_uuid, revision_uuid


async def _cleanup_flow_with_revision(*, flow_uuid: str, revision_uuid: str) -> None:
    schema = get_settings().database_schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(f'DELETE FROM "{schema}".flow_v2_revision WHERE id = CAST(:id AS uuid)'),
                {"id": revision_uuid},
            )
            await db_session.execute(
                text(f'DELETE FROM "{schema}".flow_v2 WHERE id = CAST(:id AS uuid)'),
                {"id": flow_uuid},
            )


async def _count_alarm_by_code(*, flow_uuid: str, code: str) -> int:
    schema = get_settings().database_schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        row = (
            await db_session.execute(
                text(
                    f"""
                    SELECT COUNT(*) AS total
                    FROM "{schema}".orch_sessions_alarms
                    WHERE flow_uuid = CAST(:flow_uuid AS uuid)
                      AND code = :code
                    """
                ),
                {
                    "flow_uuid": flow_uuid,
                    "code": code,
                },
            )
        ).mappings().one()
    return int(row["total"])


@pytest.mark.asyncio
async def test_deve_persistir_warning_quando_componente_nao_suportado_no_m2(monkeypatch) -> None:
    await _ensure_flow_tables()
    await _ensure_alarms_table()

    flow_uuid = str(uuid4())
    definition = {
        "trigger_start_by_ref_id": "set-1",
        "components": [
            {
                "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "ref_id": "set-1",
                "component": "set_variables",
                "parameters": {"instructions": [{"variable": "ok", "value": "1"}]},
            },
            {
                "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "ref_id": "unsupported-1",
                "component": "foo_component",
                "parameters": {},
            },
        ],
        "branches": [
            {"from": "set-1", "to": "unsupported-1"},
        ],
    }

    inserted_flow_uuid, revision_uuid = await _insert_flow_with_revision(flow_uuid=flow_uuid, definition=definition)
    monkeypatch.setattr(workflow_runtime_service, "_read_flag_true", lambda _settings: True)
    monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(
        orch_api,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=False,
            orch_default_workspace_uuid=flow_uuid,
            orch_lab_workspace_uuid=flow_uuid,
        ),
    )
    before = await _count_alarm_by_code(flow_uuid=flow_uuid, code="workflow_m2_component_not_supported")

    try:
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            response = await trigger_orch(
                flow_uuid=UUID(flow_uuid),
                payload={"external_id": f"generic-{flow_uuid[:8]}"},
                db_session=db_session,
            )

        assert response.workflow_execution is not None
        assert str(response.workflow_execution["stopped_reason"]).startswith("component_not_supported")

        after = await _count_alarm_by_code(flow_uuid=flow_uuid, code="workflow_m2_component_not_supported")
        assert after == before + 1
    finally:
        await _cleanup_flow_with_revision(flow_uuid=inserted_flow_uuid, revision_uuid=revision_uuid)


@pytest.mark.asyncio
async def test_deve_persistir_warning_quando_m2_atinge_max_steps(monkeypatch) -> None:
    await _ensure_flow_tables()
    await _ensure_alarms_table()

    flow_uuid = str(uuid4())
    definition = {
        "trigger_start_by_ref_id": "a",
        "components": [
            {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "ref_id": "a",
                "component": "set_variables",
                "parameters": {"instructions": [{"variable": "a", "value": "1"}]},
            },
            {
                "uuid": "22222222-2222-2222-2222-222222222222",
                "ref_id": "b",
                "component": "set_variables",
                "parameters": {"instructions": [{"variable": "b", "value": "2"}]},
            },
        ],
        "branches": [
            {"from": "a", "to": "b"},
            {"from": "b", "to": "a"},
        ],
    }

    inserted_flow_uuid, revision_uuid = await _insert_flow_with_revision(flow_uuid=flow_uuid, definition=definition)
    monkeypatch.setattr(workflow_runtime_service, "_read_flag_true", lambda _settings: True)
    monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(workflow_m2_service, "_read_max_steps", lambda _settings: 2)
    monkeypatch.setattr(
        orch_api,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=False,
            orch_default_workspace_uuid=flow_uuid,
            orch_lab_workspace_uuid=flow_uuid,
        ),
    )
    before = await _count_alarm_by_code(flow_uuid=flow_uuid, code="workflow_m2_max_steps_reached")

    try:
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            response = await trigger_orch(
                flow_uuid=UUID(flow_uuid),
                payload={"external_id": f"generic-max-{flow_uuid[:8]}"},
                db_session=db_session,
            )

        assert response.workflow_execution is not None
        assert response.workflow_execution["stopped_reason"] == "max_steps_reached"

        after = await _count_alarm_by_code(flow_uuid=flow_uuid, code="workflow_m2_max_steps_reached")
        assert after == before + 1
    finally:
        await _cleanup_flow_with_revision(flow_uuid=inserted_flow_uuid, revision_uuid=revision_uuid)
