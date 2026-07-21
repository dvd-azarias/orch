from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

import app.api.v1.orch as orch_api
import app.services.orch_trigger_service as orch_trigger_service
import app.services.workflow_m2_service as workflow_m2_service
import app.services.workflow_runtime_service as workflow_runtime_service
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
    slug = f"test-wic-{flow_uuid[:8]}-{uuid4().hex[:6]}"
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
                    "display_name": f"Flow WIC Test {flow_uuid[:8]}",
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


def _whatsapp_status_payload(*, status: str, recipient: str = "5511975620806") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": recipient}],
                            "statuses": [
                                {
                                    "status": status,
                                    "id": f"wamid-{status}-1",
                                    "timestamp": "1781526584",
                                    "recipient_id": recipient,
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


def _whatsapp_message_payload(*, message_id: str, message_text: str, recipient: str = "5511975620806") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "551147371486"},
                            "contacts": [{"wa_id": recipient}],
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": recipient,
                                    "timestamp": "1781526585",
                                    "type": "text",
                                    "text": {"body": message_text},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


@pytest.mark.asyncio
async def test_send_whatsapp_interactive_blocks_waiting_for_events(monkeypatch: pytest.MonkeyPatch) -> None:
    await _ensure_flow_tables()
    settings = get_settings()
    flow_uuid = str(uuid4())
    send_uuid = "a1111111-1111-1111-1111-111111111111"
    finish_uuid = "a2222222-2222-2222-2222-222222222222"
    definition = {
        "trigger_start_by_ref_id": send_uuid,
        "components": [
            {
                "uuid": send_uuid,
                "ref_id": send_uuid,
                "component_id": "send_whatsapp_interactive",
                "parameters": {
                    "whatsapp_interactive_config": {
                        "selected_number": "1147371486",
                        "numbers": [
                            {
                                "number": "1147371486",
                                "value": {
                                    "max_daily_rate_limit_consumption": 100,
                                    "meta_payload": {"to": "{{recipient_phone_number}}", "type": "text"},
                                },
                            }
                        ],
                    }
                },
            },
            {"uuid": finish_uuid, "ref_id": finish_uuid, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [{"from": send_uuid, "to": finish_uuid, "branch": "wic:1147371486:otimo"}],
    }

    inserted_flow_uuid, revision_uuid = await _insert_flow_with_revision(flow_uuid=flow_uuid, definition=definition)
    monkeypatch.setattr(workflow_runtime_service, "_read_flag_true", lambda _settings: True)
    monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(
        orch_api,
        "get_settings",
        lambda: SimpleNamespace(celery_enabled=False, celery_fileapp_ingest_enabled=True),
    )
    monkeypatch.setattr(
        orch_trigger_service,
        "get_settings",
        lambda: SimpleNamespace(celery_enabled=False),
    )

    async def _fake_prepare(**kwargs):  # noqa: ANN001
        runtime_variables = kwargs["runtime_variables"]
        runtime_variables["send_whatsapp_interactive_routing"] = {
            "assignment": {"ani": "1147371486", "linked_actuator": "whatsapp"}
        }
        return {"ani": "1147371486", "linked_actuator": "whatsapp"}

    monkeypatch.setattr(workflow_m2_service, "_prepare_send_whatsapp_interactive_contact_member", _fake_prepare)

    try:
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            response = await orch_api._trigger_orch_for_workspace(
                workspace_uuid=settings.orch_lab_workspace_uuid,
                flow_uuid=UUID(flow_uuid),
                payload=_whatsapp_status_payload(status="sent"),
                db_session=db_session,
                validate_workspace=False,
                schema_override=settings.database_schema,
            )

        assert response.workflow_execution is not None
        assert response.workflow_execution["mode"] == "inline_fallback"
        assert response.workflow_execution["stopped_reason"] == "blocked_send_whatsapp_interactive"

        row = await _get_session_state_row(response.session_id)
        assert row["last_card_uuid"] == send_uuid
        assert row["next_card_uuid"] == send_uuid
        runtime = row["runtime_variables"]
        assert runtime["workflow_v2"]["blocking_stop_reason"] == "blocked_send_whatsapp_interactive"
        assert runtime["send_whatsapp_interactive_last_error"]["code"] == "send_whatsapp_interactive_branch_not_found"
    finally:
        await _cleanup_flow_with_revision(flow_uuid=inserted_flow_uuid, revision_uuid=revision_uuid)


@pytest.mark.asyncio
async def test_send_whatsapp_interactive_resumes_on_message_and_routes_wic_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    await _ensure_flow_tables()
    settings = get_settings()
    flow_uuid = str(uuid4())
    send_uuid = "b1111111-1111-1111-1111-111111111111"
    finish_uuid = "b2222222-2222-2222-2222-222222222222"
    definition = {
        "trigger_start_by_ref_id": send_uuid,
        "components": [
            {
                "uuid": send_uuid,
                "ref_id": send_uuid,
                "component_id": "send_whatsapp_interactive",
                "parameters": {
                    "whatsapp_interactive_config": {
                        "selected_number": "1147371486",
                        "numbers": [
                            {
                                "number": "1147371486",
                                "value": {
                                    "max_daily_rate_limit_consumption": 100,
                                    "meta_payload": {"to": "{{recipient_phone_number}}", "type": "text"},
                                },
                            }
                        ],
                    }
                },
            },
            {"uuid": finish_uuid, "ref_id": finish_uuid, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [{"from": send_uuid, "to": finish_uuid, "branch": "wic:1147371486:otimo"}],
    }

    inserted_flow_uuid, revision_uuid = await _insert_flow_with_revision(flow_uuid=flow_uuid, definition=definition)
    monkeypatch.setattr(workflow_runtime_service, "_read_flag_true", lambda _settings: True)
    monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(
        orch_api,
        "get_settings",
        lambda: SimpleNamespace(celery_enabled=False, celery_fileapp_ingest_enabled=True),
    )
    monkeypatch.setattr(
        orch_trigger_service,
        "get_settings",
        lambda: SimpleNamespace(celery_enabled=False),
    )

    async def _fake_prepare(**kwargs):  # noqa: ANN001
        runtime_variables = kwargs["runtime_variables"]
        runtime_variables["send_whatsapp_interactive_routing"] = {
            "assignment": {"ani": "1147371486", "linked_actuator": "whatsapp"}
        }
        return {"ani": "1147371486", "linked_actuator": "whatsapp"}

    monkeypatch.setattr(workflow_m2_service, "_prepare_send_whatsapp_interactive_contact_member", _fake_prepare)

    try:
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            first = await orch_api._trigger_orch_for_workspace(
                workspace_uuid=settings.orch_lab_workspace_uuid,
                flow_uuid=UUID(flow_uuid),
                payload=_whatsapp_status_payload(status="sent"),
                db_session=db_session,
                validate_workspace=False,
                schema_override=settings.database_schema,
            )

        async with session_factory() as db_session:
            second = await orch_api._trigger_orch_for_workspace(
                workspace_uuid=settings.orch_lab_workspace_uuid,
                flow_uuid=UUID(flow_uuid),
                payload=_whatsapp_message_payload(message_id="wamid-msg-1", message_text="otimo"),
                db_session=db_session,
                validate_workspace=False,
                schema_override=settings.database_schema,
            )

        assert second.session_id == first.session_id
        assert second.workflow_execution is not None
        assert second.workflow_execution["stopped_reason"] == "finished_by_component"
        row = await _get_session_state_row(second.session_id)
        assert row["state"] == 3
        assert row["ended_at"] is not None
    finally:
        await _cleanup_flow_with_revision(flow_uuid=inserted_flow_uuid, revision_uuid=revision_uuid)


@pytest.mark.asyncio
async def test_send_whatsapp_template_accepts_plain_status_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    await _ensure_flow_tables()
    settings = get_settings()
    flow_uuid = str(uuid4())
    send_uuid = "c1111111-1111-1111-1111-111111111111"
    finish_uuid = "c2222222-2222-2222-2222-222222222222"
    definition = {
        "trigger_start_by_ref_id": send_uuid,
        "components": [
            {
                "uuid": send_uuid,
                "ref_id": send_uuid,
                "component_id": "send_whatsapp_template",
                "parameters": {
                    "whatsapp_interactive_config": {
                        "selected_number": "1147371486",
                        "numbers": [
                            {
                                "number": "1147371486",
                                "value": {
                                    "max_daily_rate_limit_consumption": 100,
                                    "meta_payload": {"to": "{{recipient_phone_number}}", "type": "text"},
                                },
                            }
                        ],
                    }
                },
            },
            {"uuid": finish_uuid, "ref_id": finish_uuid, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [{"from": send_uuid, "to": finish_uuid, "branch": "sent"}],
    }

    inserted_flow_uuid, revision_uuid = await _insert_flow_with_revision(flow_uuid=flow_uuid, definition=definition)
    monkeypatch.setattr(workflow_runtime_service, "_read_flag_true", lambda _settings: True)
    monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(
        orch_api,
        "get_settings",
        lambda: SimpleNamespace(celery_enabled=False, celery_fileapp_ingest_enabled=True),
    )
    monkeypatch.setattr(
        orch_trigger_service,
        "get_settings",
        lambda: SimpleNamespace(celery_enabled=False),
    )

    async def _fake_prepare(**kwargs):  # noqa: ANN001
        runtime_variables = kwargs["runtime_variables"]
        runtime_variables["send_whatsapp_interactive_routing"] = {
            "assignment": {"ani": "1147371486", "linked_actuator": "whatsapp"}
        }
        return {"ani": "1147371486", "linked_actuator": "whatsapp"}

    monkeypatch.setattr(workflow_m2_service, "_prepare_send_whatsapp_interactive_contact_member", _fake_prepare)

    try:
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            response = await orch_api._trigger_orch_for_workspace(
                workspace_uuid=settings.orch_lab_workspace_uuid,
                flow_uuid=UUID(flow_uuid),
                payload=_whatsapp_status_payload(status="sent"),
                db_session=db_session,
                validate_workspace=False,
                schema_override=settings.database_schema,
            )
        assert response.workflow_execution is not None
        assert response.workflow_execution["stopped_reason"] == "finished_by_component"
        row = await _get_session_state_row(response.session_id)
        assert row["state"] == 3
        assert row["ended_at"] is not None
    finally:
        await _cleanup_flow_with_revision(flow_uuid=inserted_flow_uuid, revision_uuid=revision_uuid)
