from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.services.orch_trigger_service as orch_trigger_service
import app.services.workflow_dispatcher_service as workflow_dispatcher_service
import app.services.workflow_m2_service as workflow
from app.services.restriction_list_check_service import (
    RestrictionListCheckError,
    RestrictionListCheckResult,
)
from app.services.workflow_revision_service import WorkflowRevisionResolution


FLOW_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REVISION_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SESSION_UUID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PERSON_UUID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
LIST_A = "11111111-1111-4111-8111-111111111111"
LIST_B = "22222222-2222-4222-8222-222222222222"
CHECK_REF = "33333333-3333-4333-8333-333333333333"
RESTRICTED_REF = "44444444-4444-4444-8444-444444444444"
ALLOWED_REF = "55555555-5555-4555-8555-555555555555"


class _Transaction:
    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        return False


class _Result:
    def scalar_one(self) -> bool:
        return True


class _Session:
    def in_transaction(self) -> bool:
        return False

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, *_args, **_kwargs) -> _Result:  # type: ignore[no-untyped-def]
        return _Result()


def _component(**parameters: object) -> dict:
    defaults = {
        "restriction_list_ids": [LIST_A],
        "evaluation_scope": "current_channel",
        "output_var": "restriction_check",
    }
    defaults.update(parameters)
    return {
        "ref_id": CHECK_REF,
        "component_id": "check_restriction_lists",
        "parameters": defaults,
    }


def _definition(component: dict | None = None) -> dict:
    return {
        "components": [
            component or _component(),
            {
                "ref_id": RESTRICTED_REF,
                "component_id": "finish_flow",
                "parameters": {"result": "unsuccess"},
            },
            {
                "ref_id": ALLOWED_REF,
                "component_id": "finish_flow",
                "parameters": {"result": "success"},
            },
        ],
        "branches": [
            {"from": CHECK_REF, "to": RESTRICTED_REF, "branch": "restricted"},
            {"from": CHECK_REF, "to": ALLOWED_REF, "branch": "allowed"},
        ],
    }


def _runtime() -> dict:
    return {
        "workflow_v2": {
            "flow_id": FLOW_UUID,
            "revision_id": REVISION_UUID,
            "next_card_cursor": CHECK_REF,
        },
        "variables": {"payload": {}, "customs": {}},
    }


def _contact() -> dict:
    return {
        "contact_list_member_id": 123,
        "contact_list_id": "66666666-6666-4666-8666-666666666666",
        "mailing_id": 456,
        "person_uuid": PERSON_UUID,
        "contact_identifier": "identifier",
        "contact_channel_type": "voice",
        "contact_channel_address": "5511975620806",
        "contact_channel_label": "principal",
    }


def _result(decision: str) -> RestrictionListCheckResult:
    restricted = decision == "restricted"
    return RestrictionListCheckResult(
        decision=decision,
        restricted=restricted,
        matches=(
            [
                {
                    "restriction_list_id": LIST_A,
                    "restriction_list_name": "Lista A",
                    "field_code": "phone",
                    "value_masked": "*********0806",
                }
            ]
            if restricted
            else []
        ),
        evaluated_list_ids=[LIST_A],
        evaluated_fields=["phone"],
        evaluated_at="2026-09-13T01:13:24Z",
        attempts=1,
    )


def _configure_execution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    definition: dict,
    runtime: dict,
) -> list[dict]:
    persisted: list[dict] = []
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
                "last_card_uuid": None,
                "next_card_uuid": CHECK_REF,
                "frozen_until": None,
                "entity_address": "5511975620806",
            }
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_contact_runtime_context_for_session",
        AsyncMock(return_value=_contact()),
    )
    monkeypatch.setattr(
        workflow,
        "get_current_workspace_uuid",
        lambda: "77777777-7777-4777-8777-777777777777",
    )

    async def _replace(*_args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        persisted.append(kwargs)

    monkeypatch.setattr(workflow, "replace_session_workflow_state", _replace)
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())
    return persisted


def test_check_restriction_lists_accepts_catalog_multiselect_shapes() -> None:
    component = _component(
        restriction_list_ids={
            "in_use": [
                {"id": LIST_A, "name": "Lista A"},
                {"id": LIST_B, "name": "Lista B"},
            ],
            "available": [],
        },
        evaluation_scope={"id": "person", "name": "Pessoa completa"},
    )
    component["parameters"] = [
        {"id": key, "value": value}
        for key, value in component["parameters"].items()
    ]

    assert workflow._check_restriction_lists_config(component) == (
        [LIST_A, LIST_B],
        "person",
        "restriction_check",
    )


@pytest.mark.parametrize(
    ("parameters", "error_code"),
    [
        ({"restriction_list_ids": []}, "check_restriction_lists_invalid_list_ids"),
        (
            {"restriction_list_ids": [LIST_A, LIST_A]},
            "check_restriction_lists_invalid_list_ids",
        ),
        (
            {"evaluation_scope": "channel"},
            "check_restriction_lists_invalid_evaluation_scope",
        ),
        (
            {"output_var": "restriction-check"},
            "check_restriction_lists_invalid_output_var",
        ),
    ],
)
def test_check_restriction_lists_rejects_invalid_runtime_configuration(
    parameters: dict,
    error_code: str,
) -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._check_restriction_lists_config(_component(**parameters))

    assert exc_info.value.code == error_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_finish"),
    [("restricted", RESTRICTED_REF), ("allowed", ALLOWED_REF)],
)
async def test_execute_workflow_routes_supplier_decision(
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
    expected_finish: str,
) -> None:
    runtime = _runtime()
    persisted = _configure_execution(
        monkeypatch,
        definition=_definition(),
        runtime=runtime,
    )
    captured: dict = {}

    def _evaluate(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return _result(decision)

    monkeypatch.setattr(workflow, "evaluate_restriction_lists", _evaluate)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.last_card_uuid == expected_finish
    assert captured == {
        "workspace_uuid": "77777777-7777-4777-8777-777777777777",
        "restriction_list_ids": [LIST_A],
        "evaluation_scope": "current_channel",
        "person_uuid": None,
        "channel_type": "voice",
        "channel_address": "5511975620806",
    }
    stored = runtime["variables"]["customs"]["restriction_check"]
    assert stored["decision"] == decision
    assert stored["match_count"] == (1 if decision == "restricted" else 0)
    assert "contact_channel_address" not in stored
    assert any(item.get("next_card_uuid") == expected_finish for item in persisted)


@pytest.mark.asyncio
async def test_execute_workflow_terminalizes_when_supplier_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    persisted = _configure_execution(
        monkeypatch,
        definition=_definition(),
        runtime=runtime,
    )

    def _evaluate(**_kwargs):  # type: ignore[no-untyped-def]
        raise RestrictionListCheckError(
            "check_restriction_lists_target_core_unavailable",
            "Supplier indisponível.",
        )

    monkeypatch.setattr(workflow, "evaluate_restriction_lists", _evaluate)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "check_restriction_lists_target_core_unavailable"
    assert result.last_card_uuid == CHECK_REF
    assert result.next_card_uuid is None
    assert runtime["check_restriction_lists_last_error"]["code"] == (
        "check_restriction_lists_target_core_unavailable"
    )
    assert runtime["workflow_v2"]["terminal_failure"]["code"] == (
        "check_restriction_lists_target_core_unavailable"
    )
    assert persisted[-1]["state"] == 3
    assert persisted[-1]["next_card_uuid"] is None


def test_restriction_list_failures_have_alarm_and_dispatcher_terminal_contract() -> None:
    code = "check_restriction_lists_target_core_unavailable"

    assert code in workflow.RESTRICTION_LIST_CHECK_ERROR_CODES
    assert code in workflow_dispatcher_service.TERMINAL_FAILURE_STOP_REASONS
    assert orch_trigger_service.m2_alarm_from_stopped_reason(code) == (
        "error",
        f"workflow_m2_{code}",
        (
            "Sessão encerrada sem liberação porque a consulta às Listas de "
            "Restrição não pôde ser confirmada."
        ),
    )
