from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.api.v1 import orch as orch_api
from app.schemas.orch import OrchDialerSupplierV2TerminalRequest


WORKSPACE_UUID = UUID("ba7eb0ec-e565-447c-8c11-8f870cf72a60")
FLOW_UUID = UUID("8b81e493-b39c-4829-8b1e-5bafd00aeb7c")
SESSION_UUID = UUID("11111111-1111-4111-8111-111111111111")
REVISION_UUID = UUID("22222222-2222-4222-8222-222222222222")
COMPONENT_REF_ID = "33333333-3333-4333-8333-333333333333"
CYCLE_UUID = UUID("44444444-4444-4444-8444-444444444444")
ATTEMPT_UUID = UUID("55555555-5555-4555-8555-555555555555")
EVENT_UUID = UUID("66666666-6666-4666-8666-666666666666")


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _DbSession:
    def __init__(self) -> None:
        self.commits = 0

    def in_transaction(self) -> bool:
        return True

    def begin_nested(self) -> _Transaction:
        return _Transaction()

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


def _request() -> OrchDialerSupplierV2TerminalRequest:
    return OrchDialerSupplierV2TerminalRequest(
        event_id=EVENT_UUID,
        cycle_id=CYCLE_UUID,
        attempt_id=ATTEMPT_UUID,
        session_uuid=SESSION_UUID,
        flow_revision_id=REVISION_UUID,
        component_ref_id=COMPONENT_REF_ID,
        outcome="answered",
        terminal=True,
        terminal_reason="answered",
        occurred_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_archived_terminal_callback_is_accepted_without_resuming_active_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session = _DbSession()
    enqueued: list[dict] = []

    monkeypatch.setattr(
        orch_api,
        "_require_dialer_supplier_v2_client",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orch_api,
        "bind_workspace_context",
        lambda value: (value, f"ws_{value}"),
    )

    async def ensure_workspace(*_args: object, **_kwargs: object) -> None:
        return None

    async def persist_callback(*_args: object, **_kwargs: object) -> dict:
        return {
            "status": "accepted",
            "accepted": True,
            "idempotent": False,
            "resume_required": False,
            "session_id": 9102,
            "session_uuid": str(SESSION_UUID),
        }

    monkeypatch.setattr(orch_api, "ensure_active_workspace", ensure_workspace)
    monkeypatch.setattr(
        orch_api,
        "apply_dialer_supplier_v2_terminal_callback",
        persist_callback,
    )
    monkeypatch.setattr(
        orch_api.advance_session_task,
        "apply_async",
        lambda **kwargs: enqueued.append(kwargs),
    )
    monkeypatch.setattr(
        orch_api,
        "get_settings",
        lambda: SimpleNamespace(celery_execute_queue="must-not-be-used"),
    )

    response = await orch_api.callback_dialer_supplier_v2_by_workspace(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        request=_request(),
        x_client_id="client",
        x_client_secret="secret",
        db_session=db_session,  # type: ignore[arg-type]
    )

    assert response.accepted is True
    assert response.idempotent is False
    assert db_session.commits == 1
    assert enqueued == []


@pytest.mark.asyncio
async def test_active_terminal_callback_keeps_existing_resume_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session = _DbSession()
    enqueued: list[dict] = []
    queue = "orch_execute_test"

    monkeypatch.setattr(
        orch_api,
        "_require_dialer_supplier_v2_client",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orch_api,
        "bind_workspace_context",
        lambda value: (value, f"ws_{value}"),
    )

    async def ensure_workspace(*_args: object, **_kwargs: object) -> None:
        return None

    async def persist_callback(*_args: object, **_kwargs: object) -> dict:
        return {
            "status": "accepted",
            "accepted": True,
            "idempotent": False,
            "resume_required": True,
            "session_id": 9101,
            "session_uuid": str(SESSION_UUID),
        }

    monkeypatch.setattr(orch_api, "ensure_active_workspace", ensure_workspace)
    monkeypatch.setattr(
        orch_api,
        "apply_dialer_supplier_v2_terminal_callback",
        persist_callback,
    )
    monkeypatch.setattr(
        orch_api.advance_session_task,
        "apply_async",
        lambda **kwargs: enqueued.append(kwargs),
    )
    monkeypatch.setattr(
        orch_api,
        "get_settings",
        lambda: SimpleNamespace(celery_execute_queue=queue),
    )

    response = await orch_api.callback_dialer_supplier_v2_by_workspace(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        request=_request(),
        x_client_id="client",
        x_client_secret="secret",
        db_session=db_session,  # type: ignore[arg-type]
    )

    assert response.accepted is True
    assert enqueued == [
        {
            "kwargs": {
                "workspace_uuid": str(WORKSPACE_UUID),
                "flow_uuid": str(FLOW_UUID),
                "session_id": 9101,
            },
            "queue": queue,
            "routing_key": queue,
        }
    ]
