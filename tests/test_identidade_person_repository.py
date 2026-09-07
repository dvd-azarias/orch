from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

import app.repositories.identidade_person_repository as repository


class _Result:
    def __init__(self, row=None):  # type: ignore[no-untyped-def]
        self.row = row

    def mappings(self):  # type: ignore[no-untyped-def]
        return self

    def first(self):  # type: ignore[no-untyped-def]
        return self.row

    def one(self):  # type: ignore[no-untyped-def]
        return self.row


class _Session:
    def __init__(self, results):  # type: ignore[no-untyped-def]
        self.results = iter(results)
        self.calls: list[tuple[object, dict]] = []

    async def execute(self, statement, params):  # type: ignore[no-untyped-def]
        self.calls.append((statement, params))
        return next(self.results)


def _person_payload(**overrides):  # type: ignore[no-untyped-def]
    payload = {
        "identifier": "12345678901",
        "full_name": "Pessoa Teste",
        "company": None,
        "gender": None,
        "role": None,
        "country": "Brasil",
        "state": "RJ",
        "city": "Rio de Janeiro",
        "birthdate": "1940-08-12",
        "primary_channel_type": "voice",
        "primary_channel_value": "21999999999",
        "primary_channel_label": "identidade_tel_1",
        "channels": [],
        "extras": {},
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_fetch_person_by_uuid_for_membership_loads_profile_and_channels() -> None:
    person_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    session = _Session(
        [
            _Result(
                {
                    "id": 1,
                    "uuid": person_uuid,
                    "identifier": "12345678901",
                    "channels": [{"type": "phone", "value": "21999999999"}],
                }
            )
        ]
    )

    person = await repository.fetch_person_by_uuid_for_update(
        session,  # type: ignore[arg-type]
        person_uuid=person_uuid,
    )

    assert person == {
        "id": 1,
        "uuid": person_uuid,
        "identifier": "12345678901",
        "channels": [{"type": "phone", "value": "21999999999"}],
    }
    statement = str(session.calls[0][0]).lower()
    assert "from persons" in statement
    assert "merged_into_uuid is null" in statement
    assert "for update" in statement
    assert session.calls[0][1] == {"person_uuid": person_uuid}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("1940-08-12", date(1940, 8, 12)),
        (date(1940, 8, 12), date(1940, 8, 12)),
        (
            datetime(1940, 8, 12, 3, 0, tzinfo=timezone.utc),
            date(1940, 8, 12),
        ),
    ],
)
def test_birthdate_db_value_returns_asyncpg_compatible_date(raw, expected) -> None:  # type: ignore[no-untyped-def]
    assert repository._birthdate_db_value(raw) == expected


def test_birthdate_db_value_rejects_invalid_provider_date() -> None:
    with pytest.raises(ValueError):
        repository._birthdate_db_value("12/08/1940")


@pytest.mark.asyncio
async def test_insert_person_binds_birthdate_as_date() -> None:
    session = _Session(
        [
            _Result(
                {
                    "id": 1,
                    "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "identifier": "12345678901",
                }
            )
        ]
    )

    await repository.insert_person_if_missing(session, payload=_person_payload())  # type: ignore[arg-type]

    assert session.calls[0][1]["birthdate"] == date(1940, 8, 12)
    assert isinstance(session.calls[0][1]["birthdate"], date)


@pytest.mark.asyncio
async def test_update_person_binds_birthdate_as_date() -> None:
    session = _Session(
        [
            _Result(
                {
                    "id": 1,
                    "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "identifier": "12345678901",
                }
            )
        ]
    )

    await repository.update_person_from_payload(
        session,  # type: ignore[arg-type]
        person_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        payload=_person_payload(),
    )

    assert session.calls[0][1]["birthdate"] == date(1940, 8, 12)


@pytest.mark.asyncio
async def test_new_source_list_draft_binds_birthdate_as_date() -> None:
    session = _Session(
        [
            _Result(None),
            _Result({"id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}),
            _Result(),
            _Result(),
            _Result(),
        ]
    )

    await repository.ensure_person_in_source_list(
        session,  # type: ignore[arg-type]
        source_list_id=1139,
        person={
            **_person_payload(),
            "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        },
    )

    assert session.calls[1][1]["birthdate"] == date(1940, 8, 12)


@pytest.mark.asyncio
async def test_existing_source_list_draft_binds_birthdate_as_date() -> None:
    session = _Session(
        [
            _Result({"id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}),
            _Result(),
            _Result(),
        ]
    )

    await repository.ensure_person_in_source_list(
        session,  # type: ignore[arg-type]
        source_list_id=1139,
        person={
            **_person_payload(),
            "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        },
    )

    assert session.calls[1][1]["birthdate"] == date(1940, 8, 12)
