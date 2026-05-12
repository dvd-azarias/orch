from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class MappingItem:
    header_name: str
    field_code: str
    ignored: bool


class SourceListStatus:
    PENDING_FIELD_MAPPING = "PENDING_FIELD_MAPPING"
    READY_TO_INGEST = "READY_TO_INGEST"
    ERROR = "ERROR"


async def resolve_mapping_template_uuid(
    db_session: AsyncSession,
    *,
    workspace_schema: str,
    flow_uuid: str,
    payload: dict[str, Any],
) -> str | None:
    file_data = payload.get("file")
    candidates: list[str] = []
    if isinstance(file_data, dict):
        candidates.append(str(file_data.get("mapping_template_id", "")).strip())
    candidates.append(str(payload.get("mapping_template_id", "")).strip())

    safe_schema = workspace_schema.replace('"', '""')
    row = await db_session.execute(
        text(
            f"""
            SELECT COALESCE(
                cr.definition -> 'canvas_properties' -> 'orchestration_trigger' ->> 'mapping_template_id',
                dr.definition -> 'canvas_properties' -> 'orchestration_trigger' ->> 'mapping_template_id',
                ''
            ) AS mapping_template_id
            FROM "{safe_schema}".flow_v2 f
            LEFT JOIN "{safe_schema}".flow_v2_revision cr ON cr.id = f.current_revision_id
            LEFT JOIN "{safe_schema}".flow_v2_revision dr ON dr.id = f.draft_revision_id
            WHERE f.id::text = :flow_uuid
            LIMIT 1
            """
        ),
        {"flow_uuid": flow_uuid},
    )
    found = row.first()
    if found is not None:
        candidates.append(str(found[0] or "").strip())

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return str(UUID(candidate))
        except ValueError:
            continue
    return None


async def resolve_mapping_template_id(
    db_session: AsyncSession,
    *,
    workspace_schema: str,
    mapping_template_uuid: str,
) -> int | None:
    safe_schema = workspace_schema.replace('"', '""')
    result = await db_session.execute(
        text(
            f"""
            SELECT id
            FROM "{safe_schema}".source_list_mapping_templates
            WHERE uuid::text = :mapping_template_uuid
            LIMIT 1
            """
        ),
        {"mapping_template_uuid": mapping_template_uuid},
    )
    row = result.first()
    if row is None:
        return None
    return int(row[0])


async def resolve_mailing_public_id_from_template(
    db_session: AsyncSession,
    *,
    workspace_schema: str,
    template_id: int,
) -> str | None:
    safe_schema = workspace_schema.replace('"', '""')
    result = await db_session.execute(
        text(
            f"""
            SELECT sl.public_id::text
            FROM "{safe_schema}".source_list_mapping_templates t
            JOIN "{safe_schema}".source_lists sl
              ON sl.id = t.created_from_source_list_id
            WHERE t.id = :template_id
            LIMIT 1
            """
        ),
        {"template_id": template_id},
    )
    value = result.scalar_one_or_none()
    if value is None:
        return None
    mailing_uuid = str(value).strip()
    return mailing_uuid or None


async def create_source_list_for_file_event(
    db_session: AsyncSession,
    *,
    workspace_schema: str,
    file_data: dict[str, Any],
    template_id: int,
    rows_total: int,
) -> tuple[int, str]:
    safe_schema = workspace_schema.replace('"', '""')
    file_name = str(file_data.get("original_name") or "").strip() or None
    file_path = str(file_data.get("folder_path") or "").strip() or None
    file_url = str(file_data.get("url") or "").strip() or None
    file_id = str(file_data.get("id") or "").strip() or "file_event"
    name = file_name or f"file_event_{file_id[:8]}"
    description = f"Auto source_list from file_event ({file_id})"
    row = (
        await db_session.execute(
            text(
                f"""
                INSERT INTO "{safe_schema}".source_lists (
                    name,
                    description,
                    status,
                    origin,
                    file_name,
                    file_path,
                    file_url,
                    rows_total,
                    rows_processed,
                    rows_discarded,
                    rows_error,
                    rows_without_channel,
                    mapping_template_id,
                    mapping_completed_at,
                    ingested_at,
                    updated_at
                ) VALUES (
                    :name,
                    :description,
                    'PENDING_FIELD_MAPPING',
                    'api',
                    :file_name,
                    :file_path,
                    :file_url,
                    :rows_total,
                    0,
                    0,
                    0,
                    0,
                    :mapping_template_id,
                    NULL,
                    NULL,
                    NOW()
                )
                RETURNING id, public_id::text
                """
            ),
            {
                "name": name,
                "description": description,
                "file_name": file_name,
                "file_path": file_path,
                "file_url": file_url,
                "rows_total": max(0, int(rows_total)),
                "mapping_template_id": template_id,
            },
        )
    ).first()
    if row is None:
        raise RuntimeError("Falha ao criar source_list para file_event.")
    return int(row[0]), str(row[1])


async def update_source_list_after_ingest(
    db_session: AsyncSession,
    *,
    workspace_schema: str,
    source_list_id: int,
    status: str,
    rows_processed: int,
    rows_discarded: int,
    rows_error: int,
    rows_without_channel: int,
) -> None:
    safe_schema = workspace_schema.replace('"', '""')
    now_utc = datetime.now(timezone.utc)
    await db_session.execute(
        text(
            f"""
            UPDATE "{safe_schema}".source_lists
            SET
                status = :status,
                rows_processed = :rows_processed,
                rows_discarded = :rows_discarded,
                rows_error = :rows_error,
                rows_without_channel = :rows_without_channel,
                mapping_completed_at = :mapping_completed_at,
                ingested_at = NULL,
                updated_at = NOW()
            WHERE id = :source_list_id
            """
        ),
        {
            "source_list_id": int(source_list_id),
            "status": status,
            "rows_processed": max(0, int(rows_processed)),
            "rows_discarded": max(0, int(rows_discarded)),
            "rows_error": max(0, int(rows_error)),
            "rows_without_channel": max(0, int(rows_without_channel)),
            "mapping_completed_at": now_utc,
        },
    )


async def load_mapping_items(
    db_session: AsyncSession,
    *,
    workspace_schema: str,
    template_id: int,
) -> list[MappingItem]:
    safe_schema = workspace_schema.replace('"', '""')
    result = await db_session.execute(
        text(
            f"""
            SELECT
                i.header_name,
                COALESCE(csf.code, '') AS field_code,
                COALESCE(i.is_ignored, FALSE) AS is_ignored
            FROM "{safe_schema}".source_list_mapping_template_items i
            LEFT JOIN target.contact_system_fields csf
              ON csf.id = i.contact_system_field_id
            WHERE i.template_id = :template_id
            ORDER BY i.id
            """
        ),
        {"template_id": template_id},
    )
    items: list[MappingItem] = []
    for row in result.fetchall():
        header_name = str(row[0] or "").strip()
        field_code = str(row[1] or "").strip().upper()
        ignored = bool(row[2])
        if not header_name:
            continue
        items.append(MappingItem(header_name=header_name, field_code=field_code, ignored=ignored))
    return items


def _normalize_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _map_row_to_person_payload(row: dict[str, Any], mapping_items: list[MappingItem]) -> dict[str, Any] | None:
    identifier = ""
    full_name = ""
    channels: list[dict[str, str]] = []
    extras: dict[str, Any] = {}

    for item in mapping_items:
        if item.ignored:
            continue
        value = _normalize_value(row.get(item.header_name))
        if not value:
            continue
        code = item.field_code
        if code == "CONTACT_IDENTIFIER":
            identifier = value
        elif code == "CONTACT_FULL_NAME":
            full_name = value
        elif code == "CHANNEL_PHONE":
            channels.append({"type": "phone", "value": value, "label": item.header_name})
        elif code == "CHANNEL_WHATSAPP":
            channels.append({"type": "whatsapp", "value": value, "label": item.header_name})
        elif code == "CHANNEL_EMAIL":
            channels.append({"type": "email", "value": value, "label": item.header_name})
        elif code == "EXTRA_GENERIC":
            extras[item.header_name] = value

    if not identifier:
        return None

    primary_channel_type = channels[0]["type"] if channels else None
    primary_channel_value = channels[0]["value"] if channels else None
    primary_channel_label = channels[0]["label"] if channels else None
    return {
        "identifier": identifier,
        "full_name": full_name or None,
        "primary_channel_type": primary_channel_type,
        "primary_channel_value": primary_channel_value,
        "primary_channel_label": primary_channel_label,
        "channels": channels,
        "extras": extras,
    }


def count_rows_without_channel(
    *,
    rows: list[dict[str, Any]],
    mapping_items: list[MappingItem],
) -> int:
    count = 0
    for row in rows:
        mapped = _map_row_to_person_payload(row, mapping_items)
        if mapped is None:
            continue
        if mapped.get("primary_channel_type") is None:
            count += 1
    return count


async def upsert_persons_from_rows(
    db_session: AsyncSession,
    *,
    workspace_schema: str,
    rows: list[dict[str, Any]],
    mapping_items: list[MappingItem],
    source_list_id: int | None = None,
) -> tuple[int, int]:
    safe_schema = workspace_schema.replace('"', '""')
    inserted_or_updated = 0
    skipped = 0
    for row in rows:
        mapped = _map_row_to_person_payload(row, mapping_items)
        if mapped is None:
            skipped += 1
            continue
        await db_session.execute(
            text(
                f"""
                INSERT INTO "{safe_schema}".persons (
                    identifier,
                    full_name,
                    primary_channel_type,
                    primary_channel_value,
                    primary_channel_label,
                    channels,
                    extras,
                    last_source_list_id,
                    last_mailing_id,
                    last_seen_at,
                    updated_at
                ) VALUES (
                    :identifier,
                    :full_name,
                    :primary_channel_type,
                    :primary_channel_value,
                    :primary_channel_label,
                    CAST(:channels AS jsonb),
                    CAST(:extras AS jsonb),
                    :source_list_id,
                    :source_list_id,
                    NOW(),
                    NOW()
                )
                ON CONFLICT (identifier) DO UPDATE SET
                    full_name = EXCLUDED.full_name,
                    primary_channel_type = EXCLUDED.primary_channel_type,
                    primary_channel_value = EXCLUDED.primary_channel_value,
                    primary_channel_label = EXCLUDED.primary_channel_label,
                    channels = EXCLUDED.channels,
                    extras = COALESCE("{safe_schema}".persons.extras, '{{}}'::jsonb) || EXCLUDED.extras,
                    last_source_list_id = COALESCE(EXCLUDED.last_source_list_id, "{safe_schema}".persons.last_source_list_id),
                    last_mailing_id = COALESCE(EXCLUDED.last_mailing_id, "{safe_schema}".persons.last_mailing_id),
                    last_seen_at = NOW(),
                    updated_at = NOW()
                """
            ),
            {
                "identifier": mapped["identifier"],
                "full_name": mapped["full_name"],
                "primary_channel_type": mapped["primary_channel_type"],
                "primary_channel_value": mapped["primary_channel_value"],
                "primary_channel_label": mapped["primary_channel_label"],
                "channels": json.dumps(mapped["channels"], ensure_ascii=False),
                "extras": json.dumps(mapped["extras"], ensure_ascii=False),
                "source_list_id": source_list_id,
            },
        )
        inserted_or_updated += 1
    return inserted_or_updated, skipped


async def resolve_person_ids_for_rows(
    db_session: AsyncSession,
    *,
    workspace_schema: str,
    rows: list[dict[str, Any]],
    mapping_items: list[MappingItem],
) -> list[str | None]:
    safe_schema = workspace_schema.replace('"', '""')
    identifiers_by_index: list[str | None] = []
    unique_identifiers: set[str] = set()

    for row in rows:
        mapped = _map_row_to_person_payload(row, mapping_items)
        identifier = str(mapped.get("identifier")).strip() if mapped else ""
        if identifier:
            identifiers_by_index.append(identifier)
            unique_identifiers.add(identifier)
        else:
            identifiers_by_index.append(None)

    if not unique_identifiers:
        return [None for _ in rows]

    result = await db_session.execute(
        text(
            f"""
            SELECT identifier, id
            FROM "{safe_schema}".persons
            WHERE identifier = ANY(CAST(:identifiers AS text[]))
            """
        ),
        {"identifiers": list(unique_identifiers)},
    )
    person_id_by_identifier = {str(row[0]): str(row[1]) for row in result.fetchall()}
    return [person_id_by_identifier.get(identifier) if identifier else None for identifier in identifiers_by_index]
