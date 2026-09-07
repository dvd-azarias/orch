from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

import app.services.workflow_m2_service as workflow
from app.services.orch_trigger_service import m2_alarm_from_stopped_reason
from app.services.workflow_revision_service import WorkflowRevisionResolution


FLOW_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REVISION_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SESSION_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
CONTACT_LIST_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
PERSON_UUID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
SELECT_REF = "11111111-1111-1111-1111-111111111111"
SELECTED_REF = "22222222-2222-2222-2222-222222222222"
NOT_FOUND_REF = "33333333-3333-3333-3333-333333333333"
EXCEPTION_REF = "44444444-4444-4444-4444-444444444444"


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
        "channel_type": "voice",
        "channel_label": None,
        "output_var": "selected_channel",
    }
    defaults.update(parameters)
    return {
        "ref_id": SELECT_REF,
        "component_id": "select_contact_channel",
        "parameters": defaults,
    }


def _contact_row(
    *,
    member_id: int = 77,
    channel_type: str = "voice",
    channel_label: str = "telefone_1",
    address: str = "5511999990001",
    is_primary: bool = True,
) -> dict:
    return {
        "contact_list_member_id": member_id,
        "contact_list_id": CONTACT_LIST_UUID,
        "mailing_id": "1140",
        "contact_identifier": "12345678901",
        "contact_name": "Contato",
        "contact_full_name": "Contato de Teste",
        "contact_channel_type": channel_type,
        "contact_channel_label": channel_label,
        "contact_channel_address": address,
        "contact_channel_extra_data": {},
        "person_uuid": PERSON_UUID,
        "is_primary": is_primary,
    }


def _runtime(*, session_scope: str = "channel") -> dict:
    return {
        "input_payload": {
            "session_scope": session_scope,
            "contact_list_member_id": 77,
            "contact_list_id": CONTACT_LIST_UUID,
            "mailing_id": 1140,
            "channel_type": "voice",
        },
        "workflow_v2": {
            "flow_id": FLOW_UUID,
            "revision_id": REVISION_UUID,
            "next_card_cursor": SELECT_REF,
        },
        "variables": {"payload": {}, "customs": {}},
    }


def _definition(*, component: dict | None = None) -> dict:
    return {
        "components": [
            component or _component(),
            {"ref_id": SELECTED_REF, "component_id": "finish_flow", "parameters": {}},
            {"ref_id": NOT_FOUND_REF, "component_id": "finish_flow", "parameters": {}},
            {"ref_id": EXCEPTION_REF, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [
            {"from": SELECT_REF, "to": SELECTED_REF, "branch": "selected"},
            {"from": SELECT_REF, "to": NOT_FOUND_REF, "branch": "not_found"},
            {"from": SELECT_REF, "to": EXCEPTION_REF, "branch": "exception"},
        ],
    }


def _configure_execution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime: dict,
    definition: dict,
    contact_row: dict,
) -> list[dict]:
    persisted: list[dict] = []
    monkeypatch.setattr(workflow, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(
        workflow, "fetch_flow_row", AsyncMock(return_value={"id": FLOW_UUID})
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
                "state": 0,
                "entity_address": "5511999990001",
                "runtime_variables": runtime,
                "last_card_uuid": None,
                "next_card_uuid": SELECT_REF,
                "frozen_until": None,
            }
        ),
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


def test_select_contact_channel_accepts_catalog_serialization() -> None:
    component = _component()
    component["parameters"] = [
        {"id": "channel_type", "value": {"id": "whatsapp", "name": "WhatsApp"}},
        {"id": "channel_label", "value": {"label": "celular", "name": "Celular"}},
        {"id": "output_var", "value": "canal_escolhido"},
    ]

    assert workflow._select_contact_channel_config(component) == (
        "whatsapp",
        "celular",
        "canal_escolhido",
    )


@pytest.mark.parametrize(
    "channel_type", [None, "", "phone", "telegram", "{{custom.type}}"]
)
def test_select_contact_channel_rejects_invalid_type(channel_type: object) -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._select_contact_channel_config(_component(channel_type=channel_type))

    assert exc_info.value.code == "select_contact_channel_invalid_channel_type"


@pytest.mark.parametrize(
    "channel_label",
    ["{{contact.channel_label}}", "a" * 129, "label\nforjada", ["tel1", "tel2"]],
)
def test_select_contact_channel_rejects_invalid_label(channel_label: object) -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._select_contact_channel_config(_component(channel_label=channel_label))

    assert exc_info.value.code == "select_contact_channel_invalid_channel_label"


@pytest.mark.parametrize(
    "output_var", ["selected-channel", "123channel", "{{custom.output}}", "a" * 129]
)
def test_select_contact_channel_rejects_invalid_output_var(output_var: str) -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._select_contact_channel_config(_component(output_var=output_var))

    assert exc_info.value.code == "select_contact_channel_invalid_output_var"


@pytest.mark.asyncio
async def test_channel_scope_only_selects_current_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _contact_row()
    fetch_candidate = AsyncMock(return_value=candidate)
    rebind = AsyncMock(return_value=True)
    monkeypatch.setattr(
        workflow, "fetch_select_contact_channel_candidate", fetch_candidate
    )
    monkeypatch.setattr(workflow, "rebind_person_session_to_contact_channel", rebind)
    runtime = _runtime(session_scope="channel")

    execution = await workflow._run_select_contact_channel(
        db_session=_Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
        session_scope="channel",
        component=_component(channel_label="telefone_1"),
        runtime_variables=runtime,
        contact_row=_contact_row(),
        now=datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
    )

    assert execution.branch_label == "selected"
    assert execution.contact_row == candidate
    assert fetch_candidate.await_args.kwargs["session_scope"] == "channel"
    assert fetch_candidate.await_args.kwargs["contact_list_member_id"] == 77
    rebind.assert_not_awaited()
    output = runtime["variables"]["customs"]["selected_channel"]
    assert output["contact_list_member_id"] == 77
    assert output["address"] == "5511999990001"


@pytest.mark.asyncio
async def test_person_scope_selects_another_member_and_rebinds_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _contact_row(
        member_id=88,
        channel_type="whatsapp",
        channel_label="celular",
        address="5511988880002",
    )
    fetch_candidate = AsyncMock(return_value=candidate)
    rebind = AsyncMock(return_value=True)
    monkeypatch.setattr(
        workflow, "fetch_select_contact_channel_candidate", fetch_candidate
    )
    monkeypatch.setattr(workflow, "rebind_person_session_to_contact_channel", rebind)
    runtime = _runtime(session_scope="person")

    execution = await workflow._run_select_contact_channel(
        db_session=_Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
        session_scope="person",
        component=_component(channel_type="whatsapp", channel_label="celular"),
        runtime_variables=runtime,
        contact_row=_contact_row(),
    )

    assert execution.branch_label == "selected"
    assert fetch_candidate.await_args.kwargs["session_scope"] == "person"
    assert rebind.await_args.kwargs["contact_list_member_id"] == 88
    selection = workflow._active_selected_contact_channel(runtime)
    assert selection is not None
    assert selection["contact_list_member_id"] == 88
    assert selection["type"] == "whatsapp"
    assert selection["address"] == "5511988880002"


@pytest.mark.asyncio
async def test_not_found_sets_null_output_without_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_select_contact_channel_candidate",
        AsyncMock(return_value=None),
    )
    rebind = AsyncMock(return_value=True)
    monkeypatch.setattr(workflow, "rebind_person_session_to_contact_channel", rebind)
    runtime = _runtime(session_scope="person")
    runtime["workflow_v2"]["selected_contact_channel"] = {
        "contact_list_member_id": 66,
        "contact_list_id": CONTACT_LIST_UUID,
        "mailing_id": 1140,
        "type": "voice",
        "address": "5511977770003",
    }

    execution = await workflow._run_select_contact_channel(
        db_session=_Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
        session_scope="person",
        component=_component(channel_type="email"),
        runtime_variables=runtime,
        contact_row=_contact_row(),
    )

    assert execution.branch_label == "not_found"
    assert execution.contact_row is None
    assert runtime["variables"]["customs"]["selected_channel"] is None
    assert runtime["select_contact_channel_last_result"]["requested_type"] == "email"
    assert "selected_contact_channel" not in runtime["workflow_v2"]
    rebind.assert_not_awaited()


@pytest.mark.asyncio
async def test_person_scope_requires_person_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_candidate = AsyncMock()
    monkeypatch.setattr(
        workflow,
        "fetch_select_contact_channel_candidate",
        fetch_candidate,
    )
    contact_row = _contact_row()
    contact_row["person_uuid"] = None

    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        await workflow._run_select_contact_channel(
            db_session=_Session(),  # type: ignore[arg-type]
            flow_uuid=FLOW_UUID,
            session_id=123,
            session_scope="person",
            component=_component(),
            runtime_variables=_runtime(session_scope="person"),
            contact_row=contact_row,
        )

    assert exc_info.value.code == "select_contact_channel_missing_contact_context"
    fetch_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_person_scope_rebind_failure_is_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_select_contact_channel_candidate",
        AsyncMock(return_value=_contact_row(member_id=88)),
    )
    monkeypatch.setattr(
        workflow,
        "rebind_person_session_to_contact_channel",
        AsyncMock(return_value=False),
    )

    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        await workflow._run_select_contact_channel(
            db_session=_Session(),  # type: ignore[arg-type]
            flow_uuid=FLOW_UUID,
            session_id=123,
            session_scope="person",
            component=_component(),
            runtime_variables=_runtime(session_scope="person"),
            contact_row=_contact_row(),
        )

    assert exc_info.value.code == "select_contact_channel_rebind_failed"


def test_person_scope_communication_requires_active_selection() -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._ensure_person_scope_component_supported(
            session_scope="person",
            component_kind_value="send_with_dialer",
        )

    assert exc_info.value.code == "person_scope_channel_component_not_supported"
    workflow._ensure_person_scope_component_supported(
        session_scope="person",
        component_kind_value="send_with_dialer",
        selected_contact_channel={"contact_list_member_id": 88},
    )


@pytest.mark.parametrize(
    "selection_patch",
    [
        {"selected": False},
        {"session_scope": "channel"},
        {"person_uuid": None},
    ],
)
def test_persisted_selection_must_be_complete_person_selection(
    selection_patch: dict[str, object],
) -> None:
    runtime = _runtime(session_scope="person")
    selection = {
        "selected": True,
        "session_scope": "person",
        "contact_list_member_id": 88,
        "contact_list_id": CONTACT_LIST_UUID,
        "mailing_id": 1140,
        "person_uuid": PERSON_UUID,
        "type": "voice",
        "address": "5511988880002",
    }
    selection.update(selection_patch)
    runtime["workflow_v2"]["selected_contact_channel"] = selection

    assert workflow._active_selected_contact_channel(runtime) is None


def test_select_contact_channel_terminal_failure_has_inline_alarm() -> None:
    alarm = m2_alarm_from_stopped_reason("select_contact_channel_rebind_failed")

    assert alarm == (
        "error",
        "workflow_m2_select_contact_channel_rebind_failed",
        "Sessão encerrada porque a seleção de canal falhou de forma determinística.",
    )


@pytest.mark.asyncio
async def test_executor_dispatches_person_selection_and_follows_selected_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(session_scope="person")
    persisted = _configure_execution(
        monkeypatch,
        runtime=runtime,
        definition=_definition(),
        contact_row=_contact_row(),
    )
    selected = _contact_row(
        member_id=88,
        channel_type="voice",
        channel_label="telefone_2",
        address="5511988880002",
        is_primary=False,
    )
    monkeypatch.setattr(
        workflow,
        "fetch_select_contact_channel_candidate",
        AsyncMock(return_value=selected),
    )
    rebind = AsyncMock(return_value=True)
    monkeypatch.setattr(workflow, "rebind_person_session_to_contact_channel", rebind)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.last_card_uuid == SELECTED_REF
    assert runtime["variables"]["contact"]["contact_list_member_id"] == 88
    assert runtime["variables"]["contact"]["channel_address"] == "5511988880002"
    assert (
        runtime["workflow_v2"]["selected_contact_channel"]["contact_list_member_id"]
        == 88
    )
    assert any(item.get("next_card_uuid") == SELECTED_REF for item in persisted)
    rebind.assert_awaited_once()


@pytest.mark.asyncio
async def test_person_selection_unlocks_dialer_with_selected_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(session_scope="person")
    definition = _definition()
    definition["components"][1] = {
        "ref_id": SELECTED_REF,
        "component_id": "send_with_dialer",
        "parameters": {},
    }
    _configure_execution(
        monkeypatch,
        runtime=runtime,
        definition=definition,
        contact_row=_contact_row(),
    )
    selected = _contact_row(
        member_id=88,
        channel_type="voice",
        channel_label="telefone_2",
        address="5511988880002",
        is_primary=False,
    )
    monkeypatch.setattr(
        workflow,
        "fetch_select_contact_channel_candidate",
        AsyncMock(return_value=selected),
    )
    monkeypatch.setattr(
        workflow,
        "rebind_person_session_to_contact_channel",
        AsyncMock(return_value=True),
    )
    prepare_dialer = AsyncMock(
        return_value={
            "contact_list_member_id": 88,
            "ani": "1147371485",
            "linked_actuator": "dialer",
        }
    )
    monkeypatch.setattr(
        workflow,
        "_prepare_send_with_dialer_contact_member",
        prepare_dialer,
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "blocked_send_with_dialer"
    assert result.last_card_uuid == SELECTED_REF
    assert prepare_dialer.await_args.kwargs["contact_list_member_id"] == 88


@pytest.mark.asyncio
async def test_not_found_revokes_previous_selection_before_communication(
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
        "type": "voice",
        "address": "5511988880002",
    }
    definition = _definition()
    definition["components"][2] = {
        "ref_id": NOT_FOUND_REF,
        "component_id": "send_with_dialer",
        "parameters": {},
    }
    persisted = _configure_execution(
        monkeypatch,
        runtime=runtime,
        definition=definition,
        contact_row=_contact_row(member_id=88, address="5511988880002"),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_select_contact_channel_candidate",
        AsyncMock(return_value=None),
    )
    prepare_dialer = AsyncMock()
    monkeypatch.setattr(
        workflow,
        "_prepare_send_with_dialer_contact_member",
        prepare_dialer,
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "person_scope_channel_component_not_supported"
    assert result.last_card_uuid == NOT_FOUND_REF
    assert "selected_contact_channel" not in runtime["workflow_v2"]
    assert persisted[-1]["state"] == 3
    prepare_dialer.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_configuration_follows_exception_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(session_scope="channel")
    runtime["workflow_v2"]["selected_contact_channel"] = {
        "contact_list_member_id": 66,
        "contact_list_id": CONTACT_LIST_UUID,
        "mailing_id": 1140,
        "type": "voice",
        "address": "5511977770003",
    }
    _configure_execution(
        monkeypatch,
        runtime=runtime,
        definition=_definition(component=_component(channel_type="telegram")),
        contact_row=_contact_row(),
    )
    fetch_candidate = AsyncMock()
    monkeypatch.setattr(
        workflow, "fetch_select_contact_channel_candidate", fetch_candidate
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.last_card_uuid == EXCEPTION_REF
    assert runtime["select_contact_channel_last_error"]["code"] == (
        "select_contact_channel_invalid_channel_type"
    )
    assert "selected_contact_channel" not in runtime["workflow_v2"]
    fetch_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_configuration_without_exception_terminalizes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(session_scope="channel")
    definition = _definition(component=_component(channel_type="telegram"))
    definition["branches"] = [
        branch for branch in definition["branches"] if branch["branch"] != "exception"
    ]
    persisted = _configure_execution(
        monkeypatch,
        runtime=runtime,
        definition=definition,
        contact_row=_contact_row(),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_select_contact_channel_candidate",
        AsyncMock(),
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "select_contact_channel_invalid_channel_type"
    assert result.last_card_uuid == SELECT_REF
    assert result.next_card_uuid is None
    assert runtime["workflow_v2"]["terminal_failure"]["code"] == (
        "select_contact_channel_invalid_channel_type"
    )
    assert persisted[-1]["state"] == 3
    assert persisted[-1]["next_card_uuid"] is None


@pytest.mark.asyncio
async def test_person_resume_hydrates_previously_selected_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(session_scope="person")
    runtime["workflow_v2"]["next_card_cursor"] = SELECTED_REF
    runtime["workflow_v2"]["selected_contact_channel"] = {
        "selected": True,
        "session_scope": "person",
        "contact_list_member_id": 88,
        "contact_list_id": CONTACT_LIST_UUID,
        "mailing_id": 1140,
        "person_uuid": PERSON_UUID,
        "type": "whatsapp",
        "label": "celular",
        "address": "5511988880002",
    }
    definition = {
        "components": [
            {"ref_id": SELECTED_REF, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [],
    }
    selected = _contact_row(
        member_id=88,
        channel_type="whatsapp",
        channel_label="celular",
        address="5511988880002",
    )
    fetch_contact = AsyncMock(return_value=selected)
    monkeypatch.setattr(workflow, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(
        workflow, "fetch_flow_row", AsyncMock(return_value={"id": FLOW_UUID})
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
                "state": 0,
                "entity_address": "5511988880002",
                "runtime_variables": runtime,
                "last_card_uuid": SELECT_REF,
                "next_card_uuid": SELECTED_REF,
                "frozen_until": None,
            }
        ),
    )
    monkeypatch.setattr(
        workflow, "fetch_contact_runtime_context_for_session", fetch_contact
    )
    monkeypatch.setattr(workflow, "replace_session_workflow_state", AsyncMock())
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert fetch_contact.await_args.kwargs["contact_list_member_id"] == 88
    assert fetch_contact.await_args.kwargs["contact_list_id"] == CONTACT_LIST_UUID
    assert fetch_contact.await_args.kwargs["mailing_id"] == 1140
    assert runtime["variables"]["contact"]["channel_type"] == "whatsapp"
