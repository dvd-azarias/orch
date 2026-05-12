from __future__ import annotations

import pytest

import app.services.fileapp_mailing_association_service as service


class _DummySettings:
    sync_webhook_base_url = "http://target-core-api.otima.io"
    sync_ws_timeout_seconds = 5.0
    target_core_api_bearer_token = "token-123"


@pytest.mark.asyncio
async def test_associate_mailing_to_flow_from_file_event_builds_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_post_json(*, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        captured["timeout_seconds"] = timeout_seconds
        return 200, '{"ok":true}'

    monkeypatch.setattr(service, "_post_json", _fake_post_json)

    result = await service.associate_mailing_to_flow_from_file_event(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="767325cd-68ca-4be7-9f21-278012b98f8a",
        mailing_uuid="71117d9b-428b-4681-8b4f-fbf33007d307",
        linked_by="2f388d0f-5519-4e30-99ad-de34c96b9a59",
    )

    assert result["status"] == "done"
    assert captured["url"] == "http://target-core-api.otima.io/v2/flow/767325cd-68ca-4be7-9f21-278012b98f8a/mailings"
    assert captured["payload"] == {
        "mailing_ids_added": ["71117d9b-428b-4681-8b4f-fbf33007d307"],
        "mailing_ids_removed": [],
        "linked_by": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
        "call_origin": "file_event",
    }
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer token-123"
    assert headers["X-WORKSPACE-UUID"] == "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
    assert headers["x-application"] == "target"


@pytest.mark.asyncio
async def test_associate_mailing_to_flow_from_file_event_ignores_without_bearer() -> None:
    class _NoTokenSettings:
        sync_webhook_base_url = "http://target-core-api.otima.io"
        sync_ws_timeout_seconds = 5.0
        target_core_api_bearer_token = None

    result = await service.associate_mailing_to_flow_from_file_event(
        settings=_NoTokenSettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="767325cd-68ca-4be7-9f21-278012b98f8a",
        mailing_uuid="71117d9b-428b-4681-8b4f-fbf33007d307",
        linked_by="file-1",
    )
    assert result["status"] == "ignored"
    assert result["reason"] == "target_core_api_bearer_token_not_configured"


@pytest.mark.asyncio
async def test_associate_mailing_to_flow_from_file_event_uses_workspace_api_key_when_bearer_missing(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _NoBearerSettings:
        sync_webhook_base_url = "http://target-core-api.otima.io"
        sync_ws_timeout_seconds = 5.0
        target_core_api_bearer_token = None

    def _fake_post_json(*, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        captured["timeout_seconds"] = timeout_seconds
        return 200, '{"ok":true}'

    monkeypatch.setattr(service, "_post_json", _fake_post_json)

    result = await service.associate_mailing_to_flow_from_file_event(
        settings=_NoBearerSettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="767325cd-68ca-4be7-9f21-278012b98f8a",
        mailing_uuid="71117d9b-428b-4681-8b4f-fbf33007d307",
        linked_by="2f388d0f-5519-4e30-99ad-de34c96b9a59",
        workspace_api_key="sk-workspace-key",
    )

    assert result["status"] == "done"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert "authorization" not in headers
    assert headers["x-api-key"] == "sk-workspace-key"
    assert headers["x-workspace-api-key"] == "sk-workspace-key"
