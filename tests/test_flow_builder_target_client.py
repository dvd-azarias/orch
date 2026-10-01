from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

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


def _target_settings() -> SimpleNamespace:
    return SimpleNamespace(
        target_core_api_base_url="https://target.example.test",
        target_core_api_bearer_token="secret-token",
        orch_flow_builder_target_timeout_seconds=8.0,
    )


@pytest.mark.asyncio
async def test_create_draft_uses_deterministic_slug_and_builder_identity(monkeypatch) -> None:
    captured: dict = {}
    flow_uuid = "f77b70f0-849b-4d11-9ccc-449b3c4ba981"
    session_id = "62aebf8a-abca-43a4-9815-e1f047d32e76"

    def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        definition = kwargs["payload"]["definition"]
        return target_client.TargetResponse(
            status_code=201,
            payload={
                "data": {
                    "summary": {"id": flow_uuid, "slug": f"orch-ai-{session_id}"},
                    "draft_revision": {
                        "checksum": "a" * 64,
                        "definition": definition,
                    },
                }
            },
        )

    monkeypatch.setattr(target_client, "_request_json", fake_request_json)
    result = await target_client.create_orchestration_draft(
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        actor="user-123",
        builder_session_id=session_id,
        definition={
            "info": {"name": "Canário IA", "description": "Teste"},
            "mode": "orchestration",
            "builder_metadata": {"plan_id": "plan-1"},
        },
        settings=_target_settings(),
    )

    assert result.flow_uuid == UUID(flow_uuid)
    assert result.draft_checksum == "a" * 64
    assert captured["url"] == "https://target.example.test/v2/flow"
    assert captured["payload"]["slug"] == f"orch-ai-{session_id}"
    assert (
        captured["payload"]["definition"]["builder_metadata"]["builder_session_id"]
        == session_id
    )


@pytest.mark.asyncio
async def test_create_draft_recovers_same_target_draft_after_slug_conflict(monkeypatch) -> None:
    session_id = "62aebf8a-abca-43a4-9815-e1f047d32e76"
    slug = f"orch-ai-{session_id}"
    flow_uuid = "f77b70f0-849b-4d11-9ccc-449b3c4ba981"
    calls: list[str] = []

    def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs["url"])
        if kwargs["method"] == "POST":
            raise target_client.FlowBuilderTargetError(
                "target_core_http_error",
                "slug já existe",
                status_code=409,
            )
        if "/v2/flow?" in kwargs["url"]:
            return target_client.TargetResponse(
                status_code=200,
                payload={"data": [{"summary": {"id": flow_uuid, "slug": slug}}]},
            )
        return target_client.TargetResponse(
            status_code=200,
            payload={
                "data": {
                    "summary": {"id": flow_uuid, "slug": slug},
                    "draft_revision": {
                        "checksum": "b" * 64,
                        "definition": {
                            "mode": "orchestration",
                            "builder_metadata": {"builder_session_id": session_id},
                        },
                    },
                }
            },
        )

    monkeypatch.setattr(target_client, "_request_json", fake_request_json)
    result = await target_client.create_orchestration_draft(
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        actor="user-123",
        builder_session_id=session_id,
        definition={"info": {"name": "Canário IA"}, "mode": "orchestration"},
        settings=_target_settings(),
    )

    assert result.flow_uuid == UUID(flow_uuid)
    assert result.draft_checksum == "b" * 64
    assert len(calls) == 3
