from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_PERSON_RETURNING_COLUMNS = """
    id,
    uuid::text AS uuid,
    identifier,
    full_name,
    company,
    gender,
    role,
    country,
    state,
    city,
    birthdate,
    extras
"""


def _birthdate_db_value(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    normalized = str(value).strip()
    if not normalized:
        return None
    return date.fromisoformat(normalized)


def _person_parameters(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "full_name": payload.get("full_name"),
        "company": payload.get("company"),
        "gender": payload.get("gender"),
        "role": payload.get("role"),
        "country": payload.get("country"),
        "state": payload.get("state"),
        "city": payload.get("city"),
        "birthdate": _birthdate_db_value(payload.get("birthdate")),
        "extras": json.dumps(
            payload.get("extras") if isinstance(payload.get("extras"), dict) else {},
            ensure_ascii=False,
        ),
    }


async def fetch_create_contact_person_by_uuid_for_update(
    db_session: AsyncSession,
    *,
    person_uuid: str,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            f"""
            SELECT {_PERSON_RETURNING_COLUMNS}
            FROM persons
            WHERE uuid = CAST(:person_uuid AS uuid)
              AND merged_into_uuid IS NULL
            LIMIT 1
            FOR UPDATE
            """
        ),
        {"person_uuid": person_uuid},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def fetch_create_contact_person_by_identifier_for_update(
    db_session: AsyncSession,
    *,
    identifier: str,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            f"""
            SELECT {_PERSON_RETURNING_COLUMNS}
            FROM persons
            WHERE identifier = :identifier
              AND merged_into_uuid IS NULL
            LIMIT 1
            FOR UPDATE
            """
        ),
        {"identifier": identifier},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def insert_create_contact_person_if_missing(
    db_session: AsyncSession,
    *,
    identifier: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    parameters = {
        "identifier": identifier,
        **_person_parameters(payload),
    }
    result = await db_session.execute(
        text(
            f"""
            INSERT INTO persons (
                identifier,
                full_name,
                company,
                gender,
                role,
                country,
                state,
                city,
                birthdate,
                extras,
                created_at,
                updated_at
            ) VALUES (
                :identifier,
                :full_name,
                :company,
                :gender,
                :role,
                :country,
                :state,
                :city,
                :birthdate,
                CAST(:extras AS jsonb),
                NOW(),
                NOW()
            )
            ON CONFLICT (identifier) DO NOTHING
            RETURNING {_PERSON_RETURNING_COLUMNS}
            """
        ),
        parameters,
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def update_create_contact_person_profile(
    db_session: AsyncSession,
    *,
    person_uuid: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    parameters = {
        "person_uuid": person_uuid,
        **_person_parameters(payload),
    }
    result = await db_session.execute(
        text(
            f"""
            UPDATE persons
            SET
                full_name = :full_name,
                company = :company,
                gender = :gender,
                role = :role,
                country = :country,
                state = :state,
                city = :city,
                birthdate = :birthdate,
                extras = CAST(:extras AS jsonb),
                updated_at = NOW()
            WHERE uuid = CAST(:person_uuid AS uuid)
              AND merged_into_uuid IS NULL
            RETURNING {_PERSON_RETURNING_COLUMNS}
            """
        ),
        parameters,
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None
