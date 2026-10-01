from __future__ import annotations

import pytest

import app.core.config as config


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"


def _minimal_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_load_dotenv", lambda _path: None)
    for key, value in {
        "DATABASE_HOST": "localhost",
        "DATABASE_PORT": "5432",
        "DATABASE_NAME": "orch",
        "DATABASE_USER": "orch",
        "DATABASE_PASSWORD": "test",
        "DATABASE_SCHEMA": "public",
        "ORCH_QUEUE_PROFILE": "prod",
    }.items():
        monkeypatch.setenv(key, value)
    for key in (
        "ORCH_FLOW_BUILDER_ENABLED",
        "ORCH_FLOW_BUILDER_CLIENT_ID",
        "ORCH_FLOW_BUILDER_CLIENT_SECRET",
        "ORCH_FLOW_BUILDER_WORKSPACE_ALLOWLIST",
        "ORCH_FLOW_BUILDER_TARGET_TIMEOUT_SECONDS",
        "ORCH_FLOW_BUILDER_LLM_MODEL",
        "ORCH_FLOW_BUILDER_LLM_TIMEOUT_SECONDS",
        "TARGET_CORE_API_BASE_URL",
        "TARGET_CORE_API_BEARER_TOKEN",
        "OTIMA_LLM_API_BASE_URL",
        "OTIMA_LLM_API_GATEWAY",
        "OTIMA_LLM_API_KEY",
        "SYNC_WEBHOOK_BASE_URL",
        "SYNC_WEBHOOK_BEARER_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    config.get_settings.cache_clear()


def test_flow_builder_is_fail_closed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _minimal_environment(monkeypatch)
    settings = config.get_settings()
    assert settings.orch_flow_builder_enabled is False
    assert settings.orch_flow_builder_workspace_allowlist == ()
    assert settings.orch_flow_builder_client_id is None
    assert settings.orch_flow_builder_client_secret is None
    assert settings.orch_flow_builder_target_timeout_seconds == 10.0
    assert settings.orch_flow_builder_llm_model == "gpt-5"
    assert settings.orch_flow_builder_llm_timeout_seconds == 60.0
    config.get_settings.cache_clear()


def test_flow_builder_requires_allowlist_credentials_and_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("ORCH_FLOW_BUILDER_ENABLED", "true")

    with pytest.raises(ValueError, match="WORKSPACE_ALLOWLIST é obrigatória"):
        config.get_settings()
    config.get_settings.cache_clear()

    monkeypatch.setenv("ORCH_FLOW_BUILDER_WORKSPACE_ALLOWLIST", WORKSPACE_UUID)
    with pytest.raises(ValueError, match="CLIENT_ID.*CLIENT_SECRET"):
        config.get_settings()
    config.get_settings.cache_clear()

    monkeypatch.setenv("ORCH_FLOW_BUILDER_CLIENT_ID", "builder-ui")
    monkeypatch.setenv("ORCH_FLOW_BUILDER_CLIENT_SECRET", "secret")
    with pytest.raises(ValueError, match="TARGET_CORE_API_BASE_URL"):
        config.get_settings()
    config.get_settings.cache_clear()

    monkeypatch.setenv("TARGET_CORE_API_BASE_URL", "https://target.example.test")
    monkeypatch.setenv("TARGET_CORE_API_BEARER_TOKEN", "target-secret")
    with pytest.raises(ValueError, match="OTIMA_LLM_API_BASE_URL"):
        config.get_settings()
    config.get_settings.cache_clear()


def test_flow_builder_accepts_complete_isolated_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("ORCH_FLOW_BUILDER_ENABLED", "true")
    monkeypatch.setenv("ORCH_FLOW_BUILDER_WORKSPACE_ALLOWLIST", WORKSPACE_UUID)
    monkeypatch.setenv("ORCH_FLOW_BUILDER_CLIENT_ID", "builder-ui")
    monkeypatch.setenv("ORCH_FLOW_BUILDER_CLIENT_SECRET", "secret")
    monkeypatch.setenv("TARGET_CORE_API_BASE_URL", "https://target.example.test")
    monkeypatch.setenv("TARGET_CORE_API_BEARER_TOKEN", "target-secret")
    monkeypatch.setenv("OTIMA_LLM_API_BASE_URL", "https://llm.example.test")
    monkeypatch.setenv("OTIMA_LLM_API_KEY", "llm-secret")

    settings = config.get_settings()
    assert settings.orch_flow_builder_enabled is True
    assert settings.orch_flow_builder_workspace_allowlist == (WORKSPACE_UUID,)
    assert settings.orch_flow_builder_llm_model == "gpt-5"
    assert settings.orch_flow_builder_llm_timeout_seconds == 60.0
    config.get_settings.cache_clear()
