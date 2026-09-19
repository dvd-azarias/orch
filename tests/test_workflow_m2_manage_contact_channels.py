from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import ANY, AsyncMock

import pytest

import app.services.workflow_m2_service as workflow


PERSON_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
FLOW_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class _NestedSession:
    @asynccontextmanager
    async def begin_nested(self):
        yield


def _component(**parameters: object) -> dict:
    defaults = {
        "person_uuid": "{{contact.person_uuid}}",
        "operation": "upsert",
        "channels": [
            {
                "type": "voice",
                "address": "{{payload.phone}}",
                "label": "principal",
                "priority": 1,
                "is_primary": True,
            }
        ],
        "output_var": "contact_channels",
    }
    defaults.update(parameters)
    return {
        "ref_id": "manage-contact-channels-1",
        "component_id": "manage_contact_channels",
        "parameters": defaults,
    }


def _runtime() -> dict:
    return {
        "variables": {
            "payload": {"phone": "+55 (11) 97562-0806"},
            "customs": {},
            "contact": {
                "identifier": "12345678901",
                "person_uuid": PERSON_UUID,
            },
        },
        "workflow_v2": {
            "person_adoption": {
                "status": "adopted",
                "person_uuid": PERSON_UUID,
                "identifier": "12345678901",
            }
        },
    }


def _person(**overrides: object) -> dict:
    person = {
        "id": 1,
        "uuid": PERSON_UUID,
        "identifier": "12345678901",
        "primary_channel_type": None,
        "primary_channel_value": None,
        "primary_channel_label": None,
        "channels": [],
    }
    person.update(overrides)
    return person


@pytest.mark.asyncio
async def test_upsert_persists_normalized_channel_and_output(monkeypatch) -> None:
    fetch = AsyncMock(return_value=_person())
    lock = AsyncMock()
    available = AsyncMock(return_value=True)
    updated_person = _person(
        primary_channel_type="voice",
        primary_channel_value="11975620806",
        primary_channel_label="principal",
    )
    update = AsyncMock(return_value=updated_person)
    monkeypatch.setattr(workflow, "fetch_manage_contact_channels_person_for_update", fetch)
    monkeypatch.setattr(workflow, "lock_manage_contact_channels_primary_projection", lock)
    monkeypatch.setattr(
        workflow,
        "manage_contact_channels_primary_projection_is_available",
        available,
    )
    monkeypatch.setattr(workflow, "update_manage_contact_channels_person", update)
    runtime = _runtime()

    branch = await workflow._run_manage_contact_channels(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(),
        runtime_variables=runtime,
        contact_row=None,
    )

    assert branch == "changed"
    fetch.assert_awaited_once_with(ANY, person_uuid=PERSON_UUID)
    lock.assert_awaited_once_with(
        ANY,
        channel_type="voice",
        channel_value="11975620806",
    )
    update.assert_awaited_once()
    persisted = update.await_args.kwargs
    assert persisted["person_uuid"] == PERSON_UUID
    assert persisted["primary_channel"]["value"] == "11975620806"
    assert persisted["channels"][0]["state"] == "active"
    output = runtime["variables"]["customs"]["contact_channels"]
    assert output["action"] == "changed"
    assert output["changed_channels"] == [
        {"type": "voice", "address": "11975620806"}
    ]
    assert output["primary_projection_applied"] is True


@pytest.mark.asyncio
async def test_upsert_same_value_is_unchanged(monkeypatch) -> None:
    existing_channel = {
        "type": "voice",
        "value": "11975620806",
        "label": "principal",
        "priority": 1,
        "is_primary": True,
        "state": "active",
        "is_valid": True,
        "is_reachable": True,
        "provider": "orch",
    }
    monkeypatch.setattr(
        workflow,
        "fetch_manage_contact_channels_person_for_update",
        AsyncMock(
            return_value=_person(
                primary_channel_type="voice",
                primary_channel_value="11975620806",
                primary_channel_label="principal",
                channels=[existing_channel],
            )
        ),
    )
    monkeypatch.setattr(
        workflow,
        "lock_manage_contact_channels_primary_projection",
        AsyncMock(),
    )
    monkeypatch.setattr(
        workflow,
        "manage_contact_channels_primary_projection_is_available",
        AsyncMock(return_value=True),
    )
    update = AsyncMock()
    monkeypatch.setattr(workflow, "update_manage_contact_channels_person", update)
    runtime = _runtime()

    branch = await workflow._run_manage_contact_channels(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(),
        runtime_variables=runtime,
        contact_row=None,
    )

    assert branch == "unchanged"
    update.assert_not_awaited()
    assert runtime["variables"]["customs"]["contact_channels"]["action"] == "unchanged"


@pytest.mark.asyncio
async def test_projection_collision_does_not_block_same_address_on_another_person(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_manage_contact_channels_person_for_update",
        AsyncMock(return_value=_person()),
    )
    monkeypatch.setattr(
        workflow,
        "lock_manage_contact_channels_primary_projection",
        AsyncMock(),
    )
    monkeypatch.setattr(
        workflow,
        "manage_contact_channels_primary_projection_is_available",
        AsyncMock(return_value=False),
    )
    update = AsyncMock(return_value=_person())
    monkeypatch.setattr(workflow, "update_manage_contact_channels_person", update)
    runtime = _runtime()

    branch = await workflow._run_manage_contact_channels(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(),
        runtime_variables=runtime,
        contact_row=None,
    )

    assert branch == "changed"
    assert update.await_args.kwargs["primary_channel"] is None
    assert update.await_args.kwargs["channels"][0]["is_primary"] is True
    assert (
        runtime["variables"]["customs"]["contact_channels"][
            "primary_projection_applied"
        ]
        is False
    )


@pytest.mark.asyncio
async def test_deactivate_missing_channel_is_not_found_without_write(monkeypatch) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_manage_contact_channels_person_for_update",
        AsyncMock(return_value=_person()),
    )
    update = AsyncMock()
    monkeypatch.setattr(workflow, "update_manage_contact_channels_person", update)
    runtime = _runtime()

    branch = await workflow._run_manage_contact_channels(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(operation="deactivate"),
        runtime_variables=runtime,
        contact_row=None,
    )

    assert branch == "not_found"
    update.assert_not_awaited()
    output = runtime["variables"]["customs"]["contact_channels"]
    assert output["missing_channels"] == [
        {"type": "voice", "address": "11975620806"}
    ]


@pytest.mark.asyncio
async def test_conflicting_duplicate_uses_conflict_branch_without_db(monkeypatch) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(workflow, "fetch_manage_contact_channels_person_for_update", fetch)
    runtime = _runtime()

    branch = await workflow._run_manage_contact_channels(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(
            channels=[
                {"type": "voice", "address": "11975620806", "label": "A"},
                {"type": "voice", "address": "+55 11 97562-0806", "label": "B"},
            ]
        ),
        runtime_variables=runtime,
        contact_row=None,
    )

    assert branch == "conflict"
    fetch.assert_not_awaited()
    assert runtime["variables"]["customs"]["contact_channels"]["conflict"]["code"] == (
        "manage_contact_channels_conflicting_duplicate"
    )


def test_unbound_person_allows_manage_channels_only_after_adoption() -> None:
    assert workflow._unbound_person_component_allowed(
        component_kind_value="manage_contact_channels",
        runtime_variables=_runtime(),
    )
    assert not workflow._unbound_person_component_allowed(
        component_kind_value="manage_contact_channels",
        runtime_variables={"variables": {"contact": {}, "customs": {}}},
    )
