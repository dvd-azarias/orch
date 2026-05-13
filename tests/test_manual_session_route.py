from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.api.v1.orch as orch_api
from app.schemas.orch import OrchCreateSessionRequest
from app.services.session_service import SessionPersistResponse
from app.services.workflow_runtime_service import WorkflowBootstrapResult


@pytest.mark.asyncio
async def test_create_orch_session_by_workspace_uses_explicit_fields(monkeypatch) -> None:
    captured: dict = {}

    def _fake_bind_workspace_context(_workspace_uuid: str):  # type: ignore[no-untyped-def]
        return "ba7eb0ec-e565-447c-8c11-8f870cf72a60", "ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60"

    async def _fake_ensure_active_workspace(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def _fake_persist_session(db_session, *, flow_uuid, app_name, extracted, payload):  # type: ignore[no-untyped-def]
        captured["flow_uuid"] = flow_uuid
        captured["app_name"] = app_name
        captured["extracted"] = extracted
        captured["payload"] = payload
        return SessionPersistResponse(
            session_id=999,
            session_uuid="11111111-1111-1111-1111-111111111111",
            session_state=0,
            session_created=True,
        )

    async def _fake_bootstrap(*args, **kwargs):  # type: ignore[no-untyped-def]
        return WorkflowBootstrapResult(
            enabled=True,
            loaded=False,
            reason="flow_not_found",
            flow_id=None,
            revision_id=None,
            revision_version=None,
            revision_mode=None,
            next_card_uuid=None,
        )

    async def _fake_persist_alarm(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(orch_api, "bind_workspace_context", _fake_bind_workspace_context)
    monkeypatch.setattr(orch_api, "ensure_active_workspace", _fake_ensure_active_workspace)
    monkeypatch.setattr(orch_api, "persist_session", _fake_persist_session)
    monkeypatch.setattr(orch_api, "bootstrap_workflow_for_session", _fake_bootstrap)
    monkeypatch.setattr(orch_api, "persist_alarm", _fake_persist_alarm)
    monkeypatch.setattr(orch_api, "get_settings", lambda: SimpleNamespace(celery_enabled=False))

    response = await orch_api.create_orch_session_by_workspace(
        workspace_uuid=uuid4(),
        flow_uuid=uuid4(),
        request=OrchCreateSessionRequest(
            app_name="GenericApp",
            entity="30392287843",
            entity_type="person",
            entity_address="5511975620806",
            entity_session_id="30392287843",
            payload={"origin": "third_party_app"},
        ),
        db_session=object(),  # type: ignore[arg-type]
    )

    assert response.accepted is True
    assert response.status == "accepted"
    assert response.persistence == "saved"
    assert response.session_id == 999
    assert response.extracted.entity == "30392287843"
    assert response.extracted.entity_session_id == "30392287843"
    assert captured["app_name"] == "GenericApp"
    assert captured["extracted"]["entity_address"] == "5511975620806"
    assert captured["payload"] == {"origin": "third_party_app"}


@pytest.mark.asyncio
async def test_create_orch_session_by_workspace_rejects_blank_entity_session_id(monkeypatch) -> None:
    def _fake_bind_workspace_context(_workspace_uuid: str):  # type: ignore[no-untyped-def]
        return "ba7eb0ec-e565-447c-8c11-8f870cf72a60", "ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60"

    async def _fake_ensure_active_workspace(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(orch_api, "bind_workspace_context", _fake_bind_workspace_context)
    monkeypatch.setattr(orch_api, "ensure_active_workspace", _fake_ensure_active_workspace)

    with pytest.raises(HTTPException) as exc_info:
        await orch_api.create_orch_session_by_workspace(
            workspace_uuid=uuid4(),
            flow_uuid=uuid4(),
            request=OrchCreateSessionRequest(
                app_name="GenericApp",
                entity="abc",
                entity_type="person",
                entity_address="addr",
                entity_session_id="   ",
                payload=None,
            ),
            db_session=object(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 422
    assert "entity_session_id" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_create_orch_session_by_workspace_rejects_invalid_app_name(monkeypatch) -> None:
    def _fake_bind_workspace_context(_workspace_uuid: str):  # type: ignore[no-untyped-def]
        return "ba7eb0ec-e565-447c-8c11-8f870cf72a60", "ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60"

    async def _fake_ensure_active_workspace(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(orch_api, "bind_workspace_context", _fake_bind_workspace_context)
    monkeypatch.setattr(orch_api, "ensure_active_workspace", _fake_ensure_active_workspace)

    with pytest.raises(HTTPException) as exc_info:
        await orch_api.create_orch_session_by_workspace(
            workspace_uuid=uuid4(),
            flow_uuid=uuid4(),
            request=OrchCreateSessionRequest(
                app_name="ThirdPartyApp",
                entity="abc",
                entity_type="person",
                entity_address="addr",
                entity_session_id="abc",
                payload=None,
            ),
            db_session=object(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 422
    assert "app_name inválido" in str(exc_info.value.detail)
