from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

import app.core.config as config


FLOW_UUID = "c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152"
WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"


def _minimal_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_load_dotenv", lambda _path: None)
    required = {
        "DATABASE_HOST": "localhost",
        "DATABASE_PORT": "5432",
        "DATABASE_NAME": "orch",
        "DATABASE_USER": "orch",
        "DATABASE_PASSWORD": "test",
        "DATABASE_SCHEMA": "public",
        "ORCH_QUEUE_PROFILE": "prod",
    }
    for key, value in required.items():
        monkeypatch.setenv(key, value)
    for key in (
        "CELERY_ENABLED",
        "CHANNEL_SUPPLIER_V2_ENABLED",
        "CHANNEL_SUPPLIER_V2_WORKSPACE_ALLOWLIST",
        "CHANNEL_SUPPLIER_V2_FLOW_ALLOWLIST",
        "CHANNEL_SUPPLIER_V2_ENCRYPTION_KEY",
        "CHANNEL_SUPPLIER_V2_ENCRYPTION_KEY_ID",
        "CHANNEL_SUPPLIER_V2_CALLBACKS_ENABLED",
        "CHANNEL_SUPPLIER_V2_CALLBACK_BASE_URL",
        "TARGET_CORE_SUPPLIER_API_BASE_URL",
        "TARGET_CORE_API_BEARER_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    config.get_settings.cache_clear()


def _enable_gate_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELERY_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SUPPLIER_V2_ENABLED", "true")
    monkeypatch.setenv(
        "CHANNEL_SUPPLIER_V2_WORKSPACE_ALLOWLIST", WORKSPACE_UUID
    )
    monkeypatch.setenv("CHANNEL_SUPPLIER_V2_FLOW_ALLOWLIST", FLOW_UUID)
    monkeypatch.setenv(
        "CHANNEL_SUPPLIER_V2_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    monkeypatch.setenv(
        "TARGET_CORE_SUPPLIER_API_BASE_URL", "https://target.internal"
    )
    monkeypatch.setenv("TARGET_CORE_API_BEARER_TOKEN", "internal-token")


def test_channel_callbacks_are_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)

    settings = config.get_settings()

    assert settings.channel_supplier_v2_callbacks_enabled is False
    assert settings.channel_supplier_v2_callback_base_url is None
    config.get_settings.cache_clear()


def test_channel_callbacks_require_gate_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("CHANNEL_SUPPLIER_V2_CALLBACKS_ENABLED", "true")
    monkeypatch.setenv(
        "CHANNEL_SUPPLIER_V2_CALLBACK_BASE_URL",
        "https://syncwebhook.example.test",
    )

    with pytest.raises(ValueError, match="CHANNEL_SUPPLIER_V2_ENABLED=true"):
        config.get_settings()
    config.get_settings.cache_clear()


def test_channel_callbacks_require_public_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    _enable_gate_one(monkeypatch)
    monkeypatch.setenv("CHANNEL_SUPPLIER_V2_CALLBACKS_ENABLED", "true")

    with pytest.raises(
        ValueError, match="CHANNEL_SUPPLIER_V2_CALLBACK_BASE_URL"
    ):
        config.get_settings()
    config.get_settings.cache_clear()


def test_channel_callbacks_accept_explicit_gate_and_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    _enable_gate_one(monkeypatch)
    monkeypatch.setenv("CHANNEL_SUPPLIER_V2_CALLBACKS_ENABLED", "true")
    monkeypatch.setenv(
        "CHANNEL_SUPPLIER_V2_CALLBACK_BASE_URL",
        "https://syncwebhook.example.test",
    )

    settings = config.get_settings()

    assert settings.channel_supplier_v2_callbacks_enabled is True
    assert (
        settings.channel_supplier_v2_callback_base_url
        == "https://syncwebhook.example.test"
    )
    config.get_settings.cache_clear()


@pytest.mark.parametrize(
    "callback_base_url",
    [
        "ftp://syncwebhook.example.test",
        "https://syncwebhook.example.test?token=secret",
        "https://syncwebhook.example.test#fragment",
        "/relative/path",
    ],
)
def test_channel_callbacks_reject_invalid_public_base_url(
    monkeypatch: pytest.MonkeyPatch,
    callback_base_url: str,
) -> None:
    _minimal_environment(monkeypatch)
    _enable_gate_one(monkeypatch)
    monkeypatch.setenv("CHANNEL_SUPPLIER_V2_CALLBACKS_ENABLED", "true")
    monkeypatch.setenv(
        "CHANNEL_SUPPLIER_V2_CALLBACK_BASE_URL",
        callback_base_url,
    )

    with pytest.raises(ValueError, match="HTTP/HTTPS sem query ou fragmento"):
        config.get_settings()
    config.get_settings.cache_clear()
