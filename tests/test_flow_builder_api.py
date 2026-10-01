from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.v1.orch_flow_builder as flow_builder_api
from app.main import app


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
    assert not any(
        path.startswith("/v1/orch/{workspace_uuid}/flow-builder") and path.endswith("/publish")
        for path in paths
    )
