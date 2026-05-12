from __future__ import annotations

import pytest

import app.services.fileapp_mailing_association_service as service


class _DummySettings:
    sync_webhook_base_url = "http://target-core-api.otima.io"
    sync_ws_timeout_seconds = 5.0
    target_core_api_bearer_token = "token-123"


@pytest.mark.asyncio
async def test_associate_mailing_pending_when_import_not_ready(monkeypatch) -> None:
    post_called = False

    def _fake_get_json(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        assert url.endswith("/v2/mailings/mailing-uuid-1")
        return 200, '{"data":{"status":"PROCESSING","ingested_at":null}}'

    def _fake_post_json(*, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        nonlocal post_called
        post_called = True
        return 200, '{"data":{"ok":true}}'

    monkeypatch.setattr(service, "_get_json", _fake_get_json)
    monkeypatch.setattr(service, "_post_json", _fake_post_json)

    result = await service.associate_mailing_to_flow_from_file_event(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="flow-uuid-1",
        mailing_uuid="mailing-uuid-1",
        linked_by="2f388d0f-5519-4e30-99ad-de34c96b9a59",
        workspace_api_key=None,
    )

    assert result["status"] == "pending"
    assert result["reason"] == "mailing_import_not_ready"
    assert post_called is False


@pytest.mark.asyncio
async def test_associate_mailing_calls_post_when_import_ready(monkeypatch) -> None:
    post_payloads: list[dict] = []

    def _fake_get_json(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        return 200, '{"data":{"status":"PROCESSED","ingested_at":"2026-05-12T16:01:00Z"}}'

    def _fake_post_json(*, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        post_payloads.append(payload)
        return 200, '{"data":[{"results":{"linked":["mailing-uuid-1"]}}]}'

    monkeypatch.setattr(service, "_get_json", _fake_get_json)
    monkeypatch.setattr(service, "_post_json", _fake_post_json)

    result = await service.associate_mailing_to_flow_from_file_event(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="flow-uuid-1",
        mailing_uuid="mailing-uuid-1",
        linked_by="2f388d0f-5519-4e30-99ad-de34c96b9a59",
        workspace_api_key=None,
    )

    assert result["status"] == "done"
    assert len(post_payloads) == 1
    assert post_payloads[0]["call_origin"] == "file_event"
