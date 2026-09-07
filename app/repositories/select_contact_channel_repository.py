from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _channel_type_sql(column: str) -> str:
    return (
        "CASE "
        f"WHEN LOWER(BTRIM(COALESCE({column}, ''))) IN ('phone', 'voice') THEN 'voice' "
        f"ELSE LOWER(BTRIM(COALESCE({column}, ''))) "
        "END"
    )


async def fetch_select_contact_channel_candidate(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    session_id: int,
    session_scope: str,
    contact_list_member_id: int,
    contact_list_id: str,
    mailing_id: int,
    person_uuid: str | None,
    channel_type: str,
    channel_label: str | None,
) -> dict[str, Any] | None:
    parameters: dict[str, Any] = {
        "flow_uuid": flow_uuid,
        "session_id": session_id,
        "contact_list_member_id": contact_list_member_id,
        "contact_list_id": contact_list_id,
        "mailing_id": mailing_id,
        "person_uuid": person_uuid,
        "channel_type": channel_type,
        "channel_label": channel_label,
    }
    scope_filter = ""
    if session_scope == "channel":
        scope_filter = """
              AND clm.id = :contact_list_member_id
              AND BTRIM(clm.contact_channel_address) = BTRIM(os.entity_address)
        """
    else:
        scope_filter = """
              AND (
                    CAST(:person_uuid AS uuid) IS NULL
                    OR clm.person_uuid = CAST(:person_uuid AS uuid)
                  )
        """

    normalized_type_sql = _channel_type_sql("clm.contact_channel_type")
    result = await db_session.execute(
        text(
            f"""
            SELECT
                clm.id AS contact_list_member_id,
                clm.contact_list_id::text AS contact_list_id,
                clm.mailing_id::text AS mailing_id,
                clm.contact_identifier,
                clm.contact_name,
                clm.contact_full_name,
                clm.contact_gender,
                clm.contact_country,
                clm.contact_province,
                clm.contact_city,
                clm.contact_birth_date,
                clm.contact_age,
                {normalized_type_sql} AS contact_channel_type,
                clm.contact_channel_label,
                BTRIM(clm.contact_channel_address) AS contact_channel_address,
                clm.contact_channel_extra_data,
                clm.person_uuid::text AS person_uuid,
                COALESCE(source_channel.is_primary, false) AS is_primary
            FROM contact_list_members clm
            JOIN orch_sessions os
              ON os.id = :session_id
             AND os.flow_uuid = CAST(:flow_uuid AS uuid)
             AND os.state <> 3
             AND os.unassigned_at IS NULL
             AND os.entity = clm.contact_identifier
            LEFT JOIN contact_draft_channels source_channel
              ON source_channel.id = clm.contact_channel_id
            WHERE clm.contact_list_id = CAST(:contact_list_id AS uuid)
              AND clm.mailing_id = CAST(:mailing_id AS bigint)
              AND clm.unassigned_at IS NULL
              AND clm.contact_channel_address IS NOT NULL
              AND BTRIM(clm.contact_channel_address) <> ''
              AND {normalized_type_sql} = :channel_type
              AND (
                    CAST(:channel_label AS text) IS NULL
                    OR BTRIM(clm.contact_channel_label) = CAST(:channel_label AS text)
                  )
              {scope_filter}
            ORDER BY COALESCE(source_channel.is_primary, false) DESC, clm.id ASC
            LIMIT 1
            FOR UPDATE OF clm, os
            """
        ),
        parameters,
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def rebind_person_session_to_contact_channel(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    session_id: int,
    contact_list_member_id: int,
    contact_list_id: str,
    mailing_id: int,
    person_uuid: str | None,
) -> bool:
    result = await db_session.execute(
        text(
            """
            UPDATE orch_sessions os
            SET
                entity_address = BTRIM(clm.contact_channel_address),
                updated_at = NOW()
            FROM contact_list_members clm
            WHERE os.id = :session_id
              AND os.flow_uuid = CAST(:flow_uuid AS uuid)
              AND os.state <> 3
              AND os.unassigned_at IS NULL
              AND clm.id = :contact_list_member_id
              AND clm.contact_list_id = CAST(:contact_list_id AS uuid)
              AND clm.mailing_id = CAST(:mailing_id AS bigint)
              AND clm.unassigned_at IS NULL
              AND clm.contact_identifier = os.entity
              AND clm.contact_channel_address IS NOT NULL
              AND BTRIM(clm.contact_channel_address) <> ''
              AND (
                    CAST(:person_uuid AS uuid) IS NULL
                    OR clm.person_uuid = CAST(:person_uuid AS uuid)
                  )
              AND NOT EXISTS (
                    SELECT 1
                    FROM orch_sessions conflicting
                    WHERE conflicting.id <> os.id
                      AND conflicting.flow_uuid = os.flow_uuid
                      AND conflicting.entity = os.entity
                      AND conflicting.entity_type = os.entity_type
                      AND BTRIM(conflicting.entity_address) = BTRIM(clm.contact_channel_address)
                      AND conflicting.state <> 3
                      AND conflicting.unassigned_at IS NULL
                  )
            RETURNING os.id
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "session_id": session_id,
            "contact_list_member_id": contact_list_member_id,
            "contact_list_id": contact_list_id,
            "mailing_id": mailing_id,
            "person_uuid": person_uuid,
        },
    )
    return result.scalar_one_or_none() is not None
