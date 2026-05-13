from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.api.v1.orch as orch_api
from app.schemas.orch import OrchCreateSessionRequest
from app.services.session_service import SessionPersistResponse
from app.services.workflow_runtime_service import WorkflowBootstrapResult


def test_create_session_request_forbids_entity_session_id_field() -> None:
    with pytest.raises(ValidationError):
        OrchCreateSessionRequest(
            app_name="GenericApp",
            entity="abc",
            entity_type="person",
            entity_address="5511999999999",
            entity_session_id="manual-value",  # type: ignore[call-arg]
            payload=None,
        )


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

    async def _fake_set_session_assigned_at_default(db_session, *, session_id):  # type: ignore[no-untyped-def]
        captured["assigned_session_id"] = session_id

    class _DummyTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _DummySession:
        def in_transaction(self) -> bool:
            return False

        def begin(self):
            return _DummyTx()

        def begin_nested(self):
            return _DummyTx()

        async def execute(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(orch_api, "bind_workspace_context", _fake_bind_workspace_context)
    monkeypatch.setattr(orch_api, "ensure_active_workspace", _fake_ensure_active_workspace)
    monkeypatch.setattr(orch_api, "persist_session", _fake_persist_session)
    monkeypatch.setattr(orch_api, "bootstrap_workflow_for_session", _fake_bootstrap)
    monkeypatch.setattr(orch_api, "persist_alarm", _fake_persist_alarm)
    monkeypatch.setattr(orch_api, "set_session_assigned_at_default", _fake_set_session_assigned_at_default)
    monkeypatch.setattr(orch_api, "get_settings", lambda: SimpleNamespace(celery_enabled=False))

    flow_uuid = uuid4()
    response = await orch_api.create_orch_session_by_workspace(
        workspace_uuid=uuid4(),
        flow_uuid=flow_uuid,
        request=OrchCreateSessionRequest(
            app_name="GenericApp",
            entity="30392287843",
            entity_type="person",
            entity_address="5511975620806",
            payload={"origin": "third_party_app"},
        ),
        db_session=_DummySession(),  # type: ignore[arg-type]
    )

    assert response.accepted is True
    assert response.status == "accepted"
    assert response.persistence == "saved"
    assert response.session_id == 999
    assert response.extracted.entity == "30392287843"
    assert response.extracted.entity_session_id == f"5511975620806:::{str(flow_uuid)}"
    assert captured["app_name"] == "GenericApp"
    assert captured["extracted"]["entity_address"] == "5511975620806"
    assert captured["payload"] == {"origin": "third_party_app"}
    assert captured["assigned_session_id"] == 999


@pytest.mark.asyncio
async def test_create_orch_session_by_workspace_rejects_blank_entity_address(monkeypatch) -> None:
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
                entity_address="   ",
                payload=None,
            ),
            db_session=object(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 422
    assert "entity_address" in str(exc_info.value.detail)


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
                payload=None,
            ),
            db_session=object(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 422
    assert "app_name inválido" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_unassign_orch_sessions_by_entity_address_updates_rows(monkeypatch) -> None:
    captured: dict = {}

    def _fake_bind_workspace_context(_workspace_uuid: str):  # type: ignore[no-untyped-def]
        return "ba7eb0ec-e565-447c-8c11-8f870cf72a60", "ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60"

    async def _fake_ensure_active_workspace(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def _fake_set_unassigned(db_session, *, flow_uuid, entity_address):  # type: ignore[no-untyped-def]
        captured["flow_uuid"] = flow_uuid
        captured["entity_address"] = entity_address
        return 3

    class _DummyTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _DummySession:
        def in_transaction(self) -> bool:
            return False

        def begin(self):
            return _DummyTx()

        def begin_nested(self):
            return _DummyTx()

        async def execute(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(orch_api, "bind_workspace_context", _fake_bind_workspace_context)
    monkeypatch.setattr(orch_api, "ensure_active_workspace", _fake_ensure_active_workspace)
    monkeypatch.setattr(orch_api, "set_unassigned_at_by_flow_and_entity_address", _fake_set_unassigned)

    flow_uuid = uuid4()
    response = await orch_api.unassign_orch_sessions_by_entity_address(
        workspace_uuid=uuid4(),
        flow_uuid=flow_uuid,
        request=orch_api.OrchUnassignSessionRequest(entity_address="5511975620806"),
        db_session=_DummySession(),  # type: ignore[arg-type]
    )

    assert response.status == "updated"
    assert response.updated_count == 3
    assert captured["entity_address"] == "5511975620806"
    assert captured["flow_uuid"] == str(flow_uuid)


@pytest.mark.asyncio
async def test_unassign_orch_sessions_by_entity_address_rejects_blank_entity_address(monkeypatch) -> None:
    def _fake_bind_workspace_context(_workspace_uuid: str):  # type: ignore[no-untyped-def]
        return "ba7eb0ec-e565-447c-8c11-8f870cf72a60", "ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60"

    async def _fake_ensure_active_workspace(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(orch_api, "bind_workspace_context", _fake_bind_workspace_context)
    monkeypatch.setattr(orch_api, "ensure_active_workspace", _fake_ensure_active_workspace)

    with pytest.raises(HTTPException) as exc_info:
        await orch_api.unassign_orch_sessions_by_entity_address(
            workspace_uuid=uuid4(),
            flow_uuid=uuid4(),
            request=orch_api.OrchUnassignSessionRequest(entity_address="   "),
            db_session=object(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 422
    assert "entity_address" in str(exc_info.value.detail)
