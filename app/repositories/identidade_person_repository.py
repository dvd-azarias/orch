from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def fetch_person_by_identifier_for_update(
    db_session: AsyncSession,
    *,
    identifier: str,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            """
            SELECT
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
                primary_channel_type,
                primary_channel_value,
                primary_channel_label,
                channels,
                extras,
                last_contact_draft_id::text AS last_contact_draft_id,
                last_source_list_id,
                last_mailing_id
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


async def insert_person_if_missing(
    db_session: AsyncSession,
    *,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    channels = payload.get("channels") if isinstance(payload.get("channels"), list) else []
    extras = payload.get("extras") if isinstance(payload.get("extras"), dict) else {}
    result = await db_session.execute(
        text(
            """
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
                primary_channel_type,
                primary_channel_value,
                primary_channel_label,
                channels,
                extras,
                last_seen_at,
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
                :primary_channel_type,
                :primary_channel_value,
                :primary_channel_label,
                CAST(:channels AS jsonb),
                CAST(:extras AS jsonb),
                NOW(),
                NOW(),
                NOW()
            )
            ON CONFLICT (identifier) DO NOTHING
            RETURNING
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
                primary_channel_type,
                primary_channel_value,
                primary_channel_label,
                channels,
                extras,
                last_contact_draft_id::text AS last_contact_draft_id,
                last_source_list_id,
                last_mailing_id
            """
        ),
        {
            "identifier": payload.get("identifier"),
            "full_name": payload.get("full_name"),
            "company": payload.get("company"),
            "gender": payload.get("gender"),
            "role": payload.get("role"),
            "country": payload.get("country"),
            "state": payload.get("state"),
            "city": payload.get("city"),
            "birthdate": payload.get("birthdate"),
            "primary_channel_type": payload.get("primary_channel_type"),
            "primary_channel_value": payload.get("primary_channel_value"),
            "primary_channel_label": payload.get("primary_channel_label"),
            "channels": json.dumps(channels, ensure_ascii=False),
            "extras": json.dumps(extras, ensure_ascii=False),
        },
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def update_person_from_payload(
    db_session: AsyncSession,
    *,
    person_uuid: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    channels = payload.get("channels") if isinstance(payload.get("channels"), list) else []
    extras = payload.get("extras") if isinstance(payload.get("extras"), dict) else {}
    result = await db_session.execute(
        text(
            """
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
                primary_channel_type = :primary_channel_type,
                primary_channel_value = :primary_channel_value,
                primary_channel_label = :primary_channel_label,
                channels = CAST(:channels AS jsonb),
                extras = CAST(:extras AS jsonb),
                last_seen_at = NOW(),
                updated_at = NOW()
            WHERE uuid = CAST(:person_uuid AS uuid)
              AND merged_into_uuid IS NULL
            RETURNING
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
                primary_channel_type,
                primary_channel_value,
                primary_channel_label,
                channels,
                extras,
                last_contact_draft_id::text AS last_contact_draft_id,
                last_source_list_id,
                last_mailing_id
            """
        ),
        {
            "person_uuid": person_uuid,
            "full_name": payload.get("full_name"),
            "company": payload.get("company"),
            "gender": payload.get("gender"),
            "role": payload.get("role"),
            "country": payload.get("country"),
            "state": payload.get("state"),
            "city": payload.get("city"),
            "birthdate": payload.get("birthdate"),
            "primary_channel_type": payload.get("primary_channel_type"),
            "primary_channel_value": payload.get("primary_channel_value"),
            "primary_channel_label": payload.get("primary_channel_label"),
            "channels": json.dumps(channels, ensure_ascii=False),
            "extras": json.dumps(extras, ensure_ascii=False),
        },
    )
    row = result.mappings().one()
    return dict(row)


async def resolve_source_list_by_public_id(
    db_session: AsyncSession,
    *,
    public_id: str,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            """
            SELECT id, public_id::text AS public_id, name, status, origin
            FROM source_lists
            WHERE public_id = CAST(:public_id AS uuid)
            LIMIT 1
            FOR UPDATE
            """
        ),
        {"public_id": public_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def ensure_person_in_source_list(
    db_session: AsyncSession,
    *,
    source_list_id: int,
    person: dict[str, Any],
) -> dict[str, Any]:
    identifier = str(person.get("identifier") or "").strip()
    channels = [
        dict(item)
        for item in (person.get("channels") if isinstance(person.get("channels"), list) else [])
        if isinstance(item, dict) and str(item.get("type") or "").strip() and str(item.get("value") or "").strip()
    ]
    existing_result = await db_session.execute(
        text(
            """
            SELECT d.id::text AS id
            FROM source_list_contact_drafts slcd
            JOIN contact_drafts d ON d.id = slcd.contact_draft_id
            WHERE slcd.source_list_id = :source_list_id
              AND d.identifier = :identifier
            ORDER BY d.created_at, d.id
            LIMIT 1
            FOR UPDATE OF d
            """
        ),
        {"source_list_id": source_list_id, "identifier": identifier},
    )
    existing = existing_result.mappings().first()
    created = existing is None

    if created:
        draft_result = await db_session.execute(
            text(
                """
                INSERT INTO contact_drafts (
                    mailing_id,
                    mailing_row_id,
                    full_name,
                    identifier,
                    company,
                    gender,
                    role,
                    country,
                    state,
                    city,
                    birthdate,
                    validation_status,
                    is_blacklisted,
                    is_duplicate,
                    extras,
                    created_at,
                    updated_at
                ) VALUES (
                    NULL,
                    NULL,
                    :full_name,
                    :identifier,
                    :company,
                    :gender,
                    :role,
                    :country,
                    :state,
                    :city,
                    :birthdate,
                    'valid',
                    FALSE,
                    FALSE,
                    CAST(:extras AS jsonb),
                    NOW(),
                    NOW()
                )
                RETURNING id::text AS id
                """
            ),
            {
                "full_name": person.get("full_name"),
                "identifier": identifier,
                "company": person.get("company"),
                "gender": person.get("gender"),
                "role": person.get("role"),
                "country": person.get("country"),
                "state": person.get("state"),
                "city": person.get("city"),
                "birthdate": person.get("birthdate"),
                "extras": json.dumps(
                    person.get("extras") if isinstance(person.get("extras"), dict) else {},
                    ensure_ascii=False,
                ),
            },
        )
        draft_id = str(draft_result.mappings().one()["id"])
        await db_session.execute(
            text(
                """
                INSERT INTO source_list_contact_drafts (source_list_id, contact_draft_id)
                VALUES (:source_list_id, CAST(:draft_id AS uuid))
                """
            ),
            {"source_list_id": source_list_id, "draft_id": draft_id},
        )
    else:
        draft_id = str(existing["id"])
        await db_session.execute(
            text(
                """
                UPDATE contact_drafts
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
                    validation_status = 'valid',
                    updated_at = NOW()
                WHERE id = CAST(:draft_id AS uuid)
                """
            ),
            {
                "draft_id": draft_id,
                "full_name": person.get("full_name"),
                "company": person.get("company"),
                "gender": person.get("gender"),
                "role": person.get("role"),
                "country": person.get("country"),
                "state": person.get("state"),
                "city": person.get("city"),
                "birthdate": person.get("birthdate"),
                "extras": json.dumps(
                    person.get("extras") if isinstance(person.get("extras"), dict) else {},
                    ensure_ascii=False,
                ),
            },
        )

    for index, channel in enumerate(channels):
        await db_session.execute(
            text(
                """
                INSERT INTO contact_draft_channels (
                    contact_draft_id,
                    type,
                    value,
                    dialer_label,
                    is_primary,
                    is_valid,
                    is_reachable,
                    used_for_promotion,
                    created_at,
                    updated_at
                ) VALUES (
                    CAST(:draft_id AS uuid),
                    :channel_type,
                    :channel_value,
                    :channel_label,
                    :is_primary,
                    TRUE,
                    TRUE,
                    FALSE,
                    NOW(),
                    NOW()
                )
                ON CONFLICT (contact_draft_id, type, value) DO UPDATE SET
                    dialer_label = EXCLUDED.dialer_label,
                    is_primary = EXCLUDED.is_primary,
                    is_valid = TRUE,
                    is_reachable = TRUE,
                    updated_at = NOW()
                """
            ),
            {
                "draft_id": draft_id,
                "channel_type": str(channel.get("type")).strip().lower(),
                "channel_value": str(channel.get("value")).strip(),
                "channel_label": str(channel.get("label") or "identidade")[:60],
                "is_primary": bool(channel.get("is_primary")) or index == 0,
            },
        )

    if created:
        await db_session.execute(
            text(
                """
                UPDATE source_lists
                SET
                    rows_total = COALESCE(rows_total, 0) + 1,
                    rows_processed = COALESCE(rows_processed, 0) + 1,
                    rows_without_channel = COALESCE(rows_without_channel, 0) + :without_channel,
                    updated_at = NOW()
                WHERE id = :source_list_id
                """
            ),
            {"source_list_id": source_list_id, "without_channel": 0 if channels else 1},
        )

    await db_session.execute(
        text(
            """
            UPDATE persons
            SET
                last_contact_draft_id = CAST(:draft_id AS uuid),
                last_source_list_id = :source_list_id,
                last_mailing_id = :source_list_id,
                last_seen_at = NOW(),
                updated_at = NOW()
            WHERE uuid = CAST(:person_uuid AS uuid)
            """
        ),
        {
            "draft_id": draft_id,
            "source_list_id": source_list_id,
            "person_uuid": person.get("uuid"),
        },
    )
    return {
        "source_list_id": source_list_id,
        "contact_draft_id": draft_id,
        "created": created,
        "channels": len(channels),
    }


async def fetch_active_flow_mailing_link(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    source_list_id: int,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            """
            SELECT
                id::text AS id,
                flow_id::text AS flow_id,
                mailing_id,
                contact_list_id::text AS contact_list_id,
                linked_at
            FROM flow_mailing_links
            WHERE flow_id = CAST(:flow_uuid AS uuid)
              AND mailing_id = :source_list_id
              AND unlinked_at IS NULL
            LIMIT 1
            """
        ),
        {"flow_uuid": flow_uuid, "source_list_id": source_list_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None
