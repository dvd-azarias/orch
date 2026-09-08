from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

import app.services.workflow_m2_service as workflow
from app.services.orch_trigger_service import m2_alarm_from_stopped_reason
from app.services.workflow_revision_service import WorkflowRevisionResolution


FLOW_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REVISION_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SESSION_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
CONTACT_LIST_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
PERSON_UUID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
SMS_REF = "11111111-1111-1111-1111-111111111111"


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


def _component() -> dict:
    return {
        "ref_id": SMS_REF,
        "component_id": "send_with_sms",
        "parameters": {
            "basic_token": "must-never-be-materialized",
            "message_pattern": "transactional",
            "message": "Hello {{contact.name}}",
            "wallet_code": "wallet",
            "supplier_code": "100",
            "callback_base_url": "https://callback.invalid",
            "callback_secret": "must-never-be-materialized",
        },
    }


def _definition() -> dict:
    return {"components": [_component()], "branches": []}


def _runtime(*, session_scope: str = "channel", member_id: int = 77) -> dict:
    return {
        "input_payload": {
            "session_scope": session_scope,
            "contact_list_member_id": member_id,
            "contact_list_id": CONTACT_LIST_UUID,
            "mailing_id": 1140,
            "channel_type": "sms",
        },
        "workflow_v2": {
            "flow_id": FLOW_UUID,
            "revision_id": REVISION_UUID,
            "next_card_cursor": SMS_REF,
        },
        "variables": {"payload": {}, "customs": {}},
    }


def _contact_row(*, member_id: int = 77, channel_type: str = "sms") -> dict:
    return {
        "contact_list_member_id": member_id,
        "contact_list_id": CONTACT_LIST_UUID,
        "mailing_id": "1140",
        "contact_identifier": "12345678901",
        "contact_name": "Contato",
        "contact_full_name": "Contato de Teste",
        "contact_channel_type": channel_type,
        "contact_channel_label": "celular",
        "contact_channel_address": "5511999990001",
        "contact_channel_extra_data": {},
        "person_uuid": PERSON_UUID,
        "is_primary": True,
    }


def _configure_execution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime: dict,
    contact_row: dict | None,
    session_state_overrides: dict | None = None,
) -> list[dict]:
    persisted: list[dict] = []
    session_state = {
        "uuid": SESSION_UUID,
        "state": 0,
        "entity_address": "5511999990001",
        "runtime_variables": runtime,
        "last_card_uuid": None,
        "next_card_uuid": SMS_REF,
        "frozen_until": None,
    }
    session_state.update(session_state_overrides or {})
    monkeypatch.setattr(workflow, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(
        workflow, "fetch_flow_row", AsyncMock(return_value={"id": FLOW_UUID})
    )
    monkeypatch.setattr(
        workflow,
        "resolve_workflow_revision_for_session",
        AsyncMock(
            return_value=WorkflowRevisionResolution(
                revision={"id": REVISION_UUID, "definition": _definition()},
                source="pinned",
                requested_revision_id=REVISION_UUID,
                failure_reason=None,
            )
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_session_workflow_state",
        AsyncMock(return_value=session_state),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_contact_runtime_context_for_session",
        AsyncMock(return_value=contact_row),
    )

    async def _replace(*_args, **kwargs) -> None:
        persisted.append(kwargs)

    monkeypatch.setattr(workflow, "replace_session_workflow_state", _replace)
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())
    return persisted


@pytest.mark.asyncio
async def test_channel_sms_marks_exact_member_and_blocks_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    persisted = _configure_execution(
        monkeypatch,
        runtime=runtime,
        contact_row=_contact_row(),
    )
    assign_sms = AsyncMock(
        return_value={
            "contact_list_member_id": 77,
            "linked_actuator": "sms",
            "mode": "marked",
        }
    )
    http_execute = Mock(side_effect=AssertionError("SMS marker-only cannot call HTTP"))
    monkeypatch.setattr(workflow, "assign_sms_routing_for_session", assign_sms)
    monkeypatch.setattr(workflow, "_http_execute", http_execute)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "blocked_send_with_sms"
    assert result.executed_steps == 1
    assert result.last_card_uuid == SMS_REF
    assert result.next_card_uuid is None
    assign_sms.assert_awaited_once()
    assert assign_sms.await_args.kwargs == {
        "flow_uuid": FLOW_UUID,
        "session_id": 123,
        "contact_list_member_id": 77,
        "contact_list_id": CONTACT_LIST_UUID,
        "mailing_id": 1140,
        "person_uuid": PERSON_UUID,
    }
    http_execute.assert_not_called()
    assert persisted[-1]["last_card_uuid"] == SMS_REF
    assert persisted[-1]["next_card_uuid"] is None
    assert runtime["workflow_v2"]["blocking_stop_reason"] == "blocked_send_with_sms"
    routing = runtime["send_with_sms_routing"]
    assert routing["component_ref_id"] == SMS_REF
    assert routing["assignment"] == {
        "contact_list_member_id": 77,
        "linked_actuator": "sms",
        "mode": "marked",
    }
    serialized_runtime = str(runtime)
    assert "must-never-be-materialized" not in serialized_runtime
    assert "Hello {{contact.name}}" not in serialized_runtime
    assert "callback.invalid" not in serialized_runtime


@pytest.mark.asyncio
async def test_person_sms_requires_and_uses_explicitly_selected_sms_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(session_scope="person")
    runtime["workflow_v2"]["selected_contact_channel"] = {
        "selected": True,
        "session_scope": "person",
        "contact_list_member_id": 88,
        "contact_list_id": CONTACT_LIST_UUID,
        "mailing_id": 1140,
        "person_uuid": PERSON_UUID,
        "type": "sms",
        "label": "celular_2",
        "address": "5511988880002",
    }
    _configure_execution(
        monkeypatch,
        runtime=runtime,
        contact_row={
            **_contact_row(member_id=88),
            "contact_channel_address": "5511988880002",
        },
    )
    assign_sms = AsyncMock(
        return_value={
            "contact_list_member_id": 88,
            "linked_actuator": "sms",
            "mode": "marked",
        }
    )
    monkeypatch.setattr(workflow, "assign_sms_routing_for_session", assign_sms)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "blocked_send_with_sms"
    assert assign_sms.await_args.kwargs["contact_list_member_id"] == 88
    assert assign_sms.await_args.kwargs["person_uuid"] == PERSON_UUID


@pytest.mark.asyncio
async def test_person_sms_without_channel_selection_terminalizes_without_marking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(session_scope="person")
    persisted = _configure_execution(
        monkeypatch,
        runtime=runtime,
        contact_row=_contact_row(),
    )
    assign_sms = AsyncMock()
    monkeypatch.setattr(workflow, "assign_sms_routing_for_session", assign_sms)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "person_scope_channel_component_not_supported"
    assert persisted[-1]["state"] == 3
    assert persisted[-1]["next_card_uuid"] is None
    assign_sms.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_type", ["phone", "voice"])
async def test_phone_or_voice_is_accepted_as_sms_capability(
    monkeypatch: pytest.MonkeyPatch,
    channel_type: str,
) -> None:
    runtime = _runtime()
    _configure_execution(
        monkeypatch,
        runtime=runtime,
        contact_row=_contact_row(channel_type=channel_type),
    )
    assign_sms = AsyncMock(
        return_value={
            "contact_list_member_id": 77,
            "linked_actuator": "sms",
            "mode": "marked",
        }
    )
    monkeypatch.setattr(workflow, "assign_sms_routing_for_session", assign_sms)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "blocked_send_with_sms"
    assert runtime["send_with_sms_routing"]["assignment"]["linked_actuator"] == "sms"
    assign_sms.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_type", ["email", "whatsapp"])
async def test_non_phone_channel_is_not_eligible_for_sms(
    monkeypatch: pytest.MonkeyPatch,
    channel_type: str,
) -> None:
    runtime = _runtime()
    persisted = _configure_execution(
        monkeypatch,
        runtime=runtime,
        contact_row=_contact_row(channel_type=channel_type),
    )
    assign_sms = AsyncMock()
    monkeypatch.setattr(workflow, "assign_sms_routing_for_session", assign_sms)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "send_with_sms_contact_not_eligible"
    assert runtime["workflow_v2"]["terminal_failure"]["code"] == (
        "send_with_sms_contact_not_eligible"
    )
    assert persisted[-1]["state"] == 3
    assert persisted[-1]["next_card_uuid"] is None
    assign_sms.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_contact_context_terminalizes_without_marking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    persisted = _configure_execution(
        monkeypatch,
        runtime=runtime,
        contact_row=None,
    )
    assign_sms = AsyncMock()
    monkeypatch.setattr(workflow, "assign_sms_routing_for_session", assign_sms)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "send_with_sms_contact_not_eligible"
    assert persisted[-1]["state"] == 3
    assert persisted[-1]["next_card_uuid"] is None
    assign_sms.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_sms_reentry_does_not_mark_or_execute_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime["workflow_v2"].update(
        {
            "blocking_execution": True,
            "blocking_stop_reason": "blocked_send_with_sms",
            "last_card_cursor": SMS_REF,
            "next_card_cursor": None,
        }
    )
    _configure_execution(
        monkeypatch,
        runtime=runtime,
        contact_row=_contact_row(),
        session_state_overrides={"last_card_uuid": SMS_REF, "next_card_uuid": None},
    )
    assign_sms = AsyncMock()
    monkeypatch.setattr(workflow, "assign_sms_routing_for_session", assign_sms)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "blocked_send_with_sms"
    assert result.executed_steps == 0
    assign_sms.assert_not_awaited()


def test_sms_block_and_terminal_alarm_are_registered() -> None:
    assert workflow._blocking_stop_reason_for_component("send_with_sms") == (
        "blocked_send_with_sms"
    )
    alarm = m2_alarm_from_stopped_reason("send_with_sms_contact_not_eligible")
    assert alarm == (
        "error",
        "workflow_m2_send_with_sms_contact_not_eligible",
        "Sessão encerrada porque o membro telefônico selecionado para SMS não permaneceu elegível para o handoff.",
    )
