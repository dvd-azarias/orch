from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

import app.repositories.create_contact_repository as repository


class _Result:
    def __init__(self, row=None):  # type: ignore[no-untyped-def]
        self.row = row

    def mappings(self):  # type: ignore[no-untyped-def]
        return self

    def first(self):  # type: ignore[no-untyped-def]
        return self.row


class _Session:
    def __init__(self, results):  # type: ignore[no-untyped-def]
        self.results = iter(results)
        self.calls: list[tuple[object, dict]] = []

    async def execute(self, statement, params):  # type: ignore[no-untyped-def]
        self.calls.append((statement, params))
        return next(self.results)


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
def test_birthdate_db_value(raw, expected) -> None:  # type: ignore[no-untyped-def]
    assert repository._birthdate_db_value(raw) == expected


@pytest.mark.asyncio
async def test_insert_person_nao_declara_canais_listas_ou_sessoes() -> None:
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

    await repository.insert_create_contact_person_if_missing(
        session,  # type: ignore[arg-type]
        identifier="12345678901",
        payload={
            "full_name": "Pessoa Teste",
            "birthdate": "1940-08-12",
            "extras": {"segment": "premium"},
        },
    )

    statement = str(session.calls[0][0]).lower()
    params = session.calls[0][1]
    assert "insert into persons" in statement
    assert "channels" not in statement
    assert "primary_channel" not in statement
    assert "source_list" not in statement
    assert "orch_sessions" not in statement
    assert params["birthdate"] == date(1940, 8, 12)
    assert params["extras"] == '{"segment": "premium"}'


@pytest.mark.asyncio
async def test_update_person_nao_altera_identifier_canais_ou_vinculos() -> None:
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

    await repository.update_create_contact_person_profile(
        session,  # type: ignore[arg-type]
        person_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        payload={"full_name": "Novo Nome", "extras": {}},
    )

    statement = str(session.calls[0][0]).lower()
    update_clause = statement.split("set", 1)[1].split("where", 1)[0]
    assert "identifier" not in update_clause
    assert "channels" not in update_clause
    assert "primary_channel" not in update_clause
    assert "last_source_list" not in update_clause
    assert "last_mailing" not in update_clause
    assert "contact_list_members" not in statement
    assert "orch_sessions" not in statement
