from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.services.workflow_m2_service as workflow
from app.services.workflow_revision_service import WorkflowRevisionResolution


FLOW_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PERSON_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _component(resource: object, **parameters: object) -> dict:
    return {
        "ref_id": "manage-contact-1",
        "component_id": "manage_contact",
        "parameters": {
            "resource": resource,
            "output_var": "contact_management",
            **parameters,
        },
    }


@pytest.mark.parametrize(
    ("resource", "expected_kind", "parameters", "expected_parameters"),
    [
        (
            "person",
            "create_contact",
            {
                "person_action": "upsert",
                "identifier": "{{contact.identifier}}",
                "enrichment_policy": "fill_missing",
                "mapping": [{"key": "full_name", "value": "{{payload.name}}"}],
            },
            {
                "person_action": "upsert",
                "identifier": "{{contact.identifier}}",
                "enrichment_policy": "fill_missing",
                "mapping": [{"key": "full_name", "value": "{{payload.name}}"}],
                "output_var": "contact_management",
            },
        ),
        (
            "channels",
            "manage_contact_channels",
            {
                "channels_person_uuid": "{{contact.person_uuid}}",
                "channels_operation": "deactivate",
                "channels": [{"type": "voice", "address": "{{payload.phone}}"}],
            },
            {
                "person_uuid": "{{contact.person_uuid}}",
                "operation": "deactivate",
                "channels": [{"type": "voice", "address": "{{payload.phone}}"}],
                "output_var": "contact_management",
            },
        ),
        (
            "list_membership",
            "source_list_membership",
            {
                "membership_person_uuid": "{{contact.person_uuid}}",
                "mailing_source": "session_origin",
                "mailing_id": None,
                "membership_state": "inactive",
                "membership_purpose": "organization_only",
            },
            {
                "person_uuid": "{{contact.person_uuid}}",
                "mailing_source": "session_origin",
                "mailing_id": None,
                "membership_state": "inactive",
                "membership_purpose": "organization_only",
                "output_var": "contact_management",
            },
        ),
    ],
)
def test_manage_contact_delegates_to_existing_contracts(
    resource: str,
    expected_kind: str,
    parameters: dict,
    expected_parameters: dict,
) -> None:
    resolved_resource, delegate_kind, delegated = (
        workflow._manage_contact_delegate_component(_component(resource, **parameters))
    )

    assert resolved_resource == resource
    assert delegate_kind == expected_kind
    assert delegated["component_id"] == expected_kind
    assert delegated["ref_id"] == "manage-contact-1"
    assert delegated["parameters"] == expected_parameters


def test_manage_contact_rejects_unknown_resource() -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._manage_contact_delegate_component(_component("identity"))

    assert exc_info.value.code == "manage_contact_invalid_resource"


def test_manage_contact_person_is_valid_unbound_person_bootstrap() -> None:
    component = _component("person")

    effective_kind = workflow._manage_contact_effective_kind(component)

    assert effective_kind == "create_contact"
    assert workflow._unbound_person_component_allowed(
        component_kind_value=effective_kind,
        runtime_variables={},
    ) is True


def test_manage_contact_accepts_catalog_parameter_list_and_dropdown_value() -> None:
    component = {
        "ref_id": "manage-contact-list-1",
        "component_id": "manage_contact",
        "parameters": [
            {"id": "resource", "value": {"id": "channels", "name": "Canais"}},
            {"id": "channels_person_uuid", "value": "{{contact.person_uuid}}"},
            {"id": "channels_operation", "value": {"id": "upsert"}},
            {
                "id": "channels",
                "value": [{"type": "voice", "address": "{{payload.phone}}"}],
            },
            {"id": "output_var", "value": "contact_management"},
        ],
    }

    resource, delegate_kind, delegated = workflow._manage_contact_delegate_component(
        component
    )

    assert resource == "channels"
    assert delegate_kind == "manage_contact_channels"
    assert delegated["parameters"]["operation"] == {"id": "upsert"}


@pytest.mark.parametrize(
    ("resource", "runner_name", "delegate_kind", "delegate_branch", "parameters"),
    [
        (
            "person",
            "_run_create_contact",
            "create_contact",
            "created",
            {
                "person_action": "upsert",
                "identifier": "{{contact.identifier}}",
                "enrichment_policy": "fill_missing",
                "mapping": [{"key": "full_name", "value": "{{payload.name}}"}],
            },
        ),
        (
            "channels",
            "_run_manage_contact_channels",
            "manage_contact_channels",
            "changed",
            {
                "channels_person_uuid": "{{contact.person_uuid}}",
                "channels_operation": "upsert",
                "channels": [{"type": "voice", "address": "{{payload.phone}}"}],
            },
        ),
        (
            "list_membership",
            "_run_source_list_membership",
            "source_list_membership",
            "changed",
            {
                "membership_person_uuid": "{{contact.person_uuid}}",
                "mailing_source": "session_origin",
                "membership_state": "inactive",
                "membership_purpose": "organization_only",
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_execute_workflow_dispatches_manage_contact_and_uses_generic_branch(
    monkeypatch,
    resource: str,
    runner_name: str,
    delegate_kind: str,
    delegate_branch: str,
    parameters: dict,
) -> None:
    manage_ref = "11111111-1111-1111-1111-111111111111"
    finish_ref = "22222222-2222-2222-2222-222222222222"
    definition = {
        "components": [
            {**_component(resource, **parameters), "ref_id": manage_ref},
            {"ref_id": finish_ref, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [{"from": manage_ref, "to": finish_ref, "branch": "changed"}],
    }
    runtime = {
        "variables": {
            "payload": {"name": "Pessoa", "phone": "11999999999"},
            "customs": {},
            "contact": {
                "identifier": "12345678901",
                "person_uuid": PERSON_UUID,
            },
        },
        "workflow_v2": {},
    }

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

    observed: dict = {}

    async def _run_delegate(**kwargs) -> str:
        observed.update(kwargs)
        kwargs["runtime_variables"][f"{delegate_kind}_last_result"] = {
            "component_ref_id": manage_ref,
            "output_var": "contact_management",
            "result": {"action": delegate_branch},
        }
        return delegate_branch

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
                revision={
                    "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                    "definition": definition,
                },
                source="pinned",
                requested_revision_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
                failure_reason=None,
            )
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_session_workflow_state",
        AsyncMock(
            return_value={
                "uuid": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "state": 0,
                "runtime_variables": runtime,
                "last_card_uuid": None,
                "next_card_uuid": manage_ref,
                "frozen_until": None,
            }
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_contact_runtime_context_for_session",
        AsyncMock(
            return_value={
                "contact_list_member_id": 10,
                "person_uuid": PERSON_UUID,
            }
        ),
    )
    monkeypatch.setattr(workflow, runner_name, _run_delegate)
    monkeypatch.setattr(workflow, "replace_session_workflow_state", AsyncMock())
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.last_card_uuid == finish_ref
    assert observed["component"]["component_id"] == delegate_kind
    assert runtime["manage_contact_last_result"]["resource"] == resource
    assert runtime["manage_contact_last_result"]["branch"] == "changed"
