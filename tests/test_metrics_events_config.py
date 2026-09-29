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
        "ORCH_METRICS_EVENTS_ENABLED",
        "ORCH_METRICS_EVENTS_WORKSPACE_ALLOWLIST",
        "METRICS_API_BASE_URL",
        "METRICS_API_KEY",
        "CELERY_METRICS_EVENTS_QUEUE",
        "CELERY_BEAT_METRICS_EVENTS_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    config.get_settings.cache_clear()


def test_metrics_events_are_disabled_and_isolated_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)

    settings = config.get_settings()

    assert settings.orch_metrics_events_enabled is False
    assert settings.orch_metrics_events_workspace_allowlist == ()
    assert settings.celery_metrics_events_queue == "orch_metrics_events"
    assert settings.celery_beat_metrics_events_enabled is False
    config.get_settings.cache_clear()


def test_metrics_events_writers_do_not_require_every_process_to_be_a_beat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("CELERY_ENABLED", "true")
    monkeypatch.setenv("ORCH_METRICS_EVENTS_ENABLED", "true")
    monkeypatch.setenv(
        "ORCH_METRICS_EVENTS_WORKSPACE_ALLOWLIST",
        WORKSPACE_UUID,
    )
    monkeypatch.setenv("METRICS_API_BASE_URL", "https://metrics.example.test/api")
    monkeypatch.setenv("METRICS_API_KEY", "test-key")

    settings = config.get_settings()

    assert settings.orch_metrics_events_enabled is True
    assert settings.celery_beat_metrics_events_enabled is False
    config.get_settings.cache_clear()


def test_metrics_events_reject_invalid_workspace_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("CELERY_ENABLED", "true")
    monkeypatch.setenv("ORCH_METRICS_EVENTS_ENABLED", "true")
    monkeypatch.setenv("ORCH_METRICS_EVENTS_WORKSPACE_ALLOWLIST", "not-a-uuid")
    monkeypatch.setenv("METRICS_API_BASE_URL", "https://metrics.example.test/api")
    monkeypatch.setenv("METRICS_API_KEY", "test-key")

    with pytest.raises(ValueError, match="WORKSPACE_ALLOWLIST contém UUID inválido"):
        config.get_settings()
    config.get_settings.cache_clear()
