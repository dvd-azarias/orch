from __future__ import annotations

import json
from typing import Any

import pytest

import app.services.switch_bot_flow_service as service
from app.services.switch_bot_flow_service import (
    SwitchBotFlowError,
    deliver_meta_payload,
    extract_meta_message_ids,
    is_meta_user_message_payload,
    resolve_runner_token,
    resolve_target_flow_uuid,
)


class _Settings:
    target_core_api_base_url = "http://target-core-api.test"
    target_core_api_bearer_token = "api-bearer"
    switch_bot_flow_http_timeout_seconds = 30.0
    switch_bot_flow_max_attempts = 3
    switch_bot_flow_retry_backoff_seconds = 0.0
    celery_result_backend = None


class _Response:
    def __init__(self, body: dict[str, Any], *, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _meta_payload(*, message_id: str = "wamid.001") -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "phone-number-1"},
                            "contacts": [{"wa_id": "5511975620806", "profile": {"name": "Jurema"}}],
                            "messages": [
                                {
                                    "from": "5511975620806",
                                    "id": message_id,
                                    "timestamp": "1770000000",
                                    "type": "text",
                                    "text": {"body": "Olá"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_resolve_target_flow_uuid_from_catalog_parameter() -> None:
    assert (
        resolve_target_flow_uuid(
            {"parameters": {"flow": {"id": "b88c26b2-b5df-4a3d-a9b1-2611c0e3cb31"}}}
        )
        == "b88c26b2-b5df-4a3d-a9b1-2611c0e3cb31"
    )


def test_meta_message_detection_preserves_message_identity() -> None:
    payload = _meta_payload()
    assert is_meta_user_message_payload(payload) is True
    assert extract_meta_message_ids(payload) == ["wamid.001"]


def test_resolve_runner_token_uses_cache_after_first_get(monkeypatch) -> None:
    calls: list[Any] = []
    service._runner_token_memory_cache.clear()
    monkeypatch.setattr(service, "_redis_client", lambda _settings: None)

    def _urlopen(req, timeout):  # type: ignore[no-untyped-def]
        calls.append((req, timeout))
        return _Response({"data": {"summary": {"runner_token": "runner-token"}}})

    monkeypatch.setattr(service.request, "urlopen", _urlopen)
    first = resolve_runner_token(
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        target_flow_uuid="b88c26b2-b5df-4a3d-a9b1-2611c0e3cb31",
        settings=_Settings(),
    )
    second = resolve_runner_token(
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        target_flow_uuid="b88c26b2-b5df-4a3d-a9b1-2611c0e3cb31",
        settings=_Settings(),
    )

    assert first == second == "runner-token"
    assert len(calls) == 1
    assert calls[0][0].get_header("X-workspace-uuid") == "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
    assert calls[0][0].get_header("Authorization") == "Bearer api-bearer"


def test_deliver_meta_payload_posts_original_json_content(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    payload = _meta_payload()

    def _urlopen(req, timeout):  # type: ignore[no-untyped-def]
        captured["url"] = req.full_url
        captured["method"] = req.method
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response({"status": "queued", "session_id": "target-session-1"}, status=202)

    monkeypatch.setattr(service.request, "urlopen", _urlopen)
    result = deliver_meta_payload(
        runner_token="runner-token",
        payload=payload,
        settings=_Settings(),
    )

    assert captured["method"] == "POST"
    assert captured["payload"] == payload
    assert captured["url"].endswith("/v5/runner/tokens/runner-token/whatsapp/session")
    assert result.status_code == 202
    assert result.session_id == "target-session-1"


def test_deliver_meta_payload_rejects_status_only_webhook() -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.001", "status": "read"}]}}]}],
    }
    with pytest.raises(SwitchBotFlowError) as exc:
        deliver_meta_payload(
            runner_token="runner-token",
            payload=payload,
            settings=_Settings(),
        )
    assert exc.value.code == "switch_bot_flow_meta_message_required"
