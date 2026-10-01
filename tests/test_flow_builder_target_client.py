from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.flow_builder_target_client as target_client


@pytest.mark.asyncio
async def test_catalog_request_is_scoped_to_orchestration_and_workspace(monkeypatch) -> None:
    captured: dict = {}

    def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return target_client.TargetResponse(
            status_code=200,
            payload={"data": [{"id": "finish_flow"}]},
        )

    monkeypatch.setattr(target_client, "_request_json", fake_request_json)
    settings = SimpleNamespace(
        target_core_api_base_url="https://target.example.test",
        target_core_api_bearer_token="secret-token",
        orch_flow_builder_target_timeout_seconds=8.0,
    )

    result = await target_client.fetch_orchestration_catalog(
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        actor="user-123",
        settings=settings,
    )

    assert result == [{"id": "finish_flow"}]
    assert captured["url"] == (
        "https://target.example.test/v1/flow-catalog-tasks?mode=orchestration"
    )
    assert captured["headers"]["X-WORKSPACE-UUID"] == (
        "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
    )
    assert captured["headers"]["X-User-UUID"] == "user-123"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["timeout_seconds"] == 8.0


@pytest.mark.asyncio
async def test_catalog_request_fails_closed_without_target_configuration() -> None:
    settings = SimpleNamespace(
        target_core_api_base_url=None,
        target_core_api_bearer_token=None,
        orch_flow_builder_target_timeout_seconds=8.0,
    )
    with pytest.raises(target_client.FlowBuilderTargetError) as exc_info:
        await target_client.fetch_orchestration_catalog(
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            actor="user-123",
            settings=settings,
        )
    assert exc_info.value.code == "target_core_not_configured"
