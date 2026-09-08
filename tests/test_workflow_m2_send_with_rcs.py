from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

import app.services.workflow_m2_service as workflow
from app.services import workflow_dispatcher_service
from app.services.orch_trigger_service import m2_alarm_from_stopped_reason
from app.services.workflow_revision_service import WorkflowRevisionResolution


FLOW_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REVISION_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SESSION_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
CONTACT_LIST_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
PERSON_UUID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
RCS_REF = "11111111-1111-1111-1111-111111111111"


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
        "ref_id": RCS_REF,
        "component_id": "send_with_rcs",
        "parameters": {"message_template": "Olá {{contact.full_name}}"},
    }


def _runtime(*, session_scope: str = "channel", member_id: int = 77) -> dict:
    return {
        "input_payload": {
            "session_scope": session_scope,
            "contact_list_member_id": member_id,
            "contact_list_id": CONTACT_LIST_UUID,
            "mailing_id": 1140,
            "channel_type": "rcs",
        },
        "workflow_v2": {
            "flow_id": FLOW_UUID,
            "revision_id": REVISION_UUID,
            "next_card_cursor": RCS_REF,
        },
        "variables": {"payload": {}, "customs": {}},
    }


def _contact_row(*, member_id: int = 77, channel_type: str = "rcs") -> dict:
    return {
        "contact_list_member_id": member_id,
        "contact_list_id": CONTACT_LIST_UUID,
        "mailing_id": "1140",
        "contact_identifier": "12345678901",
        "contact_name": "Contato",
        "contact_full_name": "Contato de Teste",
        "contact_channel_type": channel_type,
        "contact_channel_label": "identidade_tel_1",
        "contact_channel_address": "5511999990001",
        "contact_channel_extra_data": {},
        "person_uuid": PERSON_UUID,
        "is_primary": False,
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
        "next_card_uuid": RCS_REF,
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
                revision={
                    "id": REVISION_UUID,
                    "definition": {"components": [_component()], "branches": []},
                },
                source="pinned",
                requested_revision_id=REVISION_UUID,
                failure_reason=None,
            )
        ),
    )
    monkeypatch.setattr(
        workflow, "fetch_session_workflow_state", AsyncMock(return_value=session_state)
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
async def test_channel_rcs_marks_exact_member_and_blocks_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    persisted = _configure_execution(
        monkeypatch, runtime=runtime, contact_row=_contact_row()
    )
    assign_rcs = AsyncMock(
        return_value={
            "contact_list_member_id": 77,
            "linked_actuator": "rcs",
            "mode": "marked",
        }
    )
    http_execute = Mock(side_effect=AssertionError("RCS marker-only cannot call HTTP"))
    monkeypatch.setattr(workflow, "assign_rcs_routing_for_session", assign_rcs)
    monkeypatch.setattr(workflow, "_http_execute", http_execute)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "blocked_send_with_rcs"
    assert result.executed_steps == 1
    assert result.last_card_uuid == RCS_REF
    assert result.next_card_uuid is None
    assign_rcs.assert_awaited_once()
    http_execute.assert_not_called()
    assert persisted[-1]["last_card_uuid"] == RCS_REF
    assert persisted[-1]["next_card_uuid"] is None
    assert runtime["workflow_v2"]["blocking_stop_reason"] == "blocked_send_with_rcs"
    assert runtime["send_with_rcs_routing"]["assignment"]["linked_actuator"] == "rcs"
    assert "Olá" not in str(runtime)


@pytest.mark.asyncio
async def test_person_rcs_requires_and_uses_explicitly_selected_rcs_member(
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
        "type": "rcs",
        "label": "identidade_tel_2",
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
    assign_rcs = AsyncMock(
        return_value={
            "contact_list_member_id": 88,
            "linked_actuator": "rcs",
            "mode": "marked",
        }
    )
    monkeypatch.setattr(workflow, "assign_rcs_routing_for_session", assign_rcs)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "blocked_send_with_rcs"
    assert assign_rcs.await_args.kwargs["contact_list_member_id"] == 88
    assert assign_rcs.await_args.kwargs["person_uuid"] == PERSON_UUID


@pytest.mark.asyncio
async def test_person_rcs_without_selection_terminalizes_without_marking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(session_scope="person")
    persisted = _configure_execution(
        monkeypatch, runtime=runtime, contact_row=_contact_row()
    )
    assign_rcs = AsyncMock()
    monkeypatch.setattr(workflow, "assign_rcs_routing_for_session", assign_rcs)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "person_scope_channel_component_not_supported"
    assert persisted[-1]["state"] == 3
    assign_rcs.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_type", ["voice", "phone", "sms", "whatsapp", "email"])
async def test_non_rcs_member_is_not_eligible(
    monkeypatch: pytest.MonkeyPatch,
    channel_type: str,
) -> None:
    runtime = _runtime()
    persisted = _configure_execution(
        monkeypatch,
        runtime=runtime,
        contact_row=_contact_row(channel_type=channel_type),
    )
    assign_rcs = AsyncMock()
    monkeypatch.setattr(workflow, "assign_rcs_routing_for_session", assign_rcs)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "send_with_rcs_contact_not_eligible"
    assert persisted[-1]["state"] == 3
    assign_rcs.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_rcs_reentry_does_not_mark_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime["workflow_v2"].update(
        {
            "blocking_execution": True,
            "blocking_stop_reason": "blocked_send_with_rcs",
            "last_card_cursor": RCS_REF,
            "next_card_cursor": None,
        }
    )
    _configure_execution(
        monkeypatch,
        runtime=runtime,
        contact_row=_contact_row(),
        session_state_overrides={"last_card_uuid": RCS_REF, "next_card_uuid": None},
    )
    assign_rcs = AsyncMock()
    monkeypatch.setattr(workflow, "assign_rcs_routing_for_session", assign_rcs)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "blocked_send_with_rcs"
    assert result.executed_steps == 0
    assign_rcs.assert_not_awaited()


def test_rcs_block_dispatcher_and_terminal_alarm_are_registered() -> None:
    assert workflow._blocking_stop_reason_for_component("send_with_rcs") == (
        "blocked_send_with_rcs"
    )
    assert "blocked_send_with_rcs" in workflow_dispatcher_service.BLOCKING_RUNNING_STOP_REASONS
    assert "send_with_rcs_contact_not_eligible" in (
        workflow_dispatcher_service.TERMINAL_FAILURE_STOP_REASONS
    )
    assert m2_alarm_from_stopped_reason("send_with_rcs_contact_not_eligible") == (
        "error",
        "workflow_m2_send_with_rcs_contact_not_eligible",
        "Sessão encerrada porque o canal RCS selecionado não permaneceu elegível para o handoff.",
    )
