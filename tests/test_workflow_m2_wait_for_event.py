from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import app.services.workflow_m2_service as workflow
from app.services.workflow_revision_service import WorkflowRevisionResolution


FLOW_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REVISION_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SESSION_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
WAIT_REF = "11111111-1111-1111-1111-111111111111"
FINISH_REF = "22222222-2222-2222-2222-222222222222"


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _Result:
    def scalar_one(self) -> bool:
        return True


class _Session:
    def in_transaction(self) -> bool:
        return False

    def begin(self) -> _Transaction:
        return _Transaction()

    def begin_nested(self) -> _Transaction:
        return _Transaction()

    async def execute(self, *_args, **_kwargs) -> _Result:
        return _Result()


def _component(**parameters: object) -> dict:
    defaults = {
        "event_source": "callback",
        "event_result": "approved",
        "timeout_seconds": 3600,
        "output_var": "wait_event",
    }
    defaults.update(parameters)
    return {
        "ref_id": WAIT_REF,
        "component_id": "wait_for_event",
        "parameters": defaults,
    }


def _definition(*, branch: str) -> dict:
    return {
        "components": [
            _component(),
            {"ref_id": FINISH_REF, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [{"from": WAIT_REF, "to": FINISH_REF, "branch": branch}],
    }


def _runtime() -> dict:
    return {
        "workflow_v2": {
            "flow_id": FLOW_UUID,
            "revision_id": REVISION_UUID,
            "next_card_cursor": WAIT_REF,
        },
        "variables": {"payload": {}, "customs": {}},
    }


def _waiting_state(*, blocked_at: datetime, timeout_at: datetime, pending_start_index: int = 0) -> dict:
    return {
        "component_ref_id": WAIT_REF,
        "card_cursor": WAIT_REF,
        "event_source": "callback",
        "event_result": "approved",
        "timeout_seconds": 3600,
        "output_var": "wait_event",
        "pending_start_index": pending_start_index,
        "blocked_at": blocked_at.isoformat(),
        "timeout_at": timeout_at.isoformat(),
        "status": "waiting",
    }


def _configure_execution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    definition: dict,
    runtime: dict,
    frozen_until: datetime | None,
    last_card_uuid: str | None = None,
) -> tuple[list[dict], AsyncMock]:
    persisted: list[dict] = []
    clear_frozen = AsyncMock()

    monkeypatch.setattr(workflow, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(workflow, "fetch_flow_row", AsyncMock(return_value={"id": FLOW_UUID}))
    monkeypatch.setattr(
        workflow,
        "resolve_workflow_revision_for_session",
        AsyncMock(
            return_value=WorkflowRevisionResolution(
                revision={"id": REVISION_UUID, "definition": definition},
                source="pinned",
                requested_revision_id=REVISION_UUID,
                failure_reason=None,
            )
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_session_workflow_state",
        AsyncMock(
            return_value={
                "uuid": SESSION_UUID,
                "state": 0,
                "runtime_variables": runtime,
                "last_card_uuid": last_card_uuid,
                "next_card_uuid": WAIT_REF,
                "frozen_until": frozen_until,
            }
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_contact_runtime_context_for_session",
        AsyncMock(return_value=None),
    )

    async def _replace(*_args, **kwargs) -> None:
        persisted.append(kwargs)

    monkeypatch.setattr(workflow, "replace_session_workflow_state", _replace)
    monkeypatch.setattr(workflow, "clear_session_frozen_until", clear_frozen)
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())
    return persisted, clear_frozen


def test_wait_for_event_accepts_serialized_catalog_parameters() -> None:
    component = _component()
    component["parameters"] = [
        {"id": "event_source", "value": [{"id": "callback", "name": "Callback"}]},
        {"id": "event_result", "value": "APPROVED"},
        {"id": "timeout_seconds", "value": "60"},
        {"id": "output_var", "value": "approval_event"},
    ]

    assert workflow._wait_for_event_config(component) == (
        "callback",
        "approved",
        60,
        "approval_event",
    )


@pytest.mark.parametrize(
    ("parameters", "error_code"),
    [
        ({"event_source": "whatsapp"}, "wait_for_event_invalid_event_source"),
        ({"event_result": "{{dynamic}}"}, "wait_for_event_invalid_event_result"),
        ({"timeout_seconds": 0}, "wait_for_event_invalid_timeout_seconds"),
        ({"output_var": "wait-event"}, "wait_for_event_invalid_output_var"),
    ],
)
def test_wait_for_event_rejects_invalid_runtime_configuration(parameters: dict, error_code: str) -> None:
    component = _component(**parameters)

    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._wait_for_event_config(component)

    assert exc_info.value.code == error_code


@pytest.mark.asyncio
async def test_execute_wait_for_event_arms_once_and_preserves_prior_callbacks(monkeypatch) -> None:
    runtime = _runtime()
    runtime["callbacks_pending"] = [
        {
            "event_name": "callback",
            "entity": "person-1",
            "result": "approved",
            "received_at": "2026-09-06T12:00:00+00:00",
            "data": {"stale": True},
        }
    ]
    persisted, clear_frozen = _configure_execution(
        monkeypatch,
        definition=_definition(branch="received"),
        runtime=runtime,
        frozen_until=None,
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "blocked_wait_for_event"
    assert result.last_card_uuid == WAIT_REF
    assert result.next_card_uuid == WAIT_REF
    state = runtime["workflow_v2"]["wait_for_event"]
    assert state["pending_start_index"] == 1
    assert runtime["workflow_v2"]["blocking_stop_reason"] == "blocked_wait_for_event"
    assert persisted[-1]["frozen_until"].isoformat() == state["timeout_at"]
    clear_frozen.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_wait_for_event_resumes_on_matching_callback_without_consuming_others(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    runtime = _runtime()
    runtime["workflow_v2"].update(
        {
            "blocking_execution": True,
            "blocking_stop_reason": "blocked_wait_for_event",
            "wait_for_event": _waiting_state(
                blocked_at=now - timedelta(seconds=10),
                timeout_at=now + timedelta(minutes=30),
                pending_start_index=1,
            ),
        }
    )
    stale_callback = {
        "event_name": "callback",
        "entity": "person-1",
        "result": "approved",
        "received_at": (now - timedelta(minutes=5)).isoformat(),
        "data": {"stale": True},
    }
    unrelated_callback = {
        "event_name": "callback",
        "entity": "person-1",
        "result": "rejected",
        "received_at": now.isoformat(),
        "data": {"reason": "manual"},
    }
    matching_callback = {
        "event_name": "CALLBACK",
        "entity": "person-1",
        "result": "APPROVED",
        "received_at": now.isoformat(),
        "data": {"approval_id": "ap-1"},
    }
    runtime["callbacks_pending"] = [stale_callback, unrelated_callback, matching_callback]
    persisted, clear_frozen = _configure_execution(
        monkeypatch,
        definition=_definition(branch="received"),
        runtime=runtime,
        frozen_until=now + timedelta(minutes=30),
        last_card_uuid=WAIT_REF,
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert runtime["variables"]["customs"]["wait_event"] == {
        "status": "received",
        "event_source": "callback",
        "event_result": "approved",
        "received_at": matching_callback["received_at"],
        "data": {"approval_id": "ap-1"},
    }
    assert runtime["callbacks_pending"] == [stale_callback, unrelated_callback]
    assert "wait_for_event" not in runtime["workflow_v2"]
    assert "blocking_stop_reason" not in runtime["workflow_v2"]
    assert any(item.get("next_card_uuid") == FINISH_REF for item in persisted)
    assert clear_frozen.await_count >= 1


@pytest.mark.asyncio
async def test_execute_wait_for_event_times_out_and_preserves_late_callback(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    timeout_at = now - timedelta(seconds=1)
    runtime = _runtime()
    runtime["workflow_v2"].update(
        {
            "blocking_execution": True,
            "blocking_stop_reason": "blocked_wait_for_event",
            "wait_for_event": _waiting_state(
                blocked_at=now - timedelta(hours=1),
                timeout_at=timeout_at,
            ),
        }
    )
    late_callback = {
        "event_name": "callback",
        "entity": "person-1",
        "result": "approved",
        "received_at": now.isoformat(),
        "data": {"late": True},
    }
    runtime["callbacks_pending"] = [late_callback]
    _persisted, clear_frozen = _configure_execution(
        monkeypatch,
        definition=_definition(branch="timeout"),
        runtime=runtime,
        frozen_until=timeout_at,
        last_card_uuid=WAIT_REF,
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert runtime["variables"]["customs"]["wait_event"] == {
        "status": "timeout",
        "event_source": "callback",
        "event_result": "approved",
        "timeout_at": timeout_at.isoformat(),
    }
    assert runtime["callbacks_pending"] == [late_callback]
    clear_frozen.assert_awaited()


@pytest.mark.asyncio
async def test_unmatched_callback_does_not_bypass_active_timeout(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    timeout_at = now + timedelta(minutes=30)
    runtime = _runtime()
    runtime["workflow_v2"].update(
        {
            "blocking_execution": True,
            "blocking_stop_reason": "blocked_wait_for_event",
            "wait_for_event": _waiting_state(
                blocked_at=now - timedelta(seconds=10),
                timeout_at=timeout_at,
            ),
        }
    )
    runtime["callbacks_pending"] = [
        {
            "event_name": "callback",
            "entity": "person-1",
            "result": "rejected",
            "received_at": now.isoformat(),
            "data": {},
        }
    ]
    _persisted, clear_frozen = _configure_execution(
        monkeypatch,
        definition=_definition(branch="received"),
        runtime=runtime,
        frozen_until=timeout_at,
        last_card_uuid=WAIT_REF,
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "frozen_wait_active"
    assert runtime["workflow_v2"]["wait_for_event"]["timeout_at"] == timeout_at.isoformat()
    assert "wait_event" not in runtime["variables"]["customs"]
    clear_frozen.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_wait_configuration_uses_exception_branch(monkeypatch) -> None:
    invalid_component = _component(event_source="whatsapp")
    definition = {
        "components": [
            invalid_component,
            {"ref_id": FINISH_REF, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [{"from": WAIT_REF, "to": FINISH_REF, "branch": "exception"}],
    }
    runtime = _runtime()
    _persisted, clear_frozen = _configure_execution(
        monkeypatch,
        definition=definition,
        runtime=runtime,
        frozen_until=None,
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert runtime["wait_for_event_last_error"]["code"] == (
        "wait_for_event_invalid_event_source"
    )
    assert "wait_for_event" not in runtime["workflow_v2"]
    assert clear_frozen.await_count == 1
