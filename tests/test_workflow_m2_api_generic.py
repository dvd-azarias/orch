from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

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


async def _insert_flow_with_revision(*, flow_uuid: str, definition: dict) -> tuple[str, str]:
    schema = get_settings().database_schema.replace('"', '""')
    session_factory = get_session_factory()
    revision_uuid = str(uuid4())
    slug = f"test-flow-{flow_uuid[:8]}-{uuid4().hex[:6]}"
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
                    "display_name": f"Flow Test {flow_uuid[:8]}",
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


async def _get_session_state_row(session_id: int) -> dict:
    schema = get_settings().database_schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        row = (
            await db_session.execute(
                text(
                    f"""
                    SELECT
                        id,
                        state,
                        ended_at,
                        frozen_until,
                        last_card_uuid::text AS last_card_uuid,
                        next_card_uuid::text AS next_card_uuid,
                        runtime_variables
                    FROM "{schema}".orch_sessions
                    WHERE id = :session_id
                    LIMIT 1
                    """
                ),
                {"session_id": session_id},
            )
        ).mappings().first()
    return dict(row) if row is not None else {}


@pytest.mark.asyncio
async def test_deve_executar_workflow_m2_ate_finish_por_trigger_genericapp(monkeypatch) -> None:
    await _ensure_flow_tables()

    flow_uuid = str(uuid4())
    set_uuid = "11111111-1111-1111-1111-111111111111"
    api_uuid = "22222222-2222-2222-2222-222222222222"
    finish_uuid = "33333333-3333-3333-3333-333333333333"
    definition = {
        "trigger_start_by_ref_id": "set-1",
        "components": [
            {
                "uuid": set_uuid,
                "ref_id": "set-1",
                "component": "set_variables",
                "parameters": {
                    "instructions": [
                        {"variable": "resultado", "source_type": "variable", "value": "payload.valor_recebido"},
                    ]
                },
            },
            {
                "uuid": api_uuid,
                "ref_id": "api-1",
                "component": "api_call",
                "parameters": {
                    "request": {
                        "url": "https://example.test/hook",
                        "method": "POST",
                        "timeout": 500,
                        "body": {
                            "mode": "json",
                            "json": {"external_id": "{{payload.external_id}}", "valor": "{{resultado}}"},
                        },
                        "response": {
                            "status": "api_status",
                            "body": "api_body",
                            "error": "api_error",
                        },
                    }
                },
            },
            {
                "uuid": finish_uuid,
                "ref_id": "finish-1",
                "component": "finish_flow",
                "parameters": {},
            },
        ],
        "branches": [
            {"from": "set-1", "to": "api-1"},
            {"from": "api-1", "to": "finish-1", "branch": "success"},
            {"from": "api-1", "to": "finish-1", "branch": "error"},
        ],
    }

    inserted_flow_uuid, revision_uuid = await _insert_flow_with_revision(flow_uuid=flow_uuid, definition=definition)
    monkeypatch.setattr(workflow_runtime_service, "_read_flag_true", lambda _settings: True)
    monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(
        orch_api,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=True,
            orch_default_workspace_uuid=flow_uuid,
            orch_lab_workspace_uuid=flow_uuid,
        ),
    )

    enqueued: list[dict] = []

    def _fake_delay(*, flow_uuid: str, session_id: int):  # noqa: ANN001
        enqueued.append({"flow_uuid": flow_uuid, "session_id": session_id})

    monkeypatch.setattr(orch_api.advance_session_task, "delay", _fake_delay)

    payload = {"external_id": f"generic-{flow_uuid[:8]}", "valor_recebido": 114}

    try:
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            response = await trigger_orch(flow_uuid=UUID(flow_uuid), payload=payload, db_session=db_session)

        assert response.app == "GenericApp"
        assert response.workflow_bootstrap is not None
        assert response.workflow_bootstrap["loaded"] is True
        assert response.workflow_execution is not None
        assert response.workflow_execution["mode"] == "async"
        assert response.workflow_execution["enqueued"] is True
        assert len(enqueued) == 1
        assert enqueued[0]["flow_uuid"] == flow_uuid
        assert enqueued[0]["session_id"] == response.session_id

        row = await _get_session_state_row(response.session_id)
        assert row["state"] == 0
        assert row["ended_at"] is None
        assert row["frozen_until"] is None
        assert row["last_card_uuid"] is None
        assert row["next_card_uuid"] == set_uuid

        runtime = row["runtime_variables"]
        assert runtime["input_payload"]["valor_recebido"] == 114
    finally:
        await _cleanup_flow_with_revision(flow_uuid=inserted_flow_uuid, revision_uuid=revision_uuid)


@pytest.mark.asyncio
async def test_deve_congelar_execucao_em_wait_por_trigger_genericapp(monkeypatch) -> None:
    await _ensure_flow_tables()

    flow_uuid = str(uuid4())
    set_uuid = "44444444-4444-4444-4444-444444444444"
    wait_uuid = "55555555-5555-5555-5555-555555555555"
    finish_uuid = "66666666-6666-6666-6666-666666666666"
    definition = {
        "trigger_start_by_ref_id": "set-w",
        "components": [
            {
                "uuid": set_uuid,
                "ref_id": "set-w",
                "component": "set_variables",
                "parameters": {"instructions": [{"variable": "ok_wait", "value": "1"}]},
            },
            {
                "uuid": wait_uuid,
                "ref_id": "wait-w",
                "component": "scheduling_moment",
                "parameters": {"delay_in_seconds": 1},
            },
            {
                "uuid": finish_uuid,
                "ref_id": "finish-w",
                "component": "finish_flow",
                "parameters": {},
            },
        ],
        "branches": [
            {"from": "set-w", "to": "wait-w"},
            {"from": "wait-w", "to": "finish-w"},
        ],
    }

    inserted_flow_uuid, revision_uuid = await _insert_flow_with_revision(flow_uuid=flow_uuid, definition=definition)
    monkeypatch.setattr(workflow_runtime_service, "_read_flag_true", lambda _settings: True)
    monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(
        orch_api,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=True,
            orch_default_workspace_uuid=flow_uuid,
            orch_lab_workspace_uuid=flow_uuid,
        ),
    )
    monkeypatch.setattr(orch_api.advance_session_task, "delay", lambda **kwargs: None)

    payload = {"external_id": f"generic-wait-{flow_uuid[:8]}", "valor_recebido": 7}

    try:
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            response = await trigger_orch(flow_uuid=UUID(flow_uuid), payload=payload, db_session=db_session)

        assert response.app == "GenericApp"
        assert response.workflow_execution is not None
        assert response.workflow_execution["mode"] == "async"
        assert response.workflow_execution["enqueued"] is True

        row = await _get_session_state_row(response.session_id)
        assert row["state"] == 0
        assert row["ended_at"] is None
        assert row["last_card_uuid"] is None
        assert row["next_card_uuid"] == set_uuid
        assert row["frozen_until"] is None
    finally:
        await _cleanup_flow_with_revision(flow_uuid=inserted_flow_uuid, revision_uuid=revision_uuid)


@pytest.mark.asyncio
async def test_deve_preservar_bootstrap_e_respeitar_frozen_until_em_trigger_seguinte(monkeypatch) -> None:
    await _ensure_flow_tables()

    flow_uuid = str(uuid4())
    set_uuid = "77777777-7777-7777-7777-777777777777"
    wait_uuid = "88888888-8888-8888-8888-888888888888"
    finish_uuid = "99999999-9999-9999-9999-999999999999"
    definition = {
        "trigger_start_by_ref_id": "set-rewait",
        "components": [
            {
                "uuid": set_uuid,
                "ref_id": "set-rewait",
                "component": "set_variables",
                "parameters": {"instructions": [{"variable": "ok_wait", "value": "1"}]},
            },
            {
                "uuid": wait_uuid,
                "ref_id": "wait-rewait",
                "component": "scheduling_moment",
                "parameters": {"delay_in_seconds": 20},
            },
            {
                "uuid": finish_uuid,
                "ref_id": "finish-rewait",
                "component": "finish_flow",
                "parameters": {},
            },
        ],
        "branches": [
            {"from": "set-rewait", "to": "wait-rewait"},
            {"from": "wait-rewait", "to": "finish-rewait", "branch": "next"},
        ],
    }

    inserted_flow_uuid, revision_uuid = await _insert_flow_with_revision(flow_uuid=flow_uuid, definition=definition)
    monkeypatch.setattr(workflow_runtime_service, "_read_flag_true", lambda _settings: True)
    monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(
        orch_api,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=True,
            orch_default_workspace_uuid=flow_uuid,
            orch_lab_workspace_uuid=flow_uuid,
        ),
    )
    monkeypatch.setattr(orch_api.advance_session_task, "delay", lambda **kwargs: None)

    payload = {"external_id": f"generic-rewait-{flow_uuid[:8]}", "valor_recebido": 7}

    try:
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            first = await trigger_orch(flow_uuid=UUID(flow_uuid), payload=payload, db_session=db_session)
        assert first.workflow_execution is not None
        assert first.workflow_execution["mode"] == "async"

        async with session_factory() as db_session:
            second = await trigger_orch(flow_uuid=UUID(flow_uuid), payload=payload, db_session=db_session)

        assert second.session_id == first.session_id
        assert second.workflow_bootstrap is not None
        assert second.workflow_bootstrap["reason"] == "already_bootstrapped"
        assert second.workflow_execution is not None
        assert second.workflow_execution["mode"] == "async"
    finally:
        await _cleanup_flow_with_revision(flow_uuid=inserted_flow_uuid, revision_uuid=revision_uuid)
