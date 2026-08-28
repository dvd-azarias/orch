from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.services.billing_batch_service as billing


class _Result:
    def __init__(self, rows=None, scalar=None):  # type: ignore[no-untyped-def]
        self.rows = list(rows or [])
        self.scalar = scalar

    def mappings(self):  # type: ignore[no-untyped-def]
        return self

    def first(self):  # type: ignore[no-untyped-def]
        return self.rows[0] if self.rows else None

    def one(self):  # type: ignore[no-untyped-def]
        return self.rows[0]

    def all(self):  # type: ignore[no-untyped-def]
        return self.rows

    def fetchall(self):  # type: ignore[no-untyped-def]
        return self.rows

    def scalar_one_or_none(self):  # type: ignore[no-untyped-def]
        return self.scalar

    def scalar_one(self):  # type: ignore[no-untyped-def]
        return self.scalar


def _settings(**overrides):  # type: ignore[no-untyped-def]
    values = {
        "orch_billing_enabled": True,
        "billing_batch_size": 200,
        "billing_application_code": "target",
        "billing_service_code": "service-orch",
        "billing_metric_code": "service-orch",
        "billing_rabbitmq_url": "amqp://billing:secret@rabbitmq//",
        "billing_exchange": "domain.events",
        "billing_routing_key": "billing.usage.snapshot.v1.target",
        "billing_publish_confirm_timeout_seconds": 10.0,
        "billing_reprocess_chunk_size": 1000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _aggregation_results(quantity: int):  # type: ignore[no-untyped-def]
    group = {"billing_period": date(2026, 8, 1), "metric_code": "service-orch", "service_code": "service-orch"}
    events = [{"id": item} for item in range(1, quantity + 1)]
    return [_Result([group]), _Result(events), _Result(), _Result(events)]


def test_parse_billing_period_uses_semiopen_utc_month() -> None:
    start, end = billing.parse_billing_period("2026-12")
    assert start == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["2026-00", "2026-13", "2026-8", "08-2026", ""])
def test_parse_billing_period_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        billing.parse_billing_period(value)


def test_retry_backoff_has_ceiling_and_continues_at_ceiling() -> None:
    assert billing.calculate_retry_delay_seconds(
        attempt_count=1, initial_seconds=15, maximum_seconds=3600, jitter_seconds=10, random_value=0
    ) == 15
    assert billing.calculate_retry_delay_seconds(
        attempt_count=8, initial_seconds=15, maximum_seconds=3600, jitter_seconds=10, random_value=1
    ) == 1930
    assert billing.calculate_retry_delay_seconds(
        attempt_count=100, initial_seconds=15, maximum_seconds=3600, jitter_seconds=10, random_value=1
    ) == 3600


def test_batch_payload_uses_exact_utc_contract() -> None:
    payload = billing.build_batch_snapshot_payload(
        snapshot_id="orch_usage_202608_ws_uuid",
        workspace_uuid="11111111-1111-1111-1111-111111111111",
        billing_period=date(2026, 8, 1),
        snapshot_at=datetime(2026, 8, 28, 14, 53, 50, tzinfo=timezone.utc),
        quantity=200,
        metric_code="service-orch",
        service_code="service-orch",
        application_code="target",
    )
    assert payload["snapshot_at"] == "2026-08-28T14:53:50.000000Z"
    assert payload["billing_period"] == "2026-08"
    assert payload["items"][0] == {
        "unit": "event",
        "quantity": 200,
        "metric_code": "service-orch",
        "service_code": "service-orch",
    }
    billing.validate_batch_snapshot_payload(
        payload,
        snapshot_id=str(payload["snapshot_id"]),
        workspace_uuid=str(payload["workspace_uuid"]),
        quantity=200,
    )


def test_invalid_payload_is_structural_not_retryable() -> None:
    with pytest.raises(billing.InvalidBillingPayload, match="snapshot_id"):
        billing.validate_batch_snapshot_payload(
            {"snapshot_id": "different"},
            snapshot_id="expected",
            workspace_uuid="workspace",
            quantity=1,
        )


@pytest.mark.asyncio
async def test_event_registration_is_idempotent_and_uses_session_created_at() -> None:
    db_session = AsyncMock()
    db_session.execute.return_value = _Result(scalar=1)
    created = await billing.record_billing_event(
        db_session,
        workspace_uuid="11111111-1111-1111-1111-111111111111",
        session_id=42,
        settings=_settings(),
    )
    statement = str(db_session.execute.await_args.args[0])
    assert created is True
    assert "session_row.created_at" in statement
    assert "AT TIME ZONE 'UTC'" in statement
    assert "ON CONFLICT (workspace_uuid, source_session_uuid, billing_period, metric_code)" in statement


@pytest.mark.asyncio
async def test_disabled_batch_billing_does_not_touch_database() -> None:
    db_session = AsyncMock()
    assert await billing.record_billing_event(
        db_session,
        workspace_uuid="11111111-1111-1111-1111-111111111111",
        session_id=42,
        settings=_settings(orch_billing_enabled=False),
    ) is False
    db_session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_450_events_create_200_200_50_batches() -> None:
    db_session = AsyncMock()
    db_session.execute.side_effect = [
        *_aggregation_results(200),
        *_aggregation_results(200),
        *_aggregation_results(50),
    ]
    quantities = []
    for _ in range(3):
        result = await billing.aggregate_next_billing_snapshot(
            db_session,
            workspace_uuid="11111111-1111-1111-1111-111111111111",
            settings=_settings(),
            now=datetime(2026, 8, 28, tzinfo=timezone.utc),
        )
        assert result is not None
        quantities.append(result.quantity)
    assert quantities == [200, 200, 50]


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [1, 30, 200])
async def test_incomplete_and_exact_batches_are_aggregated_on_flush(quantity: int) -> None:
    db_session = AsyncMock()
    db_session.execute.side_effect = _aggregation_results(quantity)
    result = await billing.aggregate_next_billing_snapshot(
        db_session,
        workspace_uuid="11111111-1111-1111-1111-111111111111",
        settings=_settings(),
    )
    assert result is not None
    assert result.quantity == quantity
    statements = [str(call.args[0]) for call in db_session.execute.await_args_list]
    assert "FOR UPDATE SKIP LOCKED" in statements[0]
    assert "FOR UPDATE SKIP LOCKED" in statements[1]


@pytest.mark.asyncio
async def test_claim_recovers_expired_lease_and_uses_claim_token() -> None:
    db_session = AsyncMock()
    claimed = {
        "snapshot_id": "snap-1",
        "workspace_uuid": "11111111-1111-1111-1111-111111111111",
        "payload": {},
        "quantity": 1,
        "attempt_count": 9,
        "claim_token": "22222222-2222-2222-2222-222222222222",
    }
    db_session.execute.side_effect = [_Result(), _Result([claimed])]
    rows = await billing.claim_due_billing_snapshots(
        db_session,
        workspace_uuid=claimed["workspace_uuid"],
        batch_size=20,
        lease_seconds=120,
        claim_token=claimed["claim_token"],
    )
    statements = [str(call.args[0]) for call in db_session.execute.await_args_list]
    assert rows == [claimed]
    assert "processing lease expired" in statements[0]
    assert "FOR UPDATE SKIP LOCKED" in statements[1]
    assert "attempt_count = snapshot.attempt_count + 1" in statements[1]
    assert "max_attempt" not in statements[1].lower()


@pytest.mark.asyncio
async def test_sent_updates_snapshot_and_events_in_same_transaction_scope() -> None:
    db_session = AsyncMock()
    db_session.execute.side_effect = [_Result(scalar="snap-1"), _Result()]
    changed = await billing.mark_billing_snapshot_sent(
        db_session,
        snapshot_id="snap-1",
        claim_token="22222222-2222-2222-2222-222222222222",
    )
    assert changed is True
    statements = [str(call.args[0]) for call in db_session.execute.await_args_list]
    assert "claim_token = CAST(:claim_token AS uuid)" in statements[0]
    assert "UPDATE orch_billing_events" in statements[1]
    assert "status = 'sent'" in statements[1]


def test_publish_enables_confirm_mandatory_and_exact_headers(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeChannel:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class FakeConnection:
        def __init__(self, *_args, **kwargs):
            captured["connection"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def channel(self):
            return FakeChannel()

    class FakeProducer:
        def __init__(self, _channel, on_return=None):
            captured["on_return"] = on_return

        def publish(self, payload, **kwargs):
            captured["payload"] = payload
            captured["publish"] = kwargs

    monkeypatch.setattr(billing, "Connection", FakeConnection)
    monkeypatch.setattr(billing, "Producer", FakeProducer)
    payload = billing.build_batch_snapshot_payload(
        snapshot_id="snap-1",
        workspace_uuid="11111111-1111-1111-1111-111111111111",
        billing_period=date(2026, 8, 1),
        snapshot_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        quantity=1,
        metric_code="service-orch",
        service_code="service-orch",
        application_code="target",
    )
    billing.publish_batch_billing_snapshot(snapshot=payload, settings=_settings())
    assert captured["connection"]["transport_options"] == {"confirm_publish": True}
    assert captured["publish"]["mandatory"] is True
    assert captured["publish"]["delivery_mode"] == 2
    assert captured["publish"]["confirm_timeout"] == 10.0
    assert captured["publish"]["message_id"] == "snap-1"
    assert captured["publish"]["headers"] == {
        "messageId": "snap-1",
        "source_application": "target",
        "schema_version": "v1",
        "workspace_uuid": "11111111-1111-1111-1111-111111111111",
    }


def test_unroutable_message_is_not_markable_as_success(monkeypatch) -> None:
    class FakeChannel:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class FakeConnection:
        def __init__(self, *_args, **_kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def channel(self):
            return FakeChannel()

    class FakeProducer:
        def __init__(self, _channel, on_return=None):
            self.on_return = on_return

        def publish(self, _payload, **_kwargs):
            assert self.on_return is not None
            self.on_return(RuntimeError("NO_ROUTE"), "domain.events", "missing", object())

    monkeypatch.setattr(billing, "Connection", FakeConnection)
    monkeypatch.setattr(billing, "Producer", FakeProducer)
    payload = billing.build_batch_snapshot_payload(
        snapshot_id="snap-1",
        workspace_uuid="11111111-1111-1111-1111-111111111111",
        billing_period=date(2026, 8, 1),
        snapshot_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        quantity=1,
        metric_code="service-orch",
        service_code="service-orch",
        application_code="target",
    )
    with pytest.raises(billing.UnroutableBillingMessage):
        billing.publish_batch_billing_snapshot(snapshot=payload, settings=_settings())


def test_error_sanitization_removes_broker_credentials() -> None:
    error = billing.sanitize_billing_error(RuntimeError("amqp://user:password@rabbitmq// failed"))
    assert "password" not in error
    assert "amqp://***:***@rabbitmq//" in error


@pytest.mark.asyncio
async def test_reconcile_uses_semiopen_interval_and_on_conflict() -> None:
    db_session = AsyncMock()
    db_session.execute.return_value = _Result([{"id": 1}, {"id": 2}])
    start, end = billing.parse_billing_period("2026-08")
    created = await billing.reconcile_billing_events(
        db_session,
        workspace_uuid="11111111-1111-1111-1111-111111111111",
        period_start=start,
        period_end=end,
        settings=_settings(),
    )
    statement = str(db_session.execute.await_args.args[0])
    params = db_session.execute.await_args.args[1]
    assert created == 2
    assert "created_at >= :period_start" in statement
    assert "created_at < :period_end" in statement
    assert "ON CONFLICT" in statement
    assert params["period_start"] == start
    assert params["period_end"] == end


@pytest.mark.asyncio
async def test_reprocess_idempotency_returns_existing_request() -> None:
    db_session = AsyncMock()
    db_session.execute.side_effect = [
        _Result(),
        _Result(
            [{
                "request_id": "33333333-3333-3333-3333-333333333333",
                "status": "accepted",
                "billing_period": date(2026, 8, 1),
                "requested_by": "operator@example.com",
                "reason": "audit",
            }]
        ),
    ]
    result = await billing.create_billing_reprocess_request(
        db_session,
        workspace_uuid="11111111-1111-1111-1111-111111111111",
        billing_period="2026-08",
        idempotency_key="22222222-2222-2222-2222-222222222222",
        requested_by="operator@example.com",
        reason="audit",
    )
    assert result.created is False
    assert result.request_id == "33333333-3333-3333-3333-333333333333"
    assert result.billing_period == "2026-08"
    assert "ON CONFLICT (workspace_uuid, idempotency_key) DO NOTHING" in str(db_session.execute.await_args_list[0].args[0])


@pytest.mark.asyncio
async def test_reprocess_idempotency_rejects_different_request_contract() -> None:
    db_session = AsyncMock()
    db_session.execute.side_effect = [
        _Result(),
        _Result(
            [{
                "request_id": "33333333-3333-3333-3333-333333333333",
                "status": "accepted",
                "billing_period": date(2026, 7, 1),
                "requested_by": "operator@example.com",
                "reason": "other month",
            }]
        ),
    ]
    with pytest.raises(billing.BillingIdempotencyConflict):
        await billing.create_billing_reprocess_request(
            db_session,
            workspace_uuid="11111111-1111-1111-1111-111111111111",
            billing_period="2026-08",
            idempotency_key="22222222-2222-2222-2222-222222222222",
            requested_by="operator@example.com",
            reason="audit",
        )


@pytest.mark.asyncio
async def test_reprocess_uses_canonical_sessions_and_defers_processing_snapshot() -> None:
    db_session = AsyncMock()
    db_session.execute.side_effect = [
        _Result(scalar=date(2026, 8, 1)),
        _Result([{"source_sessions": 10, "cursor_session_id": 99, "events_created": 2, "has_more": False}]),
        _Result(),
        _Result(scalar=2),
        _Result(scalar=1),
        _Result(),
    ]
    result = await billing.process_billing_reprocess_request(
        db_session,
        workspace_uuid="11111111-1111-1111-1111-111111111111",
        request_id="33333333-3333-3333-3333-333333333333",
        settings=_settings(),
    )
    assert result == {
        "source_sessions": 10,
        "events_created": 2,
        "snapshots_requeued": 2,
        "processing_deferred": 1,
        "completed": True,
    }
    statements = [str(call.args[0]) for call in db_session.execute.await_args_list]
    assert "FROM orch_sessions" in statements[1]
    assert "created_at >= :period_start" in statements[1]
    assert "created_at < :period_end" in statements[1]
    assert "LIMIT :candidate_limit" in statements[1]
    assert "reprocess_requested = TRUE" in statements[4]
    assert "status = 'completed'" in statements[5]


@pytest.mark.asyncio
async def test_reprocess_persists_chunk_cursor_before_continuation() -> None:
    db_session = AsyncMock()
    db_session.execute.side_effect = [
        _Result(scalar=date(2026, 8, 1)),
        _Result([{"source_sessions": 1000, "cursor_session_id": 5000, "events_created": 997, "has_more": True}]),
        _Result(),
    ]
    result = await billing.process_billing_reprocess_request(
        db_session,
        workspace_uuid="11111111-1111-1111-1111-111111111111",
        request_id="33333333-3333-3333-3333-333333333333",
        settings=_settings(),
    )
    assert result == {
        "source_sessions": 1000,
        "events_created": 997,
        "snapshots_requeued": 0,
        "processing_deferred": 0,
        "completed": False,
    }
    progress_sql = str(db_session.execute.await_args_list[2].args[0])
    assert "cursor_session_id = :cursor_session_id" in progress_sql
    assert "status = 'completed'" not in progress_sql


@pytest.mark.asyncio
async def test_reprocess_scanner_recovers_expired_running_and_lists_accepted() -> None:
    db_session = AsyncMock()
    db_session.execute.side_effect = [
        _Result(),
        _Result([{"request_id": "33333333-3333-3333-3333-333333333333"}]),
    ]
    request_ids = await billing.recover_and_list_billing_reprocess_requests(
        db_session,
        lease_seconds=3600,
        limit=20,
    )
    statements = [str(call.args[0]) for call in db_session.execute.await_args_list]
    assert request_ids == ["33333333-3333-3333-3333-333333333333"]
    assert "reprocess lease expired" in statements[0]
    assert "WHERE status = 'accepted'" in statements[1]
    assert "last_enqueued_at" in statements[1]
    assert "FOR UPDATE SKIP LOCKED" in statements[1]


@pytest.mark.asyncio
async def test_reprocess_claim_persists_running_before_processing() -> None:
    db_session = AsyncMock()
    db_session.execute.return_value = _Result(scalar="33333333-3333-3333-3333-333333333333")
    claimed = await billing.claim_billing_reprocess_request(
        db_session,
        workspace_uuid="11111111-1111-1111-1111-111111111111",
        request_id="33333333-3333-3333-3333-333333333333",
    )
    statement = str(db_session.execute.await_args.args[0])
    assert claimed is True
    assert "status = 'running'" in statement
    assert "status = 'accepted'" in statement


@pytest.mark.asyncio
async def test_status_contains_event_snapshot_quantity_and_attempt_counters() -> None:
    db_session = AsyncMock()
    oldest_event = datetime(2026, 8, 2, tzinfo=timezone.utc)
    oldest_snapshot = datetime(2026, 8, 3, tzinfo=timezone.utc)
    db_session.execute.side_effect = [
        _Result([{"pending": 1, "batched": 2, "sent": 3, "oldest_pending_at": oldest_event}]),
        _Result(
            [{
                "pending": 4,
                "processing": 5,
                "sent": 6,
                "failed": 7,
                "blocked": 8,
                "quantity_sent": 900,
                "oldest_pending_at": oldest_snapshot,
                "max_attempt_count": 11,
            }]
        ),
    ]
    status = await billing.get_billing_status(
        db_session,
        workspace_uuid="11111111-1111-1111-1111-111111111111",
        billing_period="2026-08",
    )
    assert status["events"] == {"pending": 1, "batched": 2, "sent": 3}
    assert status["snapshots"]["blocked"] == 8
    assert status["quantity_sent"] == 900
    assert status["oldest_pending_at"] == oldest_event
    assert status["max_attempt_count"] == 11


@pytest.mark.asyncio
async def test_failed_retry_persists_next_attempt_and_releases_lease() -> None:
    db_session = AsyncMock()
    db_session.execute.return_value = _Result(scalar="snap-1")
    next_attempt = datetime.now(timezone.utc) + timedelta(seconds=15)
    changed = await billing.mark_billing_snapshot_failed(
        db_session,
        snapshot_id="snap-1",
        claim_token="22222222-2222-2222-2222-222222222222",
        error="ConnectionError",
        next_attempt_at=next_attempt,
    )
    statement = str(db_session.execute.await_args.args[0])
    assert changed is True
    assert "status = 'failed'" in statement
    assert "claim_token = NULL" in statement
    assert db_session.execute.await_args.args[1]["next_attempt_at"] == next_attempt
