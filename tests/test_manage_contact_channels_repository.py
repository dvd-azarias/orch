from __future__ import annotations

import json

import pytest

import app.repositories.manage_contact_channels_repository as repository


PERSON_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _MappingsResult:
    def __init__(self, row=None):  # type: ignore[no-untyped-def]
        self.row = row

    def mappings(self):  # type: ignore[no-untyped-def]
        return self

    def first(self):  # type: ignore[no-untyped-def]
        return self.row


class _ScalarResult:
    def __init__(self, value):  # type: ignore[no-untyped-def]
        self.value = value

    def scalar_one(self):  # type: ignore[no-untyped-def]
        return self.value


class _Session:
    def __init__(self, results):  # type: ignore[no-untyped-def]
        self.results = iter(results)
        self.calls: list[tuple[object, dict]] = []

    async def execute(self, statement, params):  # type: ignore[no-untyped-def]
        self.calls.append((statement, params))
        return next(self.results)


@pytest.mark.asyncio
async def test_fetch_person_locks_only_non_merged_person() -> None:
    session = _Session([_MappingsResult({"uuid": PERSON_UUID, "channels": []})])

    person = await repository.fetch_manage_contact_channels_person_for_update(
        session,  # type: ignore[arg-type]
        person_uuid=PERSON_UUID,
    )

    assert person == {"uuid": PERSON_UUID, "channels": []}
    sql = str(session.calls[0][0]).lower()
    assert "from persons" in sql
    assert "merged_into_uuid is null" in sql
    assert "for update" in sql


@pytest.mark.asyncio
async def test_primary_projection_lock_is_transaction_scoped() -> None:
    session = _Session([_ScalarResult(None)])

    await repository.lock_manage_contact_channels_primary_projection(
        session,  # type: ignore[arg-type]
        channel_type="voice",
        channel_value="11975620806",
    )

    sql = str(session.calls[0][0]).lower()
    assert "pg_advisory_xact_lock" in sql
    assert session.calls[0][1]["projection_key"].endswith(
        ":voice:11975620806"
    )


@pytest.mark.asyncio
async def test_primary_projection_availability_excludes_current_person() -> None:
    session = _Session([_ScalarResult(True)])

    available = (
        await repository.manage_contact_channels_primary_projection_is_available(
            session,  # type: ignore[arg-type]
            person_uuid=PERSON_UUID,
            channel_type="voice",
            channel_value="11975620806",
        )
    )

    assert available is True
    sql = str(session.calls[0][0]).lower()
    assert "uuid <> cast(:person_uuid as uuid)" in sql
    assert "primary_channel_type = :channel_type" in sql


@pytest.mark.asyncio
async def test_update_binds_channels_and_optional_primary_projection() -> None:
    channels = [
        {
            "type": "voice",
            "value": "11975620806",
            "is_primary": True,
            "is_valid": True,
            "is_reachable": True,
        }
    ]
    session = _Session([_MappingsResult({"uuid": PERSON_UUID, "channels": channels})])

    updated = await repository.update_manage_contact_channels_person(
        session,  # type: ignore[arg-type]
        person_uuid=PERSON_UUID,
        channels=channels,
        primary_channel=channels[0],
    )

    assert updated == {"uuid": PERSON_UUID, "channels": channels}
    params = session.calls[0][1]
    assert json.loads(params["channels"]) == channels
    assert params["primary_channel_type"] == "voice"
    assert params["primary_channel_value"] == "11975620806"
