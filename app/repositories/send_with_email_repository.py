from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def assign_email_routing_for_session(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    session_id: int,
    contact_list_member_id: int,
    contact_list_id: str,
    mailing_id: int,
    person_uuid: str | None,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            """
            WITH eligible AS (
                SELECT
                    clm.id,
                    CAST(clm.linked_actuator AS text) AS previous_linked_actuator
                FROM contact_list_members clm
                JOIN orch_sessions os
                  ON os.id = :session_id
                 AND os.flow_uuid = CAST(:flow_uuid AS uuid)
                 AND os.state <> 3
                 AND os.ended_at IS NULL
                 AND os.unassigned_at IS NULL
                 AND os.entity = clm.contact_identifier
                 AND BTRIM(os.entity_address) = BTRIM(clm.contact_channel_address)
                WHERE clm.id = :contact_list_member_id
                  AND clm.contact_list_id = CAST(:contact_list_id AS uuid)
                  AND clm.mailing_id = CAST(:mailing_id AS bigint)
                  AND clm.unassigned_at IS NULL
                  AND clm.contact_channel_address IS NOT NULL
                  AND BTRIM(clm.contact_channel_address) <> ''
                  AND LOWER(BTRIM(COALESCE(clm.contact_channel_type, ''))) = 'email'
                  AND (
                        CAST(:person_uuid AS uuid) IS NULL
                        OR clm.person_uuid = CAST(:person_uuid AS uuid)
                      )
                FOR UPDATE OF clm, os
            ), updated AS (
                UPDATE contact_list_members clm
                SET
                    linked_actuator = 'email',
                    updated_at = NOW()
                FROM eligible
                WHERE clm.id = eligible.id
                  AND clm.unassigned_at IS NULL
                RETURNING
                    clm.id,
                    CAST(clm.linked_actuator AS text) AS linked_actuator
            )
            SELECT
                updated.id,
                updated.linked_actuator,
                eligible.previous_linked_actuator
            FROM updated
            JOIN eligible ON eligible.id = updated.id
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
    row = result.mappings().first()
    if row is None:
        return None

    previous_linked_actuator = str(row["previous_linked_actuator"] or "").strip().lower()
    return {
        "contact_list_member_id": int(row["id"]),
        "linked_actuator": str(row["linked_actuator"]),
        "mode": "already_marked" if previous_linked_actuator == "email" else "marked",
    }
