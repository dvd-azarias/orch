from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

import app.api.v1.orch_flow_builder as flow_builder_api
from app.main import app
from app.schemas.orch_flow_builder import FlowBuilderMessageCreateRequest


HIGHCOMM_WORKSPACE = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"


def _settings(**overrides):  # type: ignore[no-untyped-def]
    values = {
        "orch_flow_builder_enabled": True,
        "orch_flow_builder_client_id": "flow-builder-ui",
        "orch_flow_builder_client_secret": "dedicated-secret",
        "orch_flow_builder_workspace_allowlist": (HIGHCOMM_WORKSPACE,),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_flow_builder_auth_fails_closed_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        flow_builder_api,
        "get_settings",
        lambda: _settings(orch_flow_builder_enabled=False),
    )
    with pytest.raises(HTTPException) as exc_info:
        flow_builder_api.require_flow_builder_client(
            client_id="flow-builder-ui",
            client_secret="dedicated-secret",
        )
    assert exc_info.value.status_code == 503


def test_flow_builder_auth_rejects_wrong_secret(monkeypatch) -> None:
    monkeypatch.setattr(flow_builder_api, "get_settings", lambda: _settings())
    with pytest.raises(HTTPException) as exc_info:
        flow_builder_api.require_flow_builder_client(
            client_id="flow-builder-ui",
            client_secret="wrong",
        )
    assert exc_info.value.status_code == 401


def test_flow_builder_auth_accepts_dedicated_credentials(monkeypatch) -> None:
    monkeypatch.setattr(flow_builder_api, "get_settings", lambda: _settings())
    assert (
        flow_builder_api.require_flow_builder_client(
            client_id="flow-builder-ui",
            client_secret="dedicated-secret",
        )
        == "flow-builder-ui"
    )


def test_flow_builder_workspace_allowlist_is_fail_closed() -> None:
    with pytest.raises(HTTPException) as exc_info:
        flow_builder_api._ensure_workspace_allowed(
            "11497cd6-0332-49bb-a9f9-de0addca0114",
            _settings(),
        )
    assert exc_info.value.status_code == 403


def test_flow_builder_rejects_blank_actor() -> None:
    with pytest.raises(HTTPException) as exc_info:
        flow_builder_api._normalize_actor("   ")
    assert exc_info.value.status_code == 422


def test_flow_builder_routes_are_registered_without_publish_endpoint() -> None:
    paths = {route.path for route in app.routes}
    base = "/v1/orch/{workspace_uuid}/flow-builder/sessions"
    assert base in paths
    assert f"{base}/{{session_id}}" in paths
    assert f"{base}/{{session_id}}/messages" in paths
    assert f"{base}/{{session_id}}/compile" in paths
    assert f"{base}/{{session_id}}/assist" in paths
    assert f"{base}/{{session_id}}/draft" in paths
    assert not any(
        path.startswith("/v1/orch/{workspace_uuid}/flow-builder") and path.endswith("/publish")
        for path in paths
    )


def test_flow_builder_create_route_precedes_generic_flow_sessions_route() -> None:
    paths = [route.path for route in app.routes]
    builder_path = "/v1/orch/{workspace_uuid}/flow-builder/sessions"
    generic_path = "/v1/orch/{workspace_uuid}/{flow_uuid}/sessions"

    assert paths.index(builder_path) < paths.index(generic_path)


@pytest.mark.asyncio
async def test_message_endpoint_rejects_image_attachment_before_database_access() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await flow_builder_api.append_builder_message(
            workspace_uuid=UUID(HIGHCOMM_WORKSPACE),
            session_id=UUID("62aebf8a-abca-43a4-9815-e1f047d32e76"),
            payload=FlowBuilderMessageCreateRequest(
                content="Use este diagrama.",
                attachment_metadata={"mime_type": "image/png"},
            ),
            db_session=None,  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_get_session_no_longer_references_message_payload(monkeypatch) -> None:
    async def fake_bind(_db_session, *, workspace_uuid):  # type: ignore[no-untyped-def]
        return str(workspace_uuid)

    async def fake_fetch(_db_session, *, workspace_uuid, session_id):  # type: ignore[no-untyped-def]
        return {
            "id": session_id,
            "workspace_uuid": workspace_uuid,
            "mode": "orchestration",
            "intent": "create",
            "status": "planning",
            "flow_uuid": None,
            "draft_checksum": None,
            "version": 1,
            "plan": {},
            "compiled_definition": None,
            "issues": [],
            "created_by": "user-123",
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:00:00Z",
            "messages": [],
        }

    monkeypatch.setattr(flow_builder_api, "_bind_builder_workspace", fake_bind)
    monkeypatch.setattr(flow_builder_api, "fetch_flow_builder_session", fake_fetch)
    result = await flow_builder_api.get_builder_session(
        workspace_uuid=UUID(HIGHCOMM_WORKSPACE),
        session_id=UUID("62aebf8a-abca-43a4-9815-e1f047d32e76"),
        db_session=None,  # type: ignore[arg-type]
    )
    assert result.version == 1
