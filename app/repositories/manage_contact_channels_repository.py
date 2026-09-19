from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def fetch_manage_contact_channels_person_for_update(
    db_session: AsyncSession,
    *,
    person_uuid: str,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            """
            SELECT
                id,
                uuid::text AS uuid,
                identifier,
                primary_channel_type,
                primary_channel_value,
                primary_channel_label,
                channels
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


async def lock_manage_contact_channels_primary_projection(
    db_session: AsyncSession,
    *,
    channel_type: str,
    channel_value: str,
) -> None:
    """Serialize assignment of the legacy globally-unique primary projection."""

    projection_key = f"persons-primary-channel:{channel_type}:{channel_value}"
    await db_session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:projection_key))"),
        {"projection_key": projection_key},
    )


async def manage_contact_channels_primary_projection_is_available(
    db_session: AsyncSession,
    *,
    person_uuid: str,
    channel_type: str,
    channel_value: str,
) -> bool:
    result = await db_session.execute(
        text(
            """
            SELECT NOT EXISTS (
                SELECT 1
                FROM persons
                WHERE primary_channel_type = :channel_type
                  AND primary_channel_value = :channel_value
                  AND uuid <> CAST(:person_uuid AS uuid)
                  AND merged_into_uuid IS NULL
            ) AS available
            """
        ),
        {
            "person_uuid": person_uuid,
            "channel_type": channel_type,
            "channel_value": channel_value,
        },
    )
    return bool(result.scalar_one())


async def update_manage_contact_channels_person(
    db_session: AsyncSession,
    *,
    person_uuid: str,
    channels: list[dict[str, Any]],
    primary_channel: dict[str, Any] | None,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            """
            UPDATE persons
            SET
                channels = CAST(:channels AS jsonb),
                primary_channel_type = :primary_channel_type,
                primary_channel_value = :primary_channel_value,
                primary_channel_label = :primary_channel_label,
                last_seen_at = NOW(),
                updated_at = NOW()
            WHERE uuid = CAST(:person_uuid AS uuid)
              AND merged_into_uuid IS NULL
            RETURNING
                id,
                uuid::text AS uuid,
                identifier,
                primary_channel_type,
                primary_channel_value,
                primary_channel_label,
                channels
            """
        ),
        {
            "person_uuid": person_uuid,
            "channels": json.dumps(channels, ensure_ascii=False),
            "primary_channel_type": (
                primary_channel.get("type") if primary_channel is not None else None
            ),
            "primary_channel_value": (
                primary_channel.get("value") if primary_channel is not None else None
            ),
            "primary_channel_label": (
                primary_channel.get("label") if primary_channel is not None else None
            ),
        },
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None
