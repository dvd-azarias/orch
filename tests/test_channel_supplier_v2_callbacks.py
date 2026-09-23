from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import app.services.workflow_m2_service as workflow
from app.services.channel_supplier_v2_callback_service import (
    ChannelSupplierV2CallbackError,
    normalize_channel_supplier_v2_callbacks,
)
from app.services.workflow_revision_service import WorkflowRevisionResolution


SESSION_UUID = "11111111-1111-4111-8111-111111111111"
FLOW_UUID = "22222222-2222-4222-8222-222222222222"
REVISION_UUID = "33333333-3333-4333-8333-333333333333"
COMPONENT_REF_ID = "44444444-4444-4444-8444-444444444444"
NEXT_REF_ID = "55555555-5555-4555-8555-555555555555"
FINISH_REF_ID = "66666666-6666-4666-8666-666666666666"
WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"


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


def _intent(channel: str) -> dict:
    return {
        "session_uuid": SESSION_UUID,
        "flow_uuid": FLOW_UUID,
        "flow_revision_id": REVISION_UUID,
        "component_ref_id": COMPONENT_REF_ID,
        "channel": channel,
        "dispatch_sequence": 1,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }


def _event(*, event_type: str, channel: str, row_id: int) -> dict:
    return {
        "id": row_id,
        "channel": channel,
        "event_type": event_type,
        "event_id": f"provider-{row_id}",
        "event_ts": datetime.now(timezone.utc),
        "received_at": datetime.now(timezone.utc),
        "payload": {
            "dispatch_identity": {
                "session_uuid": SESSION_UUID,
                "flow_uuid": FLOW_UUID,
                "flow_revision_id": REVISION_UUID,
                "component_ref_id": COMPONENT_REF_ID,
                "channel": channel,
                "dispatch_sequence": 1,
            },
            "provider_payload": {"payload": "mensagem de teste"},
        },
    }


def _configure_resume_execution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime: dict,
    definition: dict,
    next_card_uuid: str,
    events: list[dict | None],
    frozen_until: datetime | None = None,
) -> tuple[list[dict], AsyncMock, AsyncMock]:
    persisted: list[dict] = []
    mark = AsyncMock(return_value=1)
    clear_frozen = AsyncMock(return_value=1)
    enabled_settings = replace(
        workflow.get_settings(),
        channel_supplier_v2_enabled=True,
        channel_supplier_v2_workspace_allowlist=(WORKSPACE_UUID,),
        channel_supplier_v2_flow_allowlist=(FLOW_UUID,),
        channel_supplier_v2_callbacks_enabled=True,
    )
    monkeypatch.setattr(workflow, "get_settings", lambda: enabled_settings)
    monkeypatch.setattr(workflow, "get_current_workspace_uuid", lambda: WORKSPACE_UUID)
    monkeypatch.setattr(workflow, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(
        workflow,
        "fetch_flow_row",
        AsyncMock(return_value={"id": FLOW_UUID}),
    )
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
                "state": 1,
                "entity_address": "5511999990001",
                "runtime_variables": runtime,
                "last_card_uuid": COMPONENT_REF_ID,
                "next_card_uuid": next_card_uuid,
                "frozen_until": frozen_until,
            }
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_contact_runtime_context_for_session",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        workflow,
        "has_pending_channel_events",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_next_pending_channel_event",
        AsyncMock(side_effect=events),
    )
    monkeypatch.setattr(workflow, "mark_channel_event_processed", mark)
    monkeypatch.setattr(workflow, "clear_session_frozen_until", clear_frozen)

    async def _replace(*_args, **kwargs) -> None:
        persisted.append(kwargs)

    monkeypatch.setattr(workflow, "replace_session_workflow_state", _replace)
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())
    return persisted, mark, clear_frozen


@pytest.mark.parametrize(
    ("event_kind", "payload", "expected"),
    [
        ("status", {"messageid": "m-1", "status": 12}, "sent"),
        ("status", {"messageid": "m-1", "status": 4}, "sent"),
        ("dlr", {"messageid": "m-1", "status": 1}, "delivered"),
        ("dlr", {"messageid": "m-1", "status": 2}, "not_delivered"),
        ("mo", {"messageid": "m-1", "mensagem": "Resposta"}, "response"),
    ],
)
def test_sms_callbacks_follow_provider_status_contract(
    event_kind: str,
    payload: dict,
    expected: str,
) -> None:
    result = normalize_channel_supplier_v2_callbacks(
        channel="sms",
        event_kind=event_kind,
        payload=payload,
    )

    assert [item.event_type for item in result] == [expected]


@pytest.mark.parametrize(
    ("status_value", "expected"),
    [
        ("Enviado", "sent"),
        ("Entregue", "delivered"),
        ("Lido", "read"),
        ("Indisponível", "unavailable"),
        ("Não entregue", "failed"),
        ("Expirado", "expired"),
    ],
)
def test_rcs_callbacks_normalize_official_lifecycle(
    status_value: str,
    expected: str,
) -> None:
    result = normalize_channel_supplier_v2_callbacks(
        channel="rcs",
        event_kind="status",
        payload={"message_id": "rcs-1", "status": status_value},
    )

    assert result[0].event_type == expected


def test_callback_without_provider_message_id_is_rejected() -> None:
    with pytest.raises(ChannelSupplierV2CallbackError) as exc_info:
        normalize_channel_supplier_v2_callbacks(
            channel="sms",
            event_kind="status",
            payload={"status": 12},
        )

    assert exc_info.value.code == "channel_supplier_v2_callback_event_id_invalid"


@pytest.mark.asyncio
async def test_sms_lifecycle_event_is_preserved_without_advancing_send_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = {
        "workflow_v2": {
            "channel_dispatch_v2": _intent("sms"),
            "next_card_cursor": NEXT_REF_ID,
        }
    }
    events = [
        _event(event_type="response", channel="sms", row_id=1),
        _event(event_type="sent", channel="sms", row_id=2),
        None,
    ]
    fetch = AsyncMock(side_effect=events)
    mark = AsyncMock(return_value=1)
    monkeypatch.setattr(workflow, "fetch_next_pending_channel_event", fetch)
    monkeypatch.setattr(workflow, "mark_channel_event_processed", mark)

    decision = await workflow._consume_channel_supplier_v2_events(
        object(),  # type: ignore[arg-type]
        session_id=10,
        runtime_variables=runtime,
        blocking_stop_reason="blocked_send_with_sms",
    )

    assert decision.terminal is False
    assert decision.changed is True
    assert decision.channel == "sms"
    assert runtime["callbacks_pending"][0]["event_name"] == "callback"
    assert runtime["callbacks_pending"][0]["result"] == "sms_event"
    assert runtime["callbacks_pending"][0]["data"]["status"] == "response"
    assert runtime["callbacks_pending"][0]["data"]["correlation_key"].startswith(
        "cdv2:sms:"
    )
    assert "wait_for_event_activation_override" not in runtime["workflow_v2"]
    assert mark.await_count == 2


@pytest.mark.asyncio
async def test_sms_provider_acceptance_advances_without_fabricating_lifecycle_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent("sms")
    intent.update(
        {
            "status": "provider_accepted",
            "dispatch_id": "13131313-1313-4313-8313-131313131313",
            "provider_message_id": "provider-message-1",
            "provider_status": "13",
            "accepted_at": "2026-09-23T12:00:00+00:00",
        }
    )
    runtime = {
        "workflow_v2": {
            "channel_dispatch_v2": intent,
            "next_card_cursor": NEXT_REF_ID,
        }
    }
    monkeypatch.setattr(
        workflow,
        "fetch_next_pending_channel_event",
        AsyncMock(return_value=None),
    )

    decision = await workflow._consume_channel_supplier_v2_events(
        object(),  # type: ignore[arg-type]
        session_id=10,
        runtime_variables=runtime,
        blocking_stop_reason="blocked_send_with_sms",
    )

    assert decision.terminal is True
    assert decision.branch_label == "next"
    assert "callbacks_pending" not in runtime
    assert runtime["send_with_sms_last_result"] == {
        "component_ref_id": COMPONENT_REF_ID,
        "dispatch_sequence": 1,
        "dispatch_id": "13131313-1313-4313-8313-131313131313",
        "event_type": "accepted",
        "event_id": "provider-message-1",
        "provider_status": "13",
        "event_at": "2026-09-23T12:00:00+00:00",
    }
    assert runtime["workflow_v2"]["wait_for_event_activation_override"][
        "card_cursor"
    ] == NEXT_REF_ID


@pytest.mark.asyncio
async def test_rcs_waits_for_configured_read_after_delivered_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent("rcs")
    runtime = {
        "workflow_v2": {
            "channel_dispatch_v2": intent,
            "channel_dispatch_v2_wait": {
                "component_ref_id": COMPONENT_REF_ID,
                "channel": "rcs",
                "dispatch_sequence": 1,
                "completion_event": "read",
                "timeout_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "status": "waiting",
            },
        }
    }
    fetch = AsyncMock(
        side_effect=[
            _event(event_type="delivered", channel="rcs", row_id=1),
            _event(event_type="read", channel="rcs", row_id=2),
        ]
    )
    mark = AsyncMock(return_value=1)
    monkeypatch.setattr(workflow, "fetch_next_pending_channel_event", fetch)
    monkeypatch.setattr(workflow, "mark_channel_event_processed", mark)

    decision = await workflow._consume_channel_supplier_v2_events(
        object(),  # type: ignore[arg-type]
        session_id=10,
        runtime_variables=runtime,
        blocking_stop_reason="blocked_send_with_rcs",
    )

    assert decision.terminal is True
    assert decision.branch_label == "read"
    assert runtime["workflow_v2"]["channel_dispatch_v2_wait"]["outcome"] == "read"
    assert mark.await_args_list[0].kwargs["discard_reason"] == (
        "channel_supplier_v2_rcs_telemetry"
    )
    assert mark.await_args_list[1].kwargs["discard_reason"] is None


@pytest.mark.asyncio
async def test_rcs_timeout_is_terminal_without_provider_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = {
        "workflow_v2": {
            "channel_dispatch_v2": _intent("rcs"),
            "channel_dispatch_v2_wait": {
                "component_ref_id": COMPONENT_REF_ID,
                "channel": "rcs",
                "dispatch_sequence": 1,
                "completion_event": "response",
                "timeout_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                "status": "waiting",
            },
        }
    }
    monkeypatch.setattr(
        workflow,
        "fetch_next_pending_channel_event",
        AsyncMock(return_value=None),
    )

    decision = await workflow._consume_channel_supplier_v2_events(
        object(),  # type: ignore[arg-type]
        session_id=10,
        runtime_variables=runtime,
        blocking_stop_reason="blocked_send_with_rcs",
    )

    assert decision.terminal is True
    assert decision.branch_label == "timeout"


@pytest.mark.asyncio
async def test_sms_mo_after_send_card_is_forwarded_without_reopening_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = {"workflow_v2": {"channel_dispatch_v2": _intent("sms")}}
    fetch = AsyncMock(
        side_effect=[
            _event(event_type="response", channel="sms", row_id=1),
            None,
        ]
    )
    mark = AsyncMock(return_value=1)
    monkeypatch.setattr(workflow, "fetch_next_pending_channel_event", fetch)
    monkeypatch.setattr(workflow, "mark_channel_event_processed", mark)

    decision = await workflow._consume_channel_supplier_v2_events(
        object(),  # type: ignore[arg-type]
        session_id=10,
        runtime_variables=runtime,
        blocking_stop_reason="blocked_wait_for_event",
    )

    assert decision.changed is True
    assert decision.terminal is False
    assert runtime["callbacks_pending"][0]["result"] == "sms_event"
    assert "discard_reason" not in mark.await_args.kwargs


@pytest.mark.asyncio
async def test_sms_acceptance_resumes_card_and_preserves_raced_events_for_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent("sms")
    intent.update(
        {
            "status": "provider_accepted",
            "dispatch_id": "13131313-1313-4313-8313-131313131313",
            "provider_message_id": "provider-message-1",
            "provider_status": "13",
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    runtime = {
        "workflow_v2": {
            "flow_id": FLOW_UUID,
            "revision_id": REVISION_UUID,
            "channel_dispatch_v2": intent,
            "blocking_execution": True,
            "blocking_stop_reason": "blocked_send_with_sms",
            "last_card_cursor": COMPONENT_REF_ID,
            "next_card_cursor": NEXT_REF_ID,
        },
        "variables": {"payload": {}, "customs": {}},
    }
    runtime["variables"]["customs"]["sms_dispatch"] = {
        "correlation_key": workflow.build_channel_dispatch_correlation_key(
            session_uuid=SESSION_UUID,
            flow_uuid=FLOW_UUID,
            flow_revision_id=REVISION_UUID,
            component_ref_id=COMPONENT_REF_ID,
            channel="sms",
            dispatch_sequence=1,
        )
    }
    definition = {
        "components": [
            {
                "ref_id": COMPONENT_REF_ID,
                "component_id": "send_with_sms",
                "parameters": {},
            },
            {
                "ref_id": NEXT_REF_ID,
                "component_id": "wait_for_event",
                "parameters": {
                    "event_source": "callback",
                    "event_result": "sms_event",
                    "correlation_key": "{{customs.sms_dispatch.correlation_key}}",
                    "timeout_seconds": 300,
                    "output_var": "sms_reply",
                },
            },
            {
                "ref_id": FINISH_REF_ID,
                "component_id": "finish_flow",
                "parameters": {},
            },
        ],
        "branches": [
            {
                "from": COMPONENT_REF_ID,
                "to": NEXT_REF_ID,
                "branch": "next",
            },
            {
                "from": NEXT_REF_ID,
                "to": FINISH_REF_ID,
                "branch": "received",
            },
        ],
    }
    persisted, mark, clear_frozen = _configure_resume_execution(
        monkeypatch,
        runtime=runtime,
        definition=definition,
        next_card_uuid=NEXT_REF_ID,
        events=[
            _event(event_type="response", channel="sms", row_id=1),
            _event(event_type="sent", channel="sms", row_id=2),
            None,
        ],
    )

    session = _Session()
    result = await workflow.execute_workflow_m2_for_session(
        session,  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=10,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.last_card_uuid == FINISH_REF_ID
    assert runtime["variables"]["customs"]["sms_reply"]["data"][
        "channel"
    ] == "sms"
    assert len(runtime["callbacks_pending"]) == 1
    assert runtime["callbacks_pending"][0]["data"]["status"] == "sent"
    assert "blocking_stop_reason" not in runtime["workflow_v2"]
    assert mark.await_count == 2
    assert clear_frozen.await_count == 2
    assert all(
        call.args == (session,) and call.kwargs == {"session_id": 10}
        for call in clear_frozen.await_args_list
    )
    assert persisted[-1]["state"] == 3


@pytest.mark.asyncio
async def test_sms_acceptance_enters_generic_wait_without_lifecycle_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent("sms")
    intent.update(
        {
            "status": "provider_accepted",
            "dispatch_id": "13131313-1313-4313-8313-131313131313",
            "provider_message_id": "provider-message-1",
            "provider_status": "13",
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    runtime = {
        "workflow_v2": {
            "flow_id": FLOW_UUID,
            "revision_id": REVISION_UUID,
            "channel_dispatch_v2": intent,
            "blocking_execution": True,
            "blocking_stop_reason": "blocked_send_with_sms",
            "last_card_cursor": COMPONENT_REF_ID,
            "next_card_cursor": NEXT_REF_ID,
        },
        "variables": {"payload": {}, "customs": {}},
    }
    runtime["variables"]["customs"]["sms_dispatch"] = {
        "correlation_key": workflow.build_channel_dispatch_correlation_key(
            session_uuid=SESSION_UUID,
            flow_uuid=FLOW_UUID,
            flow_revision_id=REVISION_UUID,
            component_ref_id=COMPONENT_REF_ID,
            channel="sms",
            dispatch_sequence=1,
        )
    }
    definition = {
        "components": [
            {
                "ref_id": COMPONENT_REF_ID,
                "component_id": "send_with_sms",
                "parameters": {},
            },
            {
                "ref_id": NEXT_REF_ID,
                "component_id": "wait_for_event",
                "parameters": {
                    "event_source": "callback",
                    "event_result": "sms_event",
                    "correlation_key": "{{customs.sms_dispatch.correlation_key}}",
                    "timeout_seconds": 300,
                    "output_var": "sms_reply",
                },
            },
        ],
        "branches": [
            {
                "from": COMPONENT_REF_ID,
                "to": NEXT_REF_ID,
                "branch": "next",
            },
        ],
    }
    persisted, _mark, _clear_frozen = _configure_resume_execution(
        monkeypatch,
        runtime=runtime,
        definition=definition,
        next_card_uuid=NEXT_REF_ID,
        events=[None],
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=10,
    )

    assert result.stopped_reason == "blocked_wait_for_event"
    assert result.last_card_uuid == NEXT_REF_ID
    assert "callbacks_pending" not in runtime
    assert runtime["workflow_v2"]["blocking_stop_reason"] == (
        "blocked_wait_for_event"
    )
    assert runtime["send_with_sms_last_result"]["event_type"] == "accepted"
    assert persisted


@pytest.mark.asyncio
async def test_rcs_read_callback_selects_exact_branch_and_bypasses_future_freeze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent("rcs")
    timeout_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    runtime = {
        "workflow_v2": {
            "flow_id": FLOW_UUID,
            "revision_id": REVISION_UUID,
            "channel_dispatch_v2": intent,
            "channel_dispatch_v2_wait": {
                "component_ref_id": COMPONENT_REF_ID,
                "channel": "rcs",
                "dispatch_sequence": 1,
                "completion_event": "read",
                "timeout_at": timeout_at.isoformat(),
                "status": "waiting",
            },
            "blocking_execution": True,
            "blocking_stop_reason": "blocked_send_with_rcs",
            "last_card_cursor": COMPONENT_REF_ID,
            "next_card_cursor": FINISH_REF_ID,
        },
        "variables": {"payload": {}, "customs": {}},
    }
    definition = {
        "components": [
            {
                "ref_id": COMPONENT_REF_ID,
                "component_id": "send_with_rcs",
                "parameters": {},
            },
            {
                "ref_id": FINISH_REF_ID,
                "component_id": "finish_flow",
                "parameters": {},
            },
        ],
        "branches": [
            {
                "from": COMPONENT_REF_ID,
                "to": FINISH_REF_ID,
                "branch": "read",
            },
        ],
    }
    persisted, mark, clear_frozen = _configure_resume_execution(
        monkeypatch,
        runtime=runtime,
        definition=definition,
        next_card_uuid=FINISH_REF_ID,
        events=[_event(event_type="read", channel="rcs", row_id=1)],
        frozen_until=timeout_at,
    )

    session = _Session()
    result = await workflow.execute_workflow_m2_for_session(
        session,  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=10,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.last_card_uuid == FINISH_REF_ID
    assert runtime["workflow_v2"]["channel_dispatch_v2_wait"]["outcome"] == (
        "read"
    )
    assert "blocking_stop_reason" not in runtime["workflow_v2"]
    mark.assert_awaited_once()
    clear_frozen.assert_awaited_once_with(session, session_id=10)
    assert persisted[-1]["state"] == 3
