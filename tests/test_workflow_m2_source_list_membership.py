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
        "membership_state": "active",
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


def _runtime_with_session_origin() -> dict:
    runtime = _runtime()
    runtime["input_payload"] = {
        "session_scope": "person",
        "mailing_id": 1139,
        "contact_list_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "contact_list_member_id": 77,
    }
    return runtime


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


@pytest.mark.parametrize(
    "error_code",
    [
        "source_list_membership_flow_link_target_core_http_error",
        "source_list_membership_materialization_not_found",
        "source_list_membership_persistence_failed",
    ],
)
def test_membership_errors_are_terminal_without_exception_branch(
    error_code: str,
) -> None:
    assert workflow._is_terminal_workflow_error_code(error_code) is True


@pytest.fixture(autouse=True)
def _flow_scope_dependencies(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        workflow,
        "fetch_active_flow_mailing_link",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        workflow,
        "set_person_materialized_membership_state",
        AsyncMock(),
    )


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
        session_id=123,
        component=_component(
            person_uuid="{{contact_action.person_uuid}}",
            mailing_id={"mailing_id": MAILING_UUID, "name": "Lista Teste"},
            output_var="membership_result",
        ),
        runtime_variables=runtime,
    )

    assert branch == "changed"
    fetch_person.assert_awaited_once_with(ANY, person_uuid=PERSON_UUID)
    resolve_list.assert_awaited_once_with(ANY, public_id=MAILING_UUID)
    ensure_membership.assert_awaited_once_with(
        ANY,
        source_list_id=1139,
        person=_person(),
    )
    assert runtime["variables"]["customs"]["membership_result"] == {
        "action": "changed",
        "desired_state": "active",
        "previous_state": "absent",
        "current_state": "active",
        "changed": True,
        "scope": "current_flow",
        "person_uuid": PERSON_UUID,
        "mailing_id": MAILING_UUID,
        "source_list_id": 1139,
        "contact_draft_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "channels": 1,
        "contact_list_id": None,
        "flow_link_found": False,
        "source_membership_created": True,
        "materialized_members": 0,
        "members_changed": 0,
        "sessions_stopped": 0,
        "sessions_created": 0,
        "missing": None,
    }
    assert runtime["source_list_membership_last_result"]["result"]["action"] == "changed"


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
        {"id": "membership_state", "value": [{"id": "active", "name": "Ativo"}]},
        {"id": "output_var", "value": "source_list_membership"},
    ]
    runtime = _runtime()

    branch = await workflow._run_source_list_membership(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
        component=component,
        runtime_variables=runtime,
    )

    assert branch == "unchanged"
    ensure_membership.assert_awaited_once()
    assert runtime["variables"]["customs"]["source_list_membership"]["action"] == (
        "unchanged"
    )


@pytest.mark.asyncio
async def test_membership_operational_persists_then_blocks_for_target_refresh(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_person_by_uuid_for_update",
        AsyncMock(return_value=_person()),
    )
    monkeypatch.setattr(
        workflow,
        "resolve_source_list_by_public_id",
        AsyncMock(
            return_value={"id": 1139, "public_id": MAILING_UUID, "status": "PROCESSED"}
        ),
    )
    monkeypatch.setattr(
        workflow,
        "ensure_person_in_source_list",
        AsyncMock(
            return_value={
                "created": True,
                "contact_draft_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "channels": 2,
            }
        ),
    )
    runtime = _runtime()

    branch = await workflow._run_source_list_membership(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
        component=_component(
            mailing_source="selected",
            membership_purpose="current_flow_operational",
        ),
        runtime_variables=runtime,
    )

    assert branch is None
    state = runtime["workflow_v2"]["source_list_membership_flow_link"]
    assert state == {
        "component_ref_id": "source-list-membership-1",
        "flow_uuid": FLOW_UUID,
        "mailing_uuid": MAILING_UUID,
        "person_uuid": PERSON_UUID,
        "status": "pending",
        "attempts": 0,
        "status_code": None,
        "initial_operation_changed": True,
        "source_membership_created": True,
        "requested_at": ANY,
        "completed_at": None,
        "last_error": None,
    }
    output = runtime["variables"]["customs"]["source_list_membership"]
    assert output["membership_purpose"] == "current_flow_operational"
    assert output["flow_link_status"] == "pending"
    assert output["sessions_created"] == 0


@pytest.mark.asyncio
async def test_membership_operational_resumes_same_card_after_materialization(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_person_by_uuid_for_update",
        AsyncMock(return_value=_person()),
    )
    monkeypatch.setattr(
        workflow,
        "resolve_source_list_by_public_id",
        AsyncMock(
            return_value={"id": 1139, "public_id": MAILING_UUID, "status": "PROCESSED"}
        ),
    )
    monkeypatch.setattr(
        workflow,
        "ensure_person_in_source_list",
        AsyncMock(
            return_value={
                "created": False,
                "contact_draft_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "channels": 2,
            }
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_active_flow_mailing_link",
        AsyncMock(
            return_value={
                "contact_list_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
            }
        ),
    )
    monkeypatch.setattr(
        workflow,
        "set_person_materialized_membership_state",
        AsyncMock(
            return_value={
                "contact_list_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                "previous_state": "active",
                "matched_members": 2,
                "members_changed": 0,
                "sessions_stopped": 0,
            }
        ),
    )
    runtime = _runtime()
    runtime["workflow_v2"] = {
        "source_list_membership_flow_link": {
            "component_ref_id": "source-list-membership-1",
            "status": "completed",
            "initial_operation_changed": True,
            "source_membership_created": True,
        }
    }

    branch = await workflow._run_source_list_membership(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
        component=_component(
            mailing_source="selected",
            membership_purpose="current_flow_operational",
        ),
        runtime_variables=runtime,
    )

    assert branch == "changed"
    assert runtime["workflow_v2"]["source_list_membership_flow_link"]["status"] == (
        "consumed"
    )
    output = runtime["variables"]["customs"]["source_list_membership"]
    assert output["flow_link_status"] == "completed"
    assert output["source_membership_created"] is True
    assert output["materialized_members"] == 2
    assert output["sessions_created"] == 0
    operational_scope = runtime["workflow_v2"][
        "source_list_membership_operational_scope"
    ]
    assert operational_scope["status"] == "ready"
    assert operational_scope["contact_list_id"] == (
        "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    )
    assert operational_scope["mailing_id"] == 1139
    assert operational_scope["person_uuid"] == PERSON_UUID


@pytest.mark.asyncio
async def test_membership_operational_failed_refresh_routes_as_runtime_error() -> None:
    runtime = _runtime()
    runtime["workflow_v2"] = {
        "source_list_membership_flow_link": {
            "component_ref_id": "source-list-membership-1",
            "status": "failed",
            "last_error": {
                "code": "source_list_membership_flow_link_target_core_http_error",
                "message": "Target Core respondeu HTTP 503.",
            },
        }
    }

    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        await workflow._run_source_list_membership(
            db_session=_NestedSession(),  # type: ignore[arg-type]
            flow_uuid=FLOW_UUID,
            session_id=123,
            component=_component(
                mailing_source="selected",
                membership_purpose="current_flow_operational",
            ),
            runtime_variables=runtime,
        )

    assert exc_info.value.code == (
        "source_list_membership_flow_link_target_core_http_error"
    )
    assert runtime["workflow_v2"]["source_list_membership_flow_link"]["status"] == (
        "consumed"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parameters", "error_code"),
    [
        (
            {
                "membership_state": "inactive",
                "membership_purpose": "current_flow_operational",
            },
            "source_list_membership_operational_requires_active",
        ),
        (
            {
                "mailing_source": "session_origin",
                "mailing_id": None,
                "membership_purpose": "current_flow_operational",
            },
            "source_list_membership_operational_requires_selected_mailing",
        ),
    ],
)
async def test_membership_operational_rejects_unsafe_runtime_shapes(
    parameters: dict,
    error_code: str,
) -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        await workflow._run_source_list_membership(
            db_session=_NestedSession(),  # type: ignore[arg-type]
            flow_uuid=FLOW_UUID,
            session_id=123,
            component=_component(**parameters),
            runtime_variables=_runtime_with_session_origin(),
        )

    assert exc_info.value.code == error_code


@pytest.mark.asyncio
async def test_membership_resolves_source_list_from_immutable_session_origin(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_person_by_uuid_for_update",
        AsyncMock(return_value=_person()),
    )
    resolve_from_origin = AsyncMock(
        return_value={
            "id": 1139,
            "public_id": MAILING_UUID,
            "status": "PROCESSED",
        }
    )
    resolve_by_public_id = AsyncMock()
    monkeypatch.setattr(
        workflow,
        "resolve_source_list_from_session_origin",
        resolve_from_origin,
    )
    monkeypatch.setattr(
        workflow,
        "resolve_source_list_by_public_id",
        resolve_by_public_id,
    )
    monkeypatch.setattr(
        workflow,
        "fetch_active_flow_mailing_link",
        AsyncMock(
            return_value={
                "mailing_id": 1139,
                "contact_list_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            }
        ),
    )
    set_state = AsyncMock(
        return_value={
            "contact_list_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "previous_state": "active",
            "matched_members": 1,
            "members_changed": 1,
            "sessions_stopped": 0,
        }
    )
    monkeypatch.setattr(workflow, "set_person_materialized_membership_state", set_state)
    runtime = _runtime_with_session_origin()

    branch = await workflow._run_source_list_membership(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
        component=_component(
            mailing_source="session_origin",
            mailing_id=None,
            membership_state="inactive",
        ),
        runtime_variables=runtime,
    )

    assert branch == "changed"
    resolve_from_origin.assert_awaited_once_with(
        ANY,
        flow_uuid=FLOW_UUID,
        session_id=123,
        source_list_id=1139,
        person_uuid=PERSON_UUID,
    )
    resolve_by_public_id.assert_not_awaited()
    assert runtime["variables"]["customs"]["source_list_membership"]["mailing_id"] == (
        MAILING_UUID
    )
    set_state.assert_awaited_once_with(
        ANY,
        flow_uuid=FLOW_UUID,
        current_session_id=123,
        source_list_id=1139,
        contact_list_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        person_uuid=PERSON_UUID,
        contact_draft_id=None,
        identifier="12345678901",
        desired_state="inactive",
    )


@pytest.mark.asyncio
async def test_membership_session_origin_requires_session_mailing_id() -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        await workflow._run_source_list_membership(
            db_session=_NestedSession(),  # type: ignore[arg-type]
            flow_uuid=FLOW_UUID,
            session_id=123,
            component=_component(
                mailing_source="session_origin",
                mailing_id=None,
            ),
            runtime_variables=_runtime(),
        )

    assert exc_info.value.code == "source_list_membership_missing_session_mailing_id"


@pytest.mark.asyncio
async def test_membership_active_reactivates_existing_materialized_members(monkeypatch) -> None:
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
        AsyncMock(
            return_value={
                "created": False,
                "contact_draft_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "channels": 2,
            }
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_active_flow_mailing_link",
        AsyncMock(return_value={"contact_list_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"}),
    )
    set_state = AsyncMock(
        return_value={
            "contact_list_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            "previous_state": "inactive",
            "matched_members": 2,
            "members_changed": 2,
            "sessions_stopped": 0,
        }
    )
    monkeypatch.setattr(workflow, "set_person_materialized_membership_state", set_state)
    runtime = _runtime()

    branch = await workflow._run_source_list_membership(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
        component=_component(membership_state="active"),
        runtime_variables=runtime,
    )

    assert branch == "changed"
    set_state.assert_awaited_once_with(
        ANY,
        flow_uuid=FLOW_UUID,
        current_session_id=123,
        source_list_id=1139,
        contact_list_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        person_uuid=PERSON_UUID,
        contact_draft_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        identifier="12345678901",
        desired_state="active",
    )
    result = runtime["variables"]["customs"]["source_list_membership"]
    assert result["previous_state"] == "inactive"
    assert result["current_state"] == "active"
    assert result["members_changed"] == 2
    assert result["sessions_created"] == 0


@pytest.mark.asyncio
async def test_membership_inactive_changes_only_materialized_scope(monkeypatch) -> None:
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
    monkeypatch.setattr(
        workflow,
        "fetch_active_flow_mailing_link",
        AsyncMock(
            return_value={
                "mailing_id": 1139,
                "contact_list_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            }
        ),
    )
    set_state = AsyncMock(
        return_value={
            "contact_list_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "previous_state": "active",
            "matched_members": 3,
            "members_changed": 3,
            "sessions_stopped": 2,
        }
    )
    monkeypatch.setattr(workflow, "set_person_materialized_membership_state", set_state)
    ensure_membership = AsyncMock()
    monkeypatch.setattr(workflow, "ensure_person_in_source_list", ensure_membership)
    runtime = _runtime()
    runtime["workflow_v2"] = {
        "selected_contact_channel": {
            "selected": True,
            "session_scope": "person",
            "contact_list_member_id": 77,
            "contact_list_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "mailing_id": 1139,
            "person_uuid": PERSON_UUID,
            "type": "voice",
            "address": "21999999999",
        }
    }

    branch = await workflow._run_source_list_membership(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
        component=_component(membership_state={"id": "inactive", "name": "Inativo"}),
        runtime_variables=runtime,
    )

    assert branch == "changed"
    ensure_membership.assert_not_awaited()
    set_state.assert_awaited_once_with(
        ANY,
        flow_uuid=FLOW_UUID,
        current_session_id=123,
        source_list_id=1139,
        contact_list_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        person_uuid=PERSON_UUID,
        contact_draft_id=None,
        identifier="12345678901",
        desired_state="inactive",
    )
    assert runtime["variables"]["customs"]["source_list_membership"] == {
        "action": "changed",
        "desired_state": "inactive",
        "previous_state": "active",
        "current_state": "inactive",
        "changed": True,
        "scope": "current_flow",
        "person_uuid": PERSON_UUID,
        "mailing_id": MAILING_UUID,
        "source_list_id": 1139,
        "contact_draft_id": None,
        "channels": None,
        "contact_list_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "flow_link_found": True,
        "source_membership_created": False,
        "materialized_members": 3,
        "members_changed": 3,
        "sessions_stopped": 2,
        "sessions_created": 0,
        "missing": None,
        "selected_contact_channel_invalidated": True,
    }
    assert "selected_contact_channel" not in runtime["workflow_v2"]


@pytest.mark.asyncio
async def test_membership_inactive_without_flow_link_follows_not_found(monkeypatch) -> None:
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
    set_state = AsyncMock()
    monkeypatch.setattr(workflow, "set_person_materialized_membership_state", set_state)
    runtime = _runtime()

    branch = await workflow._run_source_list_membership(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
        component=_component(membership_state="inactive"),
        runtime_variables=runtime,
    )

    assert branch == "not_found"
    set_state.assert_not_awaited()
    result = runtime["variables"]["customs"]["source_list_membership"]
    assert result["missing"] == "flow_mailing_link"
    assert result["current_state"] is None


@pytest.mark.asyncio
async def test_membership_inactive_without_materialized_member_follows_not_found(
    monkeypatch,
) -> None:
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
        "fetch_active_flow_mailing_link",
        AsyncMock(return_value={"contact_list_id": "dddddddd-dddd-dddd-dddd-dddddddddddd"}),
    )
    monkeypatch.setattr(
        workflow,
        "set_person_materialized_membership_state",
        AsyncMock(
            return_value={
                "contact_list_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "previous_state": "absent",
                "matched_members": 0,
                "members_changed": 0,
                "sessions_stopped": 0,
            }
        ),
    )
    runtime = _runtime()

    branch = await workflow._run_source_list_membership(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
        component=_component(membership_state="inactive"),
        runtime_variables=runtime,
    )

    assert branch == "not_found"
    assert runtime["variables"]["customs"]["source_list_membership"]["missing"] == (
        "materialized_member"
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
        session_id=123,
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
        session_id=123,
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
        session_id=123,
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
            session_id=123,
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
            session_id=123,
            component=_component(),
            runtime_variables=_runtime(),
        )

    assert exc_info.value.code == "source_list_membership_person_without_identifier"
    ensure_membership.assert_not_awaited()


@pytest.mark.parametrize(
    ("parameters", "error_code"),
    [
        ({"person_uuid": "person-invalid"}, "source_list_membership_invalid_person_uuid"),
        (
            {"mailing_source": "current"},
            "source_list_membership_invalid_mailing_source",
        ),
        ({"mailing_id": "mailing-invalid"}, "source_list_membership_invalid_mailing_id"),
        ({"mailing_id": None}, "source_list_membership_missing_mailing_id"),
        ({"membership_state": None}, "source_list_membership_missing_state"),
        ({"membership_state": "disabled"}, "source_list_membership_invalid_state"),
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
            session_id=123,
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
            session_id=123,
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
async def test_execute_workflow_routes_membership_by_changed_branch(monkeypatch) -> None:
    membership_ref = "11111111-1111-1111-1111-111111111111"
    finish_ref = "22222222-2222-2222-2222-222222222222"
    definition = {
        "components": [
            {**_component(), "ref_id": membership_ref},
            {"ref_id": finish_ref, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [{"from": membership_ref, "to": finish_ref, "branch": "changed"}],
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
    assert runtime["variables"]["customs"]["source_list_membership"]["action"] == "changed"
    assert any(item.get("next_card_uuid") == finish_ref for item in persisted)


@pytest.mark.asyncio
async def test_execute_workflow_blocks_operational_membership_on_same_card(
    monkeypatch,
) -> None:
    membership_ref = "11111111-1111-1111-1111-111111111111"
    finish_ref = "22222222-2222-2222-2222-222222222222"
    definition = {
        "components": [
            {
                **_component(
                    mailing_source="selected",
                    membership_purpose="current_flow_operational",
                ),
                "ref_id": membership_ref,
            },
            {"ref_id": finish_ref, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [
            {"from": membership_ref, "to": finish_ref, "branch": "changed"}
        ],
    }
    runtime = _runtime()
    persisted: list[dict] = []
    _configure_workflow_dependencies(
        monkeypatch,
        definition=definition,
        runtime=runtime,
        persisted=persisted,
    )
    run_membership = AsyncMock(return_value=None)
    monkeypatch.setattr(workflow, "_run_source_list_membership", run_membership)

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "blocked_source_list_membership_flow_link"
    assert result.executed_steps == 1
    run_membership.assert_awaited_once()
    assert persisted[-1]["last_card_uuid"] == membership_ref
    assert persisted[-1]["next_card_uuid"] == membership_ref
    assert runtime["workflow_v2"]["blocking_stop_reason"] == (
        "blocked_source_list_membership_flow_link"
    )


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
