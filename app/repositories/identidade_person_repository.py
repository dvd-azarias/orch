from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


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


async def fetch_person_by_uuid_for_update(
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
            "birthdate": _birthdate_db_value(payload.get("birthdate")),
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
            "birthdate": _birthdate_db_value(payload.get("birthdate")),
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
                "birthdate": _birthdate_db_value(person.get("birthdate")),
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
                "birthdate": _birthdate_db_value(person.get("birthdate")),
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


async def set_person_materialized_membership_state(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    current_session_id: int,
    source_list_id: int,
    contact_list_id: str,
    person_uuid: str,
    contact_draft_id: str | None,
    identifier: str | None,
    desired_state: str,
) -> dict[str, Any]:
    matched_result = await db_session.execute(
        text(
            """
            SELECT id, unassigned_at
            FROM contact_list_members
            WHERE contact_list_id = CAST(:contact_list_id AS uuid)
              AND mailing_id = :source_list_id
              AND deleted_at IS NULL
              AND (
                    person_uuid = CAST(:person_uuid AS uuid)
                    OR (
                        CAST(:contact_draft_id AS uuid) IS NOT NULL
                        AND contact_draft_id = CAST(:contact_draft_id AS uuid)
                    )
                    OR (
                        CAST(:identifier AS text) IS NOT NULL
                        AND contact_identifier = CAST(:identifier AS text)
                    )
                  )
            ORDER BY id
            FOR UPDATE
            """
        ),
        {
            "contact_list_id": contact_list_id,
            "source_list_id": source_list_id,
            "person_uuid": person_uuid,
            "contact_draft_id": contact_draft_id,
            "identifier": identifier,
        },
    )
    matched = [dict(row) for row in matched_result.mappings().all()]
    member_ids = [int(row["id"]) for row in matched]
    active_before = sum(1 for row in matched if row.get("unassigned_at") is None)
    inactive_before = len(matched) - active_before

    if active_before and inactive_before:
        previous_state = "mixed"
    elif active_before:
        previous_state = "active"
    elif inactive_before:
        previous_state = "inactive"
    else:
        previous_state = "absent"

    changed_member_ids: list[int] = []
    if member_ids:
        if desired_state == "active":
            update_result = await db_session.execute(
                text(
                    """
                    UPDATE contact_list_members
                    SET
                        status = 0,
                        last_ref_id = NULL,
                        linked_actuator = NULL,
                        ani = NULL,
                        next_candidate = 0,
                        failure_attempts = 0,
                        busy_attempts = 0,
                        noanswer_attempts = 0,
                        machine_attempts = 0,
                        rejected_attempts = 0,
                        invalidnumber_attempts = 0,
                        external_identifier = NULL,
                        scheduling_moment = NULL,
                        segment = NULL,
                        unassigned_at = NULL,
                        updated_at = NOW()
                    WHERE id = ANY(CAST(:member_ids AS bigint[]))
                      AND unassigned_at IS NOT NULL
                    RETURNING id
                    """
                ),
                {"member_ids": member_ids},
            )
        else:
            update_result = await db_session.execute(
                text(
                    """
                    UPDATE contact_list_members
                    SET
                        status = 0,
                        last_ref_id = NULL,
                        linked_actuator = NULL,
                        ani = NULL,
                        next_candidate = 0,
                        failure_attempts = 0,
                        busy_attempts = 0,
                        noanswer_attempts = 0,
                        machine_attempts = 0,
                        rejected_attempts = 0,
                        invalidnumber_attempts = 0,
                        external_identifier = NULL,
                        scheduling_moment = NULL,
                        segment = NULL,
                        unassigned_at = COALESCE(unassigned_at, NOW()),
                        updated_at = NOW()
                    WHERE id = ANY(CAST(:member_ids AS bigint[]))
                      AND (
                            unassigned_at IS NULL
                            OR status IS DISTINCT FROM 0
                            OR last_ref_id IS NOT NULL
                            OR linked_actuator IS NOT NULL
                            OR ani IS NOT NULL
                            OR next_candidate IS DISTINCT FROM 0
                            OR failure_attempts IS DISTINCT FROM 0
                            OR busy_attempts IS DISTINCT FROM 0
                            OR noanswer_attempts IS DISTINCT FROM 0
                            OR machine_attempts IS DISTINCT FROM 0
                            OR rejected_attempts IS DISTINCT FROM 0
                            OR invalidnumber_attempts IS DISTINCT FROM 0
                            OR external_identifier IS NOT NULL
                            OR scheduling_moment IS NOT NULL
                            OR segment IS NOT NULL
                          )
                    RETURNING id
                    """
                ),
                {"member_ids": member_ids},
            )
        changed_member_ids = [int(row["id"]) for row in update_result.mappings().all()]

    stopped_session_ids: list[int] = []
    if desired_state == "inactive" and member_ids:
        stopped_result = await db_session.execute(
            text(
                """
                UPDATE orch_sessions
                SET
                    state = 5,
                    unassigned_at = NOW(),
                    ended_at = COALESCE(ended_at, NOW()),
                    updated_at = NOW()
                WHERE flow_uuid = CAST(:flow_uuid AS uuid)
                  AND id <> :current_session_id
                  AND unassigned_at IS NULL
                  AND ended_at IS NULL
                  AND state NOT IN (3, 5)
                  AND runtime_variables #>> '{input_payload,contact_list_id}' = :contact_list_id
                  AND runtime_variables #>> '{input_payload,mailing_id}' = :source_list_id_text
                  AND (
                        COALESCE(
                            runtime_variables #>> '{session_identity,contact_list_member_id}',
                            runtime_variables #>> '{input_payload,contact_list_member_id}'
                        ) = ANY(CAST(:member_ids AS text[]))
                        OR (
                            CAST(:identifier AS text) IS NOT NULL
                            AND entity = CAST(:identifier AS text)
                        )
                      )
                RETURNING id
                """
            ),
            {
                "flow_uuid": flow_uuid,
                "current_session_id": current_session_id,
                "contact_list_id": contact_list_id,
                "source_list_id_text": str(source_list_id),
                "member_ids": [str(member_id) for member_id in member_ids],
                "identifier": identifier,
            },
        )
        stopped_session_ids = [int(row["id"]) for row in stopped_result.mappings().all()]

    return {
        "contact_list_id": contact_list_id,
        "previous_state": previous_state,
        "matched_members": len(member_ids),
        "members_changed": len(changed_member_ids),
        "sessions_stopped": len(stopped_session_ids),
    }
