from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.tasks import channel_supplier_v2_tasks as tasks
from app.services.channel_supplier_v2_service import ChannelDispatchStatusResult


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
FLOW_UUID = "c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152"


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "channel_supplier_v2_enabled": True,
        "channel_supplier_v2_workspace_allowlist": (WORKSPACE_UUID,),
        "channel_supplier_v2_flow_allowlist": (FLOW_UUID,),
        "channel_supplier_v2_registration_lease_seconds": 120,
        "channel_supplier_v2_reconcile_batch_size": 100,
        "celery_channel_supplier_v2_queue": "orch_channel_supplier_v2_test",
        "celery_execute_queue": "orch_execute_test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Transaction:
    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_args):  # noqa: ANN204, ANN002
        return None


class _Mappings:
    def all(self) -> list[dict]:
        return [{"id": 71, "flow_uuid": FLOW_UUID, "attempts": 2}]


class _Rows:
    def mappings(self) -> _Mappings:
        return _Mappings()


class _Session:
    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_args):  # noqa: ANN204, ANN002
        return None

    def begin(self) -> _Transaction:
        return _Transaction()

    def in_transaction(self) -> bool:
        return False

    async def commit(self) -> None:
        return None

    async def execute(self, statement, _parameters=None):  # noqa: ANN001, ANN201
        if 'FROM "orch_sessions"' in str(statement):
            return _Rows()
        return object()


@pytest.mark.asyncio
async def test_reconciler_recovers_only_allowlisted_workspace_on_dedicated_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    enqueued: list[dict] = []
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "get_session_factory", lambda: (lambda: _Session()))
    monkeypatch.setattr(
        tasks,
        "list_completed_workspaces",
        lambda _session: _async_value(
            [
                {"workspace_uuid": WORKSPACE_UUID},
                {"workspace_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
            ]
        ),
    )
    monkeypatch.setattr(
        tasks,
        "bind_workspace_context",
        lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"),
    )
    monkeypatch.setattr(
        tasks.register_channel_supplier_v2_dispatch_task,
        "apply_async",
        lambda **kwargs: enqueued.append(kwargs),
    )

    result = await tasks._reconcile_pending_channel_supplier_v2_dispatches_task()

    assert result == {"scanned": 1, "enqueued": 1}
    assert enqueued[0]["queue"] == "orch_channel_supplier_v2_test"
    assert enqueued[0]["kwargs"] == {
        "workspace_uuid": WORKSPACE_UUID,
        "flow_uuid": FLOW_UUID,
        "session_id": 71,
        "recovery_attempt": 3,
    }


@pytest.mark.asyncio
async def test_reconciler_is_inert_when_feature_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(channel_supplier_v2_enabled=False)
    )

    assert await tasks._reconcile_pending_channel_supplier_v2_dispatches_task() == {
        "scanned": 0,
        "enqueued": 0,
    }


@pytest.mark.asyncio
async def test_accepted_sms_transitions_once_and_schedules_workflow_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = {
        "idempotency_key": "orch:v2:channel-dispatch:" + ("a" * 64),
        "dispatch_id": "13131313-1313-4313-8313-131313131313",
    }
    enqueued: list[dict] = []
    monkeypatch.setattr(
        tasks,
        "get_channel_dispatch",
        lambda **_kwargs: ChannelDispatchStatusResult(
            dispatch_id=intent["dispatch_id"],
            state="accepted",
            provider_message_id="provider-message-1",
            provider_status="13",
            accepted_at="2026-09-23T12:00:00+00:00",
            failed_at=None,
            uncertain_at=None,
            last_error_code=None,
            last_error_message=None,
        ),
    )
    monkeypatch.setattr(
        tasks,
        "_transition_registered_dispatch",
        lambda **_kwargs: _async_value(True),
    )
    from app.tasks import workflow_tasks

    monkeypatch.setattr(
        workflow_tasks.resume_channel_supplier_v2_acceptance_task,
        "apply_async",
        lambda **kwargs: enqueued.append(kwargs),
    )

    result = await tasks._sync_registered_dispatch(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        session_id=71,
        intent=intent,
        settings=_settings(),
    )

    assert result == {
        "status": "provider_accepted",
        "dispatch_id": intent["dispatch_id"],
        "transitioned": True,
    }
    assert enqueued == [
        {
            "kwargs": {
                "workspace_uuid": WORKSPACE_UUID,
                "flow_uuid": FLOW_UUID,
                "session_id": 71,
            },
            "queue": "orch_execute_test",
            "routing_key": "orch_execute_test",
        }
    ]


@pytest.mark.asyncio
async def test_pending_sms_remains_registered_without_resuming_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = {
        "idempotency_key": "orch:v2:channel-dispatch:" + ("a" * 64),
        "dispatch_id": "13131313-1313-4313-8313-131313131313",
    }
    monkeypatch.setattr(
        tasks,
        "get_channel_dispatch",
        lambda **_kwargs: ChannelDispatchStatusResult(
            dispatch_id=intent["dispatch_id"],
            state="dispatching",
            provider_message_id=None,
            provider_status=None,
            accepted_at=None,
            failed_at=None,
            uncertain_at=None,
            last_error_code=None,
            last_error_message=None,
        ),
    )

    result = await tasks._sync_registered_dispatch(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        session_id=71,
        intent=intent,
        settings=_settings(),
    )

    assert result == {
        "status": "registered",
        "dispatch_id": intent["dispatch_id"],
        "dispatch_state": "dispatching",
    }


@pytest.mark.asyncio
async def test_duplicate_acceptance_does_not_schedule_second_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = {
        "idempotency_key": "orch:v2:channel-dispatch:" + ("a" * 64),
        "dispatch_id": "13131313-1313-4313-8313-131313131313",
    }
    monkeypatch.setattr(
        tasks,
        "get_channel_dispatch",
        lambda **_kwargs: ChannelDispatchStatusResult(
            dispatch_id=intent["dispatch_id"],
            state="accepted",
            provider_message_id="provider-message-1",
            provider_status="13",
            accepted_at="2026-09-23T12:00:00+00:00",
            failed_at=None,
            uncertain_at=None,
            last_error_code=None,
            last_error_message=None,
        ),
    )
    monkeypatch.setattr(
        tasks,
        "_transition_registered_dispatch",
        lambda **_kwargs: _async_value(False),
    )
    from app.tasks import workflow_tasks

    enqueued: list[dict] = []
    monkeypatch.setattr(
        workflow_tasks.resume_channel_supplier_v2_acceptance_task,
        "apply_async",
        lambda **kwargs: enqueued.append(kwargs),
    )

    result = await tasks._sync_registered_dispatch(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        session_id=71,
        intent=intent,
        settings=_settings(),
    )

    assert result["status"] == "provider_accepted"
    assert result["transitioned"] is False
    assert enqueued == []


@pytest.mark.asyncio
async def test_failed_sms_is_recorded_without_advancing_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = {
        "idempotency_key": "orch:v2:channel-dispatch:" + ("a" * 64),
        "dispatch_id": "13131313-1313-4313-8313-131313131313",
    }
    monkeypatch.setattr(
        tasks,
        "get_channel_dispatch",
        lambda **_kwargs: ChannelDispatchStatusResult(
            dispatch_id=intent["dispatch_id"],
            state="failed",
            provider_message_id=None,
            provider_status="rejected",
            accepted_at=None,
            failed_at="2026-09-23T12:00:00+00:00",
            uncertain_at=None,
            last_error_code="provider_rejected",
            last_error_message="provider rejected request",
        ),
    )
    monkeypatch.setattr(
        tasks,
        "_transition_registered_dispatch",
        lambda **_kwargs: _async_value(True),
    )

    result = await tasks._sync_registered_dispatch(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        session_id=71,
        intent=intent,
        settings=_settings(),
    )

    assert result == {
        "status": "provider_failed",
        "dispatch_id": intent["dispatch_id"],
        "transitioned": True,
    }


async def _async_value(value):  # type: ignore[no-untyped-def]
    return value
