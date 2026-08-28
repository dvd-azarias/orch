from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi import HTTPException

import app.api.v1.orch as orch_api
from app.schemas.orch import OrchBillingReprocessRequest
from app.services.billing_batch_service import ReprocessRequest


class _DbSession:
    def __init__(self) -> None:
        self.execute = AsyncMock()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()


def _settings(**overrides):  # type: ignore[no-untyped-def]
    values = {
        "orch_billing_enabled": True,
        "billing_admin_client_id": "billing-admin",
        "billing_admin_client_secret": "billing-secret",
        "celery_billing_queue": "orch.billing.outbox",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_billing_admin_auth_is_fail_closed_when_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(orch_api, "get_settings", lambda: _settings(billing_admin_client_id=None, billing_admin_client_secret=None))
    with pytest.raises(HTTPException) as exc_info:
        orch_api._require_billing_admin(client_id="billing-admin", client_secret="billing-secret")
    assert exc_info.value.status_code == 401


def test_billing_admin_auth_accepts_dedicated_credentials(monkeypatch) -> None:
    monkeypatch.setattr(orch_api, "get_settings", lambda: _settings())
    orch_api._require_billing_admin(client_id="billing-admin", client_secret="billing-secret")


@pytest.mark.asyncio
async def test_reprocess_endpoint_persists_before_enqueue_and_returns_202_contract(monkeypatch) -> None:
    db_session = _DbSession()
    order: list[str] = []

    async def _create(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        order.append("persist")
        return ReprocessRequest(
            request_id="33333333-3333-3333-3333-333333333333",
            status="accepted",
            created=True,
            billing_period="2026-08",
        )

    async def _commit():
        order.append("commit")

    def _enqueue(**_kwargs):  # type: ignore[no-untyped-def]
        order.append("enqueue")

    db_session.commit.side_effect = _commit
    monkeypatch.setattr(orch_api, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        orch_api,
        "bind_workspace_context",
        lambda value: (value, f"ws_{value}"),
    )
    monkeypatch.setattr(orch_api, "ensure_active_workspace", AsyncMock(return_value={}))
    monkeypatch.setattr(orch_api, "create_billing_reprocess_request", _create)
    monkeypatch.setattr(orch_api, "mark_billing_reprocess_enqueued", AsyncMock())
    monkeypatch.setattr(orch_api.billing_reprocess_task, "apply_async", _enqueue)

    workspace_uuid = UUID("11111111-1111-1111-1111-111111111111")
    response = await orch_api.reprocess_service_orch_billing(
        workspace_uuid=workspace_uuid,
        request=OrchBillingReprocessRequest(billing_period="2026-08", reason="audit gap"),
        idempotency_key=UUID("22222222-2222-2222-2222-222222222222"),
        requested_by="operator@example.com",
        x_client_id="billing-admin",
        x_client_secret="billing-secret",
        db_session=db_session,  # type: ignore[arg-type]
    )

    assert order == ["persist", "commit", "enqueue", "commit"]
    assert response.status == "accepted"
    assert response.enqueued is True
    assert response.idempotent is False


@pytest.mark.asyncio
async def test_reprocess_endpoint_keeps_accepted_when_enqueue_fails(monkeypatch) -> None:
    db_session = _DbSession()
    monkeypatch.setattr(orch_api, "get_settings", lambda: _settings())
    monkeypatch.setattr(orch_api, "bind_workspace_context", lambda value: (value, f"ws_{value}"))
    monkeypatch.setattr(orch_api, "ensure_active_workspace", AsyncMock(return_value={}))
    monkeypatch.setattr(
        orch_api,
        "create_billing_reprocess_request",
        AsyncMock(
            return_value=ReprocessRequest(
                request_id="33333333-3333-3333-3333-333333333333",
                status="accepted",
                created=True,
                billing_period="2026-08",
            )
        ),
    )
    monkeypatch.setattr(
        orch_api.billing_reprocess_task,
        "apply_async",
        Mock(side_effect=RuntimeError("broker unavailable")),
    )

    response = await orch_api.reprocess_service_orch_billing(
        workspace_uuid=UUID("11111111-1111-1111-1111-111111111111"),
        request=OrchBillingReprocessRequest(billing_period="2026-08", reason="audit gap"),
        idempotency_key=UUID("22222222-2222-2222-2222-222222222222"),
        requested_by="operator@example.com",
        x_client_id="billing-admin",
        x_client_secret="billing-secret",
        db_session=db_session,  # type: ignore[arg-type]
    )
    assert response.status == "accepted"
    assert response.enqueued is False


@pytest.mark.asyncio
async def test_status_endpoint_returns_all_required_counters(monkeypatch) -> None:
    db_session = _DbSession()
    summary = {
        "workspace_uuid": "11111111-1111-1111-1111-111111111111",
        "billing_period": "2026-08",
        "events": {"pending": 1, "batched": 2, "sent": 3},
        "snapshots": {"pending": 4, "processing": 5, "sent": 6, "failed": 7, "blocked": 8},
        "quantity_sent": 900,
        "oldest_pending_at": None,
        "max_attempt_count": 11,
    }
    monkeypatch.setattr(orch_api, "get_settings", lambda: _settings())
    monkeypatch.setattr(orch_api, "bind_workspace_context", lambda value: (value, f"ws_{value}"))
    monkeypatch.setattr(orch_api, "ensure_active_workspace", AsyncMock(return_value={}))
    monkeypatch.setattr(orch_api, "get_billing_status", AsyncMock(return_value=summary))

    response = await orch_api.get_service_orch_billing_status(
        workspace_uuid=UUID(summary["workspace_uuid"]),
        billing_period="2026-08",
        x_client_id="billing-admin",
        x_client_secret="billing-secret",
        db_session=db_session,  # type: ignore[arg-type]
    )
    assert response.events.model_dump() == {"pending": 1, "batched": 2, "sent": 3}
    assert response.snapshots.blocked == 8
    assert response.quantity_sent == 900
    assert response.max_attempt_count == 11
