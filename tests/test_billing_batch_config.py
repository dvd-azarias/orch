from __future__ import annotations

import pytest

import app.core.config as config


def _minimal_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
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
    for key in ("ORCH_BILLING_SNAPSHOT_ENABLED", "ORCH_BILLING_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    config.get_settings.cache_clear()


def test_legacy_and_batch_billing_are_disabled_by_default(monkeypatch) -> None:
    _minimal_environment(monkeypatch)
    settings = config.get_settings()
    assert settings.orch_billing_snapshot_enabled is False
    assert settings.orch_billing_enabled is False
    assert settings.billing_batch_size == 200
    assert settings.billing_flush_interval_seconds == 300
    assert settings.billing_reprocess_chunk_size == 1000
    assert settings.celery_billing_queue == "orch.billing.outbox"
    config.get_settings.cache_clear()


def test_dual_billing_flags_fail_closed(monkeypatch) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("ORCH_BILLING_SNAPSHOT_ENABLED", "true")
    monkeypatch.setenv("ORCH_BILLING_ENABLED", "true")
    config.get_settings.cache_clear()
    with pytest.raises(ValueError, match="não podem estar ativos ao mesmo tempo"):
        config.get_settings()
    config.get_settings.cache_clear()


def test_billing_configuration_rejects_out_of_range_batch(monkeypatch) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("BILLING_BATCH_SIZE", "0")
    with pytest.raises(ValueError, match="BILLING_BATCH_SIZE"):
        config.get_settings()
    config.get_settings.cache_clear()


def test_enabled_batch_billing_requires_dedicated_broker_url(monkeypatch) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("ORCH_BILLING_ENABLED", "true")
    monkeypatch.delenv("BILLING_RABBITMQ_URL", raising=False)
    with pytest.raises(ValueError, match="BILLING_RABBITMQ_URL"):
        config.get_settings()
    config.get_settings.cache_clear()
