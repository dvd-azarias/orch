from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.billing_snapshot_service as billing


def _settings(**overrides):  # type: ignore[no-untyped-def]
    values = {
        "orch_billing_snapshot_enabled": True,
        "orch_billing_rabbitmq_url": "amqp://billing:secret@rabbitmq//",
        "orch_billing_exchange": "domain.events",
        "orch_billing_routing_key": "billing.usage.snapshot.v1.target",
        "orch_billing_application_code": "target",
        "orch_billing_service_code": "service-orch",
        "orch_billing_metric_code": "service-orch",
        "orch_billing_publish_timeout_seconds": 3.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_billing_snapshot_uses_utc_contract() -> None:
    settings = _settings()
    snapshot = billing.build_billing_snapshot(
        workspace_uuid="workspace-1",
        session_uuid="1fe19d40-2330-4263-9781-1805ece1d816",
        created_at=datetime(2026, 8, 25, 15, 30, 45, 123456, tzinfo=timezone.utc),
        settings=settings,
    )

    assert snapshot == {
        "snapshot_id": "orch_usage_202608_1fe19d40-2330-4263-9781-1805ece1d816",
        "workspace_uuid": "workspace-1",
        "application_code": "target",
        "billing_period": "2026-08",
        "snapshot_at": "2026-08-25T15:30:45.123Z",
        "currency": "BRL",
        "correction": False,
        "items": [
            {
                "service_code": "service-orch",
                "metric_code": "service-orch",
                "unit": "event",
                "quantity": 1,
            }
        ],
    }


def test_snapshot_id_is_stable_for_same_session() -> None:
    settings = _settings()
    created_at = datetime(2026, 8, 25, tzinfo=timezone.utc)
    first = billing.build_billing_snapshot(
        workspace_uuid="workspace-1", session_uuid="session-1", created_at=created_at, settings=settings
    )
    second = billing.build_billing_snapshot(
        workspace_uuid="workspace-1", session_uuid="session-1", created_at=created_at, settings=settings
    )

    assert first["snapshot_id"] == second["snapshot_id"]


@pytest.mark.asyncio
async def test_disabled_billing_does_not_write_outbox() -> None:
    db_session = AsyncMock()
    result = await billing.create_billing_snapshot_outbox(
        db_session,
        workspace_uuid="workspace-1",
        session_id=1,
        session_uuid="1fe19d40-2330-4263-9781-1805ece1d816",
        settings=_settings(orch_billing_snapshot_enabled=False),
    )

    assert result is None
    db_session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_outbox_failure_is_fail_open(monkeypatch) -> None:
    class _Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class _DbSession:
        def begin_nested(self):
            return _Transaction()

    monkeypatch.setattr(billing, "create_billing_snapshot_outbox", AsyncMock(side_effect=RuntimeError("missing table")))

    await billing.try_create_billing_snapshot_outbox(
        _DbSession(),
        workspace_uuid="workspace-1",
        session_id=1,
        session_uuid="1fe19d40-2330-4263-9781-1805ece1d816",
    )


def test_publish_uses_persistent_target_routing(monkeypatch) -> None:
    published = {}

    class FakeChannel:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class FakeConnection:
        def __init__(self, *_args, **_kwargs):
            published["connection"] = _kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def channel(self):
            return FakeChannel()

    class FakeProducer:
        def __init__(self, _channel):
            return None

        def publish(self, payload, **kwargs):
            published["payload"] = payload
            published["kwargs"] = kwargs

    monkeypatch.setattr(billing, "Connection", FakeConnection)
    monkeypatch.setattr(billing, "Producer", FakeProducer)
    snapshot = billing.build_billing_snapshot(
        workspace_uuid="workspace-1",
        session_uuid="session-1",
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        settings=_settings(),
    )

    billing.publish_billing_snapshot(snapshot=snapshot, settings=_settings())

    assert published["kwargs"]["routing_key"] == "billing.usage.snapshot.v1.target"
    assert published["kwargs"]["delivery_mode"] == 2
    assert published["kwargs"]["serializer"] == "json"
    assert published["kwargs"]["message_id"] == snapshot["snapshot_id"]
    assert published["kwargs"]["headers"]["source_application"] == "orch"


def test_publish_requires_configured_broker() -> None:
    snapshot = billing.build_billing_snapshot(
        workspace_uuid="workspace-1",
        session_uuid="session-1",
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        settings=_settings(),
    )

    with pytest.raises(RuntimeError, match="ORCH_BILLING_RABBITMQ_URL"):
        billing.publish_billing_snapshot(
            snapshot=snapshot,
            settings=_settings(orch_billing_rabbitmq_url=None),
        )
