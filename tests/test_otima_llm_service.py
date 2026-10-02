from __future__ import annotations

from types import SimpleNamespace

import app.services.otima_llm_service as llm_service


def _settings():
    return SimpleNamespace(
        otima_llm_api_gateway="http://llm.internal",
        otima_llm_api_base_url=None,
        otima_llm_api_key="secret",
        otima_llm_api_timeout_seconds=10,
    )


def test_gpt5_chat_payload_omits_unsupported_temperature(monkeypatch) -> None:
    captured = {}

    def fake_request(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return 200, {"choices": [{"message": {"content": '{"ok":true}'}}]}

    monkeypatch.setattr(llm_service, "get_settings", _settings)
    monkeypatch.setattr(llm_service, "_http_json_request", fake_request)

    result = llm_service.execute_otima_llm_prompt(
        model="gpt-5",
        system_prompt="system",
        user_prompt="user",
        workspace_uuid=None,
        workspace_api_key=None,
    )

    assert "temperature" not in captured["payload"]
    assert captured["timeout_seconds"] == 10.0
    assert result["parsed_json"] == {"ok": True}


def test_non_gpt5_chat_payload_preserves_temperature(monkeypatch) -> None:
    captured = {}

    def fake_request(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return 200, {"choices": [{"message": {"content": '{"ok":true}'}}]}

    monkeypatch.setattr(llm_service, "get_settings", _settings)
    monkeypatch.setattr(llm_service, "_http_json_request", fake_request)

    llm_service.execute_otima_llm_prompt(
        model="gpt-4o-mini",
        system_prompt="system",
        user_prompt="user",
        workspace_uuid=None,
        workspace_api_key=None,
    )

    assert captured["payload"]["temperature"] == 0.2


def test_explicit_timeout_overrides_shared_setting(monkeypatch) -> None:
    captured = {}

    def fake_request(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return 200, {"choices": [{"message": {"content": '{"ok":true}'}}]}

    monkeypatch.setattr(llm_service, "get_settings", _settings)
    monkeypatch.setattr(llm_service, "_http_json_request", fake_request)

    llm_service.execute_otima_llm_prompt(
        model="gpt-5",
        system_prompt="system",
        user_prompt="user",
        workspace_uuid=None,
        workspace_api_key=None,
        timeout_seconds=60,
    )

    assert captured["timeout_seconds"] == 60.0


def test_multimodal_chat_payload_uses_image_url_content(monkeypatch) -> None:
    captured = {}

    def fake_request(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return 200, {"choices": [{"message": {"content": '{"ok":true}'}}]}

    monkeypatch.setattr(llm_service, "get_settings", _settings)
    monkeypatch.setattr(llm_service, "_http_json_request", fake_request)

    llm_service.execute_otima_llm_prompt(
        model="gpt-5",
        system_prompt="system",
        user_prompt="analyze",
        user_image_data_url="data:image/png;base64,AAAA",
        workspace_uuid=None,
        workspace_api_key=None,
    )

    user_content = captured["payload"]["messages"][1]["content"]
    assert user_content[0] == {"type": "text", "text": "analyze"}
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["detail"] == "high"


def test_multimodal_responses_payload_uses_input_image(monkeypatch) -> None:
    requests = []

    def fake_request(**kwargs):  # type: ignore[no-untyped-def]
        requests.append(kwargs)
        if kwargs["url"].endswith("/chat/completions"):
            return 404, {}
        if kwargs["url"].endswith("/responses"):
            return 200, {"output_text": '{"ok":true}'}
        return 404, {}

    monkeypatch.setattr(llm_service, "get_settings", _settings)
    monkeypatch.setattr(llm_service, "_http_json_request", fake_request)

    llm_service.execute_otima_llm_prompt(
        model="gpt-5",
        system_prompt="system",
        user_prompt="analyze",
        user_image_data_url="data:image/png;base64,AAAA",
        workspace_uuid=None,
        workspace_api_key=None,
    )

    responses_request = next(item for item in requests if item["url"].endswith("/responses"))
    user_content = responses_request["payload"]["input"][1]["content"]
    assert user_content[0] == {"type": "input_text", "text": "analyze"}
    assert user_content[1]["type"] == "input_image"
    assert user_content[1]["detail"] == "high"
