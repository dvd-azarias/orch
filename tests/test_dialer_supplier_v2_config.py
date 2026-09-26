from __future__ import annotations

import pytest

import app.core.config as config


FLOW_UUID = "4e163399-e9a0-4335-895f-316c6a161299"
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
        "DIALER_SUPPLIER_V2_ENABLED",
        "DIALER_SUPPLIER_V2_WORKSPACE_ALLOWLIST",
        "DIALER_SUPPLIER_V2_FLOW_ALLOWLIST",
        "CELERY_DIALER_SUPPLIER_V2_QUEUE",
        "CELERY_BEAT_DIALER_SUPPLIER_V2_RECONCILE_ENABLED",
        "CELERY_BEAT_CHANNEL_SUPPLIER_V2_RECONCILE_ENABLED",
        "TARGET_CORE_SUPPLIER_API_BASE_URL",
        "TARGET_CORE_API_BEARER_TOKEN",
        "ORCH_DIALER_MULTILANE_V2_ENABLED",
        "ORCH_DIALER_MULTILANE_V2_FLOW_UUIDS",
        "ORCH_DIALER_MULTILANE_V2_MAX_LANES_PER_FLOW",
        "ORCH_DIALER_MULTILANE_V2_MAX_EXECUTION_GROUPS_PER_FLOW",
        "ORCH_JOURNEY_DASHBOARD_ENABLED",
        "CELERY_BEAT_JOURNEY_SNAPSHOT_ENABLED",
        "CELERY_JOURNEY_SNAPSHOT_QUEUE",
    ):
        monkeypatch.delenv(key, raising=False)
    config.get_settings.cache_clear()


def test_supplier_v2_is_disabled_and_isolated_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    settings = config.get_settings()

    assert settings.dialer_supplier_v2_enabled is False
    assert settings.dialer_supplier_v2_workspace_allowlist == ()
    assert settings.dialer_supplier_v2_flow_allowlist == ()
    assert settings.celery_dialer_supplier_v2_queue == "orch_dialer_supplier_v2"
    assert settings.celery_beat_dialer_supplier_v2_reconcile_enabled is False
    assert settings.celery_beat_channel_supplier_v2_reconcile_enabled is False
    assert settings.orch_dialer_multilane_v2_enabled is False
    assert settings.orch_dialer_multilane_v2_flow_uuids == ()
    assert settings.orch_dialer_multilane_v2_max_lanes_per_flow == 1
    assert settings.orch_dialer_multilane_v2_max_execution_groups_per_flow == 1
    assert settings.orch_journey_dashboard_enabled is True
    assert settings.celery_beat_journey_snapshot_enabled is False
    assert settings.celery_journey_snapshot_queue == "orch_journey_snapshot"
    config.get_settings.cache_clear()


def _enable_supplier_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELERY_ENABLED", "true")
    monkeypatch.setenv("DIALER_SUPPLIER_V2_ENABLED", "true")
    monkeypatch.setenv("DIALER_SUPPLIER_V2_WORKSPACE_ALLOWLIST", WORKSPACE_UUID)
    monkeypatch.setenv("DIALER_SUPPLIER_V2_FLOW_ALLOWLIST", FLOW_UUID)
    monkeypatch.setenv(
        "TARGET_CORE_SUPPLIER_API_BASE_URL",
        "https://supplier.internal",
    )
    monkeypatch.setenv("TARGET_CORE_API_BEARER_TOKEN", "internal-token")


def test_multilane_requires_supplier_v2_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("ORCH_DIALER_MULTILANE_V2_ENABLED", "true")
    monkeypatch.setenv("ORCH_DIALER_MULTILANE_V2_FLOW_UUIDS", FLOW_UUID)
    monkeypatch.setenv("ORCH_DIALER_MULTILANE_V2_MAX_LANES_PER_FLOW", "2")

    with pytest.raises(ValueError, match="DIALER_SUPPLIER_V2_ENABLED=true"):
        config.get_settings()
    config.get_settings.cache_clear()


def test_multilane_requires_flow_in_supplier_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    _enable_supplier_v2(monkeypatch)
    monkeypatch.setenv("ORCH_DIALER_MULTILANE_V2_ENABLED", "true")
    monkeypatch.setenv(
        "ORCH_DIALER_MULTILANE_V2_FLOW_UUIDS",
        "8b81e493-b39c-4829-8b1e-5bafd00aeb7c",
    )
    monkeypatch.setenv("ORCH_DIALER_MULTILANE_V2_MAX_LANES_PER_FLOW", "2")

    with pytest.raises(ValueError, match="DIALER_SUPPLIER_V2_FLOW_ALLOWLIST"):
        config.get_settings()
    config.get_settings.cache_clear()


def test_multilane_accepts_explicit_intersection_and_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    _enable_supplier_v2(monkeypatch)
    monkeypatch.setenv("ORCH_DIALER_MULTILANE_V2_ENABLED", "true")
    monkeypatch.setenv("ORCH_DIALER_MULTILANE_V2_FLOW_UUIDS", FLOW_UUID)
    monkeypatch.setenv("ORCH_DIALER_MULTILANE_V2_MAX_LANES_PER_FLOW", "2")
    monkeypatch.setenv(
        "ORCH_DIALER_MULTILANE_V2_MAX_EXECUTION_GROUPS_PER_FLOW",
        "2",
    )

    settings = config.get_settings()

    assert settings.orch_dialer_multilane_v2_enabled is True
    assert settings.orch_dialer_multilane_v2_flow_uuids == (FLOW_UUID,)
    assert settings.orch_dialer_multilane_v2_max_lanes_per_flow == 2
    assert settings.orch_dialer_multilane_v2_max_execution_groups_per_flow == 2
    config.get_settings.cache_clear()


def test_supplier_v2_enabled_requires_workspace_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("CELERY_ENABLED", "true")
    monkeypatch.setenv("DIALER_SUPPLIER_V2_ENABLED", "true")

    with pytest.raises(ValueError, match="WORKSPACE_ALLOWLIST é obrigatória"):
        config.get_settings()
    config.get_settings.cache_clear()


def test_supplier_v2_enabled_requires_flow_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("CELERY_ENABLED", "true")
    monkeypatch.setenv("DIALER_SUPPLIER_V2_ENABLED", "true")
    monkeypatch.setenv("DIALER_SUPPLIER_V2_WORKSPACE_ALLOWLIST", WORKSPACE_UUID)

    with pytest.raises(ValueError, match="FLOW_ALLOWLIST é obrigatória"):
        config.get_settings()
    config.get_settings.cache_clear()


def test_supplier_v2_enabled_rejects_invalid_flow_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("CELERY_ENABLED", "true")
    monkeypatch.setenv("DIALER_SUPPLIER_V2_ENABLED", "true")
    monkeypatch.setenv("DIALER_SUPPLIER_V2_WORKSPACE_ALLOWLIST", WORKSPACE_UUID)
    monkeypatch.setenv("DIALER_SUPPLIER_V2_FLOW_ALLOWLIST", "not-a-uuid")

    with pytest.raises(ValueError, match="allowlists.*UUID inválido"):
        config.get_settings()
    config.get_settings.cache_clear()


def test_supplier_v2_enabled_accepts_explicit_flow_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("CELERY_ENABLED", "true")
    monkeypatch.setenv("DIALER_SUPPLIER_V2_ENABLED", "true")
    monkeypatch.setenv("DIALER_SUPPLIER_V2_WORKSPACE_ALLOWLIST", WORKSPACE_UUID)
    monkeypatch.setenv("DIALER_SUPPLIER_V2_FLOW_ALLOWLIST", FLOW_UUID)
    monkeypatch.setenv(
        "TARGET_CORE_SUPPLIER_API_BASE_URL",
        "https://supplier.internal",
    )
    monkeypatch.setenv("TARGET_CORE_API_BEARER_TOKEN", "internal-token")

    settings = config.get_settings()

    assert settings.dialer_supplier_v2_enabled is True
    assert settings.dialer_supplier_v2_workspace_allowlist == (WORKSPACE_UUID,)
    assert settings.dialer_supplier_v2_flow_allowlist == (FLOW_UUID,)
    config.get_settings.cache_clear()


@pytest.mark.parametrize(
    ("missing_name", "configured_name", "configured_value"),
    [
        (
            "TARGET_CORE_SUPPLIER_API_BASE_URL",
            "TARGET_CORE_API_BEARER_TOKEN",
            "internal-token",
        ),
        (
            "TARGET_CORE_API_BEARER_TOKEN",
            "TARGET_CORE_SUPPLIER_API_BASE_URL",
            "https://supplier.internal",
        ),
    ],
)
def test_supplier_v2_enabled_requires_supplier_credentials(
    monkeypatch: pytest.MonkeyPatch,
    missing_name: str,
    configured_name: str,
    configured_value: str,
) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("CELERY_ENABLED", "true")
    monkeypatch.setenv("DIALER_SUPPLIER_V2_ENABLED", "true")
    monkeypatch.setenv("DIALER_SUPPLIER_V2_WORKSPACE_ALLOWLIST", WORKSPACE_UUID)
    monkeypatch.setenv("DIALER_SUPPLIER_V2_FLOW_ALLOWLIST", FLOW_UUID)
    monkeypatch.setenv(configured_name, configured_value)
    monkeypatch.delenv(missing_name, raising=False)

    with pytest.raises(ValueError, match=missing_name):
        config.get_settings()
    config.get_settings.cache_clear()


def test_supplier_v2_enabled_requires_celery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimal_environment(monkeypatch)
    monkeypatch.setenv("CELERY_ENABLED", "false")
    monkeypatch.setenv("DIALER_SUPPLIER_V2_ENABLED", "true")

    with pytest.raises(ValueError, match="CELERY_ENABLED=true é obrigatório"):
        config.get_settings()
    config.get_settings.cache_clear()
