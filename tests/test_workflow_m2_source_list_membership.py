from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import ANY, AsyncMock

import pytest

import app.services.workflow_m2_service as workflow
from app.services.workflow_revision_service import WorkflowRevisionResolution


PERSON_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
FLOW_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
MAILING_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class _NestedSession:
    @asynccontextmanager
    async def begin_nested(self):
        yield


def _component(**parameters: object) -> dict:
    defaults = {
        "person_uuid": "{{contact.person_uuid}}",
        "mailing_id": MAILING_UUID,
        "output_var": "source_list_membership",
    }
    defaults.update(parameters)
    return {
        "ref_id": "source-list-membership-1",
        "component_id": "source_list_membership",
        "parameters": defaults,
    }


def _runtime() -> dict:
    return {
        "variables": {
            "payload": {},
            "customs": {},
            "contact": {
                "identifier": "12345678901",
                "person_uuid": PERSON_UUID,
            },
        }
    }


def _person() -> dict:
    return {
        "id": 1,
        "uuid": PERSON_UUID,
        "identifier": "12345678901",
        "full_name": "Pessoa Teste",
        "company": None,
        "gender": None,
        "role": None,
        "country": "Brasil",
        "state": "RJ",
        "city": "Rio de Janeiro",
        "birthdate": None,
        "channels": [{"type": "phone", "value": "21999999999"}],
        "extras": {},
    }


@pytest.mark.asyncio
async def test_membership_links_person_from_prior_card_output(monkeypatch) -> None:
    fetch_person = AsyncMock(return_value=_person())
    resolve_list = AsyncMock(
        return_value={"id": 1139, "public_id": MAILING_UUID, "status": "PROCESSED"}
    )
    ensure_membership = AsyncMock(
        return_value={
            "source_list_id": 1139,
            "contact_draft_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "created": True,
            "channels": 1,
        }
    )
    monkeypatch.setattr(workflow, "fetch_person_by_uuid_for_update", fetch_person)
    monkeypatch.setattr(workflow, "resolve_source_list_by_public_id", resolve_list)
    monkeypatch.setattr(workflow, "ensure_person_in_source_list", ensure_membership)
    runtime = _runtime()
    runtime["variables"]["customs"]["contact_action"] = {"person_uuid": PERSON_UUID}

    branch = await workflow._run_source_list_membership(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(
            person_uuid="{{contact_action.person_uuid}}",
            mailing_id={"mailing_id": MAILING_UUID, "name": "Lista Teste"},
            output_var="membership_result",
        ),
        runtime_variables=runtime,
    )

    assert branch == "linked"
    fetch_person.assert_awaited_once_with(ANY, person_uuid=PERSON_UUID)
    resolve_list.assert_awaited_once_with(ANY, public_id=MAILING_UUID)
    ensure_membership.assert_awaited_once_with(
        ANY,
        source_list_id=1139,
        person=_person(),
    )
    assert runtime["variables"]["customs"]["membership_result"] == {
        "action": "linked",
        "person_uuid": PERSON_UUID,
        "mailing_id": MAILING_UUID,
        "source_list_id": 1139,
        "contact_draft_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "channels": 1,
        "missing": None,
    }
    assert runtime["source_list_membership_last_result"]["result"]["action"] == "linked"


@pytest.mark.asyncio
async def test_membership_is_idempotent_and_accepts_serialized_parameters(monkeypatch) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_person_by_uuid_for_update",
        AsyncMock(return_value=_person()),
    )
    monkeypatch.setattr(
        workflow,
        "resolve_source_list_by_public_id",
        AsyncMock(return_value={"id": 1139, "status": "READY_TO_INGEST"}),
    )
    ensure_membership = AsyncMock(
        return_value={
            "contact_draft_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "created": False,
            "channels": 1,
        }
    )
    monkeypatch.setattr(workflow, "ensure_person_in_source_list", ensure_membership)
    component = _component()
    component["parameters"] = [
        {"id": "person_uuid", "value": "{{contact.person_uuid}}"},
        {"id": "mailing_id", "value": [{"mailing_id": MAILING_UUID, "name": "Lista"}]},
        {"id": "output_var", "value": "source_list_membership"},
    ]
    runtime = _runtime()

    branch = await workflow._run_source_list_membership(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=component,
        runtime_variables=runtime,
    )

    assert branch == "already_linked"
    ensure_membership.assert_awaited_once()
    assert runtime["variables"]["customs"]["source_list_membership"]["action"] == (
        "already_linked"
    )


@pytest.mark.asyncio
async def test_membership_unresolved_person_template_follows_not_found_without_writing(
    monkeypatch,
) -> None:
    fetch_person = AsyncMock()
    resolve_list = AsyncMock()
    ensure_membership = AsyncMock()
    monkeypatch.setattr(workflow, "fetch_person_by_uuid_for_update", fetch_person)
    monkeypatch.setattr(workflow, "resolve_source_list_by_public_id", resolve_list)
    monkeypatch.setattr(workflow, "ensure_person_in_source_list", ensure_membership)
    runtime = _runtime()

    branch = await workflow._run_source_list_membership(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(person_uuid="{{contact_action.person_uuid}}"),
        runtime_variables=runtime,
    )

    assert branch == "not_found"
    fetch_person.assert_not_awaited()
    resolve_list.assert_not_awaited()
    ensure_membership.assert_not_awaited()
    assert runtime["variables"]["customs"]["source_list_membership"]["missing"] == "person"


@pytest.mark.asyncio
async def test_membership_missing_person_follows_not_found(monkeypatch) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_person_by_uuid_for_update",
        AsyncMock(return_value=None),
    )
    resolve_list = AsyncMock()
    ensure_membership = AsyncMock()
    monkeypatch.setattr(workflow, "resolve_source_list_by_public_id", resolve_list)
    monkeypatch.setattr(workflow, "ensure_person_in_source_list", ensure_membership)
    runtime = _runtime()

    branch = await workflow._run_source_list_membership(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(),
        runtime_variables=runtime,
    )

    assert branch == "not_found"
    resolve_list.assert_not_awaited()
    ensure_membership.assert_not_awaited()
    assert runtime["variables"]["customs"]["source_list_membership"]["missing"] == "person"


@pytest.mark.asyncio
async def test_membership_missing_list_follows_not_found(monkeypatch) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_person_by_uuid_for_update",
        AsyncMock(return_value=_person()),
    )
    monkeypatch.setattr(
        workflow,
        "resolve_source_list_by_public_id",
        AsyncMock(return_value=None),
    )
    ensure_membership = AsyncMock()
    monkeypatch.setattr(workflow, "ensure_person_in_source_list", ensure_membership)
    runtime = _runtime()

    branch = await workflow._run_source_list_membership(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(),
        runtime_variables=runtime,
    )

    assert branch == "not_found"
    ensure_membership.assert_not_awaited()
    assert runtime["variables"]["customs"]["source_list_membership"]["missing"] == "mailing"


@pytest.mark.asyncio
async def test_membership_rejects_list_not_ready_without_writing(monkeypatch) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_person_by_uuid_for_update",
        AsyncMock(return_value=_person()),
    )
    monkeypatch.setattr(
        workflow,
        "resolve_source_list_by_public_id",
        AsyncMock(return_value={"id": 1139, "status": "UPLOADED"}),
    )
    ensure_membership = AsyncMock()
    monkeypatch.setattr(workflow, "ensure_person_in_source_list", ensure_membership)

    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        await workflow._run_source_list_membership(
            db_session=_NestedSession(),  # type: ignore[arg-type]
            flow_uuid=FLOW_UUID,
            component=_component(),
            runtime_variables=_runtime(),
        )

    assert exc_info.value.code == "source_list_membership_mailing_not_ready"
    ensure_membership.assert_not_awaited()


@pytest.mark.asyncio
async def test_membership_rejects_person_without_identifier(monkeypatch) -> None:
    person = _person()
    person["identifier"] = None
    monkeypatch.setattr(
        workflow,
        "fetch_person_by_uuid_for_update",
        AsyncMock(return_value=person),
    )
    monkeypatch.setattr(
        workflow,
        "resolve_source_list_by_public_id",
        AsyncMock(return_value={"id": 1139, "status": "PROCESSED"}),
    )
    ensure_membership = AsyncMock()
    monkeypatch.setattr(workflow, "ensure_person_in_source_list", ensure_membership)

    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        await workflow._run_source_list_membership(
            db_session=_NestedSession(),  # type: ignore[arg-type]
            flow_uuid=FLOW_UUID,
            component=_component(),
            runtime_variables=_runtime(),
        )

    assert exc_info.value.code == "source_list_membership_person_without_identifier"
    ensure_membership.assert_not_awaited()


@pytest.mark.parametrize(
    ("parameters", "error_code"),
    [
        ({"person_uuid": "person-invalid"}, "source_list_membership_invalid_person_uuid"),
        ({"mailing_id": "mailing-invalid"}, "source_list_membership_invalid_mailing_id"),
        ({"mailing_id": None}, "source_list_membership_missing_mailing_id"),
        ({"output_var": "membership-result"}, "source_list_membership_invalid_output_var"),
    ],
)
@pytest.mark.asyncio
async def test_membership_rejects_invalid_runtime_contract(
    parameters,
    error_code,
) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        await workflow._run_source_list_membership(
            db_session=_NestedSession(),  # type: ignore[arg-type]
            flow_uuid=FLOW_UUID,
            component=_component(**parameters),
            runtime_variables=_runtime(),
        )

    assert exc_info.value.code == error_code


@pytest.mark.asyncio
async def test_membership_wraps_unexpected_persistence_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_person_by_uuid_for_update",
        AsyncMock(side_effect=RuntimeError("db unavailable")),
    )

    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        await workflow._run_source_list_membership(
            db_session=_NestedSession(),  # type: ignore[arg-type]
            flow_uuid=FLOW_UUID,
            component=_component(),
            runtime_variables=_runtime(),
        )

    assert exc_info.value.code == "source_list_membership_persistence_failed"


class _Transaction:
    async def __aenter__(self):
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

    def begin_nested(self) -> _Transaction:
        return _Transaction()

    async def execute(self, *_args, **_kwargs) -> _Result:
        return _Result()


def _configure_workflow_dependencies(
    monkeypatch,
    *,
    definition: dict,
    runtime: dict,
    persisted: list[dict],
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(workflow, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(workflow, "fetch_flow_row", AsyncMock(return_value={"id": FLOW_UUID}))
    monkeypatch.setattr(
        workflow,
        "resolve_workflow_revision_for_session",
        AsyncMock(
            return_value=WorkflowRevisionResolution(
                revision={
                    "id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                    "definition": definition,
                },
                source="pinned",
                requested_revision_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                failure_reason=None,
            )
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_session_workflow_state",
        AsyncMock(
            return_value={
                "uuid": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                "state": 0,
                "runtime_variables": runtime,
                "last_card_uuid": None,
                "next_card_uuid": definition["components"][0]["ref_id"],
                "frozen_until": None,
            }
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_contact_runtime_context_for_session",
        AsyncMock(return_value={"contact_list_member_id": 10, "person_uuid": PERSON_UUID}),
    )

    async def _replace(*_args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        persisted.append(kwargs)

    monkeypatch.setattr(workflow, "replace_session_workflow_state", _replace)
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())


@pytest.mark.asyncio
async def test_execute_workflow_routes_membership_by_linked_branch(monkeypatch) -> None:
    membership_ref = "11111111-1111-1111-1111-111111111111"
    finish_ref = "22222222-2222-2222-2222-222222222222"
    definition = {
        "components": [
            {**_component(), "ref_id": membership_ref},
            {"ref_id": finish_ref, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [{"from": membership_ref, "to": finish_ref, "branch": "linked"}],
    }
    runtime = _runtime()
    persisted: list[dict] = []
    _configure_workflow_dependencies(
        monkeypatch,
        definition=definition,
        runtime=runtime,
        persisted=persisted,
    )
    monkeypatch.setattr(
        workflow,
        "fetch_person_by_uuid_for_update",
        AsyncMock(return_value=_person()),
    )
    monkeypatch.setattr(
        workflow,
        "resolve_source_list_by_public_id",
        AsyncMock(return_value={"id": 1139, "status": "PROCESSED"}),
    )
    monkeypatch.setattr(
        workflow,
        "ensure_person_in_source_list",
        AsyncMock(return_value={"created": True, "contact_draft_id": "draft-1", "channels": 1}),
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.last_card_uuid == finish_ref
    assert runtime["variables"]["customs"]["source_list_membership"]["action"] == "linked"
    assert any(item.get("next_card_uuid") == finish_ref for item in persisted)


@pytest.mark.asyncio
async def test_execute_workflow_routes_membership_failure_to_exception(monkeypatch) -> None:
    membership_ref = "11111111-1111-1111-1111-111111111111"
    finish_ref = "22222222-2222-2222-2222-222222222222"
    definition = {
        "components": [
            {**_component(), "ref_id": membership_ref},
            {"ref_id": finish_ref, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [{"from": membership_ref, "to": finish_ref, "branch": "exception"}],
    }
    runtime = _runtime()
    persisted: list[dict] = []
    _configure_workflow_dependencies(
        monkeypatch,
        definition=definition,
        runtime=runtime,
        persisted=persisted,
    )
    monkeypatch.setattr(
        workflow,
        "fetch_person_by_uuid_for_update",
        AsyncMock(return_value=_person()),
    )
    monkeypatch.setattr(
        workflow,
        "resolve_source_list_by_public_id",
        AsyncMock(return_value={"id": 1139, "status": "UPLOADED"}),
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert runtime["source_list_membership_last_error"]["code"] == (
        "source_list_membership_mailing_not_ready"
    )
    assert any(item.get("next_card_uuid") == finish_ref for item in persisted)
