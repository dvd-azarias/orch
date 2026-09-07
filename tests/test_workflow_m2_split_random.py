from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

import app.services.workflow_m2_service as workflow
from app.services.workflow_revision_service import WorkflowRevisionResolution


FLOW_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REVISION_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SESSION_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
SPLIT_REF = "11111111-1111-1111-1111-111111111111"
VARIANT_A_REF = "22222222-2222-2222-2222-222222222222"
VARIANT_B_REF = "33333333-3333-3333-3333-333333333333"
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

    async def execute(self, *_args, **_kwargs) -> _Result:
        return _Result()


def _component(**parameters: object) -> dict:
    defaults = {
        "variant_a_percentage": 50,
        "variant_b_percentage": 50,
        "output_var": "split_random",
    }
    defaults.update(parameters)
    return {
        "ref_id": SPLIT_REF,
        "component_id": "split_random",
        "parameters": defaults,
    }


def _definition(
    *,
    component: dict | None = None,
    include_variant_a: bool = True,
    include_variant_b: bool = True,
    include_exception: bool = True,
) -> dict:
    branches: list[dict] = []
    if include_variant_a:
        branches.append({"from": SPLIT_REF, "to": VARIANT_A_REF, "branch": "variant_a"})
    if include_variant_b:
        branches.append({"from": SPLIT_REF, "to": VARIANT_B_REF, "branch": "variant_b"})
    if include_exception:
        branches.append({"from": SPLIT_REF, "to": EXCEPTION_REF, "branch": "exception"})
    return {
        "components": [
            component or _component(),
            {"ref_id": VARIANT_A_REF, "component_id": "finish_flow", "parameters": {}},
            {"ref_id": VARIANT_B_REF, "component_id": "finish_flow", "parameters": {}},
            {"ref_id": EXCEPTION_REF, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": branches,
    }


def _runtime() -> dict:
    return {
        "workflow_v2": {
            "flow_id": FLOW_UUID,
            "revision_id": REVISION_UUID,
            "next_card_cursor": SPLIT_REF,
        },
        "variables": {"payload": {}, "customs": {}},
    }


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
                "next_card_uuid": SPLIT_REF,
                "frozen_until": None,
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
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())
    return persisted


def test_split_random_accepts_serialized_catalog_parameters() -> None:
    component = _component()
    component["parameters"] = [
        {"id": "variant_a_percentage", "value": {"value": "35"}},
        {"id": "variant_b_percentage", "value": {"id": "65"}},
        {"id": "output_var", "value": "experiment_group"},
    ]

    assert workflow._split_random_config(component) == (35, 65, "experiment_group")


def test_split_random_uses_default_output_variable_when_optional_field_is_blank() -> None:
    assert workflow._split_random_config(_component(output_var="   ")) == (
        50,
        50,
        "split_random",
    )


@pytest.mark.parametrize("invalid", [None, "", True, -1, 101, 1.5, "cinquenta", float("nan"), float("inf")])
def test_split_random_rejects_invalid_percentage(invalid: object) -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._split_random_config(_component(variant_a_percentage=invalid))

    assert exc_info.value.code == "split_random_invalid_percentage"


def test_split_random_rejects_percentage_total_other_than_one_hundred() -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._split_random_config(
            _component(variant_a_percentage=60, variant_b_percentage=50)
        )

    assert exc_info.value.code == "split_random_invalid_total"


@pytest.mark.parametrize("output_var", ["split-random", "123split", "{{custom.output}}", "a" * 129])
def test_split_random_rejects_invalid_output_variable(output_var: str) -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._split_random_config(_component(output_var=output_var))

    assert exc_info.value.code == "split_random_invalid_output_var"


def test_split_random_is_stable_for_same_session_component_and_revision() -> None:
    definition = _definition()
    first_runtime = _runtime()
    second_runtime = _runtime()
    timestamp = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    first_branch = workflow._run_split_random(
        component=_component(),
        definition=definition,
        current_card_uuid=SPLIT_REF,
        runtime_variables=first_runtime,
        flow_uuid=FLOW_UUID,
        session_identity=SESSION_UUID,
        revision_id=REVISION_UUID,
        now=timestamp,
    )
    second_branch = workflow._run_split_random(
        component=_component(),
        definition=definition,
        current_card_uuid=SPLIT_REF,
        runtime_variables=second_runtime,
        flow_uuid=FLOW_UUID,
        session_identity=SESSION_UUID,
        revision_id=REVISION_UUID,
        now=timestamp,
    )

    assert second_branch == first_branch
    assert second_runtime["split_random_last_result"] == first_runtime["split_random_last_result"]
    assert first_runtime["variables"]["customs"]["split_random"] == first_branch
    assert first_runtime["split_random_last_result"]["strategy"] == "sha256_mod_100_v1"


@pytest.mark.parametrize(
    ("variant_a_percentage", "variant_b_percentage", "expected_branch"),
    [(0, 100, "variant_b"), (100, 0, "variant_a")],
)
def test_split_random_honors_zero_and_one_hundred_boundaries(
    variant_a_percentage: int,
    variant_b_percentage: int,
    expected_branch: str,
) -> None:
    runtime = _runtime()
    branch = workflow._run_split_random(
        component=_component(
            variant_a_percentage=variant_a_percentage,
            variant_b_percentage=variant_b_percentage,
        ),
        definition=_definition(),
        current_card_uuid=SPLIT_REF,
        runtime_variables=runtime,
        flow_uuid=FLOW_UUID,
        session_identity=SESSION_UUID,
        revision_id=REVISION_UUID,
    )

    assert branch == expected_branch
    assert runtime["variables"]["customs"]["split_random"] == expected_branch


@pytest.mark.parametrize(
    ("bucket", "expected_branch"),
    [(34, "variant_a"), (35, "variant_b")],
)
def test_split_random_uses_variant_a_percentage_as_bucket_boundary(
    monkeypatch: pytest.MonkeyPatch,
    bucket: int,
    expected_branch: str,
) -> None:
    monkeypatch.setattr(workflow, "_split_random_bucket", lambda **_kwargs: bucket)
    runtime = _runtime()

    branch = workflow._run_split_random(
        component=_component(variant_a_percentage=35, variant_b_percentage=65),
        definition=_definition(),
        current_card_uuid=SPLIT_REF,
        runtime_variables=runtime,
        flow_uuid=FLOW_UUID,
        session_identity=SESSION_UUID,
        revision_id=REVISION_UUID,
    )

    assert branch == expected_branch


def test_split_random_rejects_missing_or_ambiguous_primary_branch() -> None:
    missing = _definition(include_variant_b=False)
    duplicated = _definition()
    duplicated["branches"].append(
        {"from": SPLIT_REF, "to": EXCEPTION_REF, "branch": "variant_a"}
    )

    for definition in (missing, duplicated):
        with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
            workflow._validate_split_random_branches(
                definition=definition,
                current_card_uuid=SPLIT_REF,
            )
        assert exc_info.value.code == "split_random_invalid_branches"


@pytest.mark.asyncio
async def test_execute_workflow_routes_split_random_to_selected_variant(monkeypatch) -> None:
    definition = _definition()
    runtime = _runtime()
    persisted = _configure_execution(monkeypatch, definition=definition, runtime=runtime)
    bucket = workflow._split_random_bucket(
        flow_uuid=FLOW_UUID,
        session_identity=SESSION_UUID,
        revision_id=REVISION_UUID,
        current_card_uuid=SPLIT_REF,
    )
    expected_branch = "variant_a" if bucket < 50 else "variant_b"
    expected_finish = VARIANT_A_REF if expected_branch == "variant_a" else VARIANT_B_REF

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.last_card_uuid == expected_finish
    assert runtime["variables"]["customs"]["split_random"] == expected_branch
    assert runtime["split_random_last_result"]["bucket"] == bucket
    assert any(item.get("next_card_uuid") == expected_finish for item in persisted)


@pytest.mark.asyncio
async def test_invalid_split_random_configuration_uses_exception_branch(monkeypatch) -> None:
    definition = _definition(
        component=_component(variant_a_percentage=60, variant_b_percentage=50)
    )
    runtime = _runtime()
    _configure_execution(monkeypatch, definition=definition, runtime=runtime)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.last_card_uuid == EXCEPTION_REF
    assert runtime["split_random_last_error"]["code"] == "split_random_invalid_total"
    assert "split_random_last_result" not in runtime


@pytest.mark.asyncio
async def test_missing_primary_branch_uses_exception_instead_of_first_outgoing_edge(monkeypatch) -> None:
    definition = _definition(include_variant_b=False)
    runtime = _runtime()
    _configure_execution(monkeypatch, definition=definition, runtime=runtime)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.last_card_uuid == EXCEPTION_REF
    assert runtime["split_random_last_error"]["code"] == "split_random_invalid_branches"


@pytest.mark.asyncio
async def test_invalid_split_random_without_exception_terminates_once(monkeypatch) -> None:
    definition = _definition(include_variant_b=False, include_exception=False)
    runtime = _runtime()
    persisted = _configure_execution(monkeypatch, definition=definition, runtime=runtime)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "split_random_invalid_branches"
    assert result.last_card_uuid == SPLIT_REF
    assert result.next_card_uuid is None
    assert runtime["split_random_last_error"]["code"] == "split_random_invalid_branches"
    assert runtime["workflow_v2"]["terminal_failure"]["code"] == "split_random_invalid_branches"
    assert persisted[-1]["state"] == 3
    assert persisted[-1]["next_card_uuid"] is None
