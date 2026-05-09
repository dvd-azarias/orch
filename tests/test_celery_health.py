from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import app.services.celery_health_service as celery_health_service


def test_check_celery_health_disabled_returns_healthy(monkeypatch) -> None:
    monkeypatch.setattr(
        celery_health_service,
        "get_settings",
        lambda: SimpleNamespace(celery_enabled=False),
    )

    health = celery_health_service.check_celery_health()

    assert health["enabled"] is False
    assert health["healthy"] is True
    assert health["details"]["reason"] == "celery_disabled"


def test_check_celery_health_happy_path(monkeypatch) -> None:
    settings = SimpleNamespace(
        celery_enabled=True,
        celery_result_backend="redis://localhost:6379/0",
        celery_health_heartbeat_key="orch:beat:heartbeat",
        celery_health_heartbeat_ttl_seconds=30,
    )
    monkeypatch.setattr(celery_health_service, "get_settings", lambda: settings)

    @contextmanager
    def _conn():
        class _C:
            def ensure_connection(self, max_retries=0):  # noqa: ANN001
                return None

        yield _C()

    monkeypatch.setattr(celery_health_service.celery_app, "connection_for_read", lambda: _conn())

    class _Inspect:
        def ping(self):
            return {"worker@local": {"ok": "pong"}}

    monkeypatch.setattr(celery_health_service.celery_app.control, "inspect", lambda timeout=1.5: _Inspect())
    monkeypatch.setattr(
        celery_health_service,
        "_read_beat_heartbeat",
        lambda redis_url, heartbeat_key: (True, datetime.now(timezone.utc)),
    )

    health = celery_health_service.check_celery_health()

    assert health["enabled"] is True
    assert health["healthy"] is True
    assert health["broker_ok"] is True
    assert health["worker_ok"] is True
    assert health["beat_ok"] is True
    assert len(health["worker_nodes"]) == 1
