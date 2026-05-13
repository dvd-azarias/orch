from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SESSION_STATE_FINISHED = 3
SESSION_STATE_STOPPED_AFTER_UNASSIGN = 5


@dataclass(frozen=True)
class PersistResult:
    id: int
    uuid: str
    state: int
    created: bool


@dataclass(frozen=True)
class WhatsappStatusTimestamps:
    whatsapp_sent_at: datetime | None
    whatsapp_delivered_at: datetime | None
    whatsapp_read_at: datetime | None
    whatsapp_failed_at: datetime | None


@dataclass(frozen=True)
class DialerStatusTimestamps:
    dialer_answered_at: datetime | None
    dialer_busy_at: datetime | None
    dialer_rejected_at: datetime | None
    dialer_invalid_number_at: datetime | None
    dialer_not_answered_at: datetime | None
    dialer_failed_at: datetime | None


@dataclass(frozen=True)
class SessionStateUpdate:
    state: int
    ended_at: datetime | None


SESSION_SUMMARY_SELECT = """
id,
uuid::text AS uuid,
flow_uuid::text AS flow_uuid,
state,
entity_origin_app,
entity,
entity_type,
entity_address,
entity_session_id,
started_at,
ended_at,
created_at,
updated_at
"""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_runtime_patch(
    *,
    app_name: str,
    payload: dict[str, Any],
    extracted: dict[str, Any],
) -> str:
    runtime_patch = {
        "source_app": app_name,
        "last_event_received_at": _now_utc_iso(),
        "last_payload": payload,
        "last_extracted": extracted,
    }
    return json.dumps(runtime_patch, ensure_ascii=False)


def _parse_unix_timestamp(raw_value: Any) -> datetime | None:
    if raw_value is None:
        return None

    try:
        parsed = float(str(raw_value).strip())
    except (TypeError, ValueError):
        return None

    return datetime.fromtimestamp(parsed, tz=timezone.utc)


def _parse_datetime(raw_value: Any) -> datetime | None:
    if raw_value is None:
        return None

    text_value = str(raw_value).strip()
    if not text_value:
        return None

    parsed_iso = _parse_unix_timestamp(text_value)
    if parsed_iso is not None:
        return parsed_iso

    try:
        parsed = datetime.strptime(text_value, "%Y-%m-%d %H:%M:%S")
        return parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_int_code(raw_value: Any) -> int | None:
    if raw_value is None:
        return None

    text_value = str(raw_value).strip()
    if not text_value:
        return None
    if text_value.isdigit() or (text_value.startswith("-") and text_value[1:].isdigit()):
        try:
            return int(text_value)
        except ValueError:
            return None
    return None


def _extract_whatsapp_status_timestamps(payload: dict[str, Any]) -> WhatsappStatusTimestamps:
    if payload.get("object") != "whatsapp_business_account":
        return WhatsappStatusTimestamps(None, None, None, None)

    entries = payload.get("entry")
    if not isinstance(entries, list):
        return WhatsappStatusTimestamps(None, None, None, None)

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            statuses = value.get("statuses")
            if not isinstance(statuses, list):
                continue
            for status_item in statuses:
                if not isinstance(status_item, dict):
                    continue
                status_name = str(status_item.get("status", "")).strip().lower()
                event_at = _parse_unix_timestamp(status_item.get("timestamp"))
                if event_at is None:
                    continue

                if status_name == "sent":
                    return WhatsappStatusTimestamps(event_at, None, None, None)
                if status_name == "delivered":
                    return WhatsappStatusTimestamps(None, event_at, None, None)
                if status_name == "read":
                    return WhatsappStatusTimestamps(None, None, event_at, None)
                if status_name == "failed":
                    return WhatsappStatusTimestamps(None, None, None, event_at)

    return WhatsappStatusTimestamps(None, None, None, None)


def _extract_dialer_status_timestamps(payload: dict[str, Any]) -> DialerStatusTimestamps:
    hangup = payload.get("hangup")
    if not isinstance(hangup, dict):
        return DialerStatusTimestamps(None, None, None, None, None, None)

    event_at = (
        _parse_datetime(hangup.get("EndTime"))
        or _parse_datetime(hangup.get("StartTime"))
        or datetime.now(timezone.utc)
    )

    disposition = str(hangup.get("Disposition", "")).strip().upper()
    classifier = str(hangup.get("DialerClassifierStatus", "")).strip().upper()
    cause_txt = str(hangup.get("Cause-txt", "")).strip().upper()
    hint = " ".join(part for part in [disposition, classifier, cause_txt] if part)
    code = _parse_int_code(hangup.get("DialerHangupCause")) or _parse_int_code(hangup.get("Cause"))

    is_success = "ANSWERED" in hint or code in {16, 200}
    is_busy = "BUSY" in hint or code in {17, 486, 600}
    is_noanswer = any(k in hint for k in ("NO ANSWER", "NOANSWER", "RINGING", "SILENCIO")) or code in {
        18,
        19,
        20,
        31,
        102,
        480,
        408,
        487,
        490,
    }
    is_invalid = any(k in hint for k in ("INVALID", "UNALLOCATED", "NOT FOUND")) or code in {
        1,
        2,
        3,
        22,
        26,
        28,
        404,
        410,
        484,
        604,
    }
    is_rejected = any(k in hint for k in ("REJECT", "FORBIDDEN", "DECLINED")) or code in {
        21,
        55,
        57,
        87,
        401,
        403,
        603,
    }

    if is_success:
        return DialerStatusTimestamps(event_at, None, None, None, None, None)
    if is_busy:
        return DialerStatusTimestamps(None, event_at, None, None, None, None)
    if is_invalid:
        return DialerStatusTimestamps(None, None, None, event_at, None, None)
    if is_rejected:
        return DialerStatusTimestamps(None, None, event_at, None, None, None)
    if is_noanswer:
        return DialerStatusTimestamps(None, None, None, None, event_at, None)
    return DialerStatusTimestamps(None, None, None, None, None, event_at)


def _derive_state_update(
    *,
    app_name: str,
    whatsapp_timestamps: WhatsappStatusTimestamps,
    dialer_timestamps: DialerStatusTimestamps,
) -> SessionStateUpdate:
    if app_name == "DialerApp":
        terminal_event_at = (
            dialer_timestamps.dialer_answered_at
            or dialer_timestamps.dialer_busy_at
            or dialer_timestamps.dialer_rejected_at
            or dialer_timestamps.dialer_invalid_number_at
            or dialer_timestamps.dialer_not_answered_at
            or dialer_timestamps.dialer_failed_at
        )
        if terminal_event_at is not None:
            return SessionStateUpdate(state=3, ended_at=terminal_event_at)
        return SessionStateUpdate(state=1, ended_at=None)

    if app_name == "WhatsApp":
        if whatsapp_timestamps.whatsapp_read_at is not None:
            return SessionStateUpdate(state=3, ended_at=whatsapp_timestamps.whatsapp_read_at)
        if whatsapp_timestamps.whatsapp_failed_at is not None:
            return SessionStateUpdate(state=3, ended_at=whatsapp_timestamps.whatsapp_failed_at)
        if (
            whatsapp_timestamps.whatsapp_sent_at is not None
            or whatsapp_timestamps.whatsapp_delivered_at is not None
        ):
            return SessionStateUpdate(state=2, ended_at=None)
        return SessionStateUpdate(state=1, ended_at=None)

    return SessionStateUpdate(state=0, ended_at=None)


async def upsert_active_session(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    app_name: str,
    entity: str,
    entity_type: str,
    entity_address: str,
    entity_session_id: str,
    payload: dict[str, Any],
    extracted: dict[str, Any],
) -> PersistResult:
    lock_key = f"{flow_uuid}|{entity}|{entity_type}|{entity_address}"
    runtime_patch_json = _build_runtime_patch(
        app_name=app_name,
        payload=payload,
        extracted=extracted,
    )
    whatsapp_timestamps = _extract_whatsapp_status_timestamps(payload)
    dialer_timestamps = _extract_dialer_status_timestamps(payload)
    state_update = _derive_state_update(
        app_name=app_name,
        whatsapp_timestamps=whatsapp_timestamps,
        dialer_timestamps=dialer_timestamps,
    )
    allow_finished_reuse_by_session_id = app_name in {"WhatsApp", "DialerApp"}
    allow_address_reuse_without_entity_match = app_name in {"WhatsApp", "DialerApp"}

    await db_session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": lock_key},
    )

    update_result = await db_session.execute(
        text(
            """
            UPDATE orch_sessions
            SET
                entity_session_id = COALESCE(entity_session_id, :entity_session_id),
                entity_origin_app = COALESCE(entity_origin_app, :entity_origin_app),
                state = GREATEST(state, :state),
                ended_at = COALESCE(:ended_at, ended_at),
                runtime_variables = COALESCE(runtime_variables, '{}'::jsonb) || CAST(:runtime_patch AS jsonb),
                whatsapp_sent_at = COALESCE(:whatsapp_sent_at, whatsapp_sent_at),
                whatsapp_delivered_at = COALESCE(:whatsapp_delivered_at, whatsapp_delivered_at),
                whatsapp_read_at = COALESCE(:whatsapp_read_at, whatsapp_read_at),
                whatsapp_failed_at = COALESCE(:whatsapp_failed_at, whatsapp_failed_at),
                dialer_answered_at = COALESCE(:dialer_answered_at, dialer_answered_at),
                dialer_busy_at = COALESCE(:dialer_busy_at, dialer_busy_at),
                dialer_rejected_at = COALESCE(:dialer_rejected_at, dialer_rejected_at),
                dialer_invalid_number_at = COALESCE(:dialer_invalid_number_at, dialer_invalid_number_at),
                dialer_not_answered_at = COALESCE(:dialer_not_answered_at, dialer_not_answered_at),
                dialer_failed_at = COALESCE(:dialer_failed_at, dialer_failed_at),
                updated_at = NOW()
            WHERE id = (
                SELECT id
                FROM orch_sessions
                WHERE
                    flow_uuid = CAST(:flow_uuid AS uuid)
                    AND entity_type = :entity_type
                    AND entity_address = :entity_address
                    AND (
                        :allow_address_reuse_without_entity_match
                        OR entity = :entity
                    )
                    AND state <> :state_finished
                    AND unassigned_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
            )
            RETURNING id, uuid::text AS uuid, state
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "entity": entity,
            "entity_type": entity_type,
            "entity_address": entity_address,
            "entity_session_id": entity_session_id,
            "entity_origin_app": app_name,
            "state": state_update.state,
            "state_finished": SESSION_STATE_FINISHED,
            "allow_address_reuse_without_entity_match": allow_address_reuse_without_entity_match,
            "ended_at": state_update.ended_at,
            "runtime_patch": runtime_patch_json,
            "whatsapp_sent_at": whatsapp_timestamps.whatsapp_sent_at,
            "whatsapp_delivered_at": whatsapp_timestamps.whatsapp_delivered_at,
            "whatsapp_read_at": whatsapp_timestamps.whatsapp_read_at,
            "whatsapp_failed_at": whatsapp_timestamps.whatsapp_failed_at,
            "dialer_answered_at": dialer_timestamps.dialer_answered_at,
            "dialer_busy_at": dialer_timestamps.dialer_busy_at,
            "dialer_rejected_at": dialer_timestamps.dialer_rejected_at,
            "dialer_invalid_number_at": dialer_timestamps.dialer_invalid_number_at,
            "dialer_not_answered_at": dialer_timestamps.dialer_not_answered_at,
            "dialer_failed_at": dialer_timestamps.dialer_failed_at,
        },
    )

    updated_row = update_result.mappings().first()
    if updated_row is not None:
        return PersistResult(
            id=int(updated_row["id"]),
            uuid=str(updated_row["uuid"]),
            state=int(updated_row["state"]),
            created=False,
        )

    retry_result = await db_session.execute(
        text(
            """
            UPDATE orch_sessions
            SET
                entity_origin_app = COALESCE(entity_origin_app, :entity_origin_app),
                state = GREATEST(state, :state),
                ended_at = COALESCE(:ended_at, ended_at),
                runtime_variables = COALESCE(runtime_variables, '{}'::jsonb) || CAST(:runtime_patch AS jsonb),
                whatsapp_sent_at = COALESCE(:whatsapp_sent_at, whatsapp_sent_at),
                whatsapp_delivered_at = COALESCE(:whatsapp_delivered_at, whatsapp_delivered_at),
                whatsapp_read_at = COALESCE(:whatsapp_read_at, whatsapp_read_at),
                whatsapp_failed_at = COALESCE(:whatsapp_failed_at, whatsapp_failed_at),
                dialer_answered_at = COALESCE(:dialer_answered_at, dialer_answered_at),
                dialer_busy_at = COALESCE(:dialer_busy_at, dialer_busy_at),
                dialer_rejected_at = COALESCE(:dialer_rejected_at, dialer_rejected_at),
                dialer_invalid_number_at = COALESCE(:dialer_invalid_number_at, dialer_invalid_number_at),
                dialer_not_answered_at = COALESCE(:dialer_not_answered_at, dialer_not_answered_at),
                dialer_failed_at = COALESCE(:dialer_failed_at, dialer_failed_at),
                updated_at = NOW()
            WHERE id = (
                SELECT id
                FROM orch_sessions
                WHERE
                    flow_uuid = CAST(:flow_uuid AS uuid)
                    AND entity_type = :entity_type
                    AND entity_address = :entity_address
                    AND (
                        :allow_address_reuse_without_entity_match
                        OR entity = :entity
                    )
                    AND entity_session_id = :entity_session_id
                    AND (
                        :allow_finished_reuse_by_session_id
                        OR state <> :state_finished
                    )
                    AND unassigned_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
            )
            RETURNING id, uuid::text AS uuid, state
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "entity": entity,
            "entity_type": entity_type,
            "entity_address": entity_address,
            "entity_session_id": entity_session_id,
            "allow_finished_reuse_by_session_id": allow_finished_reuse_by_session_id,
            "allow_address_reuse_without_entity_match": allow_address_reuse_without_entity_match,
            "state_finished": SESSION_STATE_FINISHED,
            "entity_origin_app": app_name,
            "state": state_update.state,
            "ended_at": state_update.ended_at,
            "runtime_patch": runtime_patch_json,
            "whatsapp_sent_at": whatsapp_timestamps.whatsapp_sent_at,
            "whatsapp_delivered_at": whatsapp_timestamps.whatsapp_delivered_at,
            "whatsapp_read_at": whatsapp_timestamps.whatsapp_read_at,
            "whatsapp_failed_at": whatsapp_timestamps.whatsapp_failed_at,
            "dialer_answered_at": dialer_timestamps.dialer_answered_at,
            "dialer_busy_at": dialer_timestamps.dialer_busy_at,
            "dialer_rejected_at": dialer_timestamps.dialer_rejected_at,
            "dialer_invalid_number_at": dialer_timestamps.dialer_invalid_number_at,
            "dialer_not_answered_at": dialer_timestamps.dialer_not_answered_at,
            "dialer_failed_at": dialer_timestamps.dialer_failed_at,
        },
    )

    retry_row = retry_result.mappings().first()
    if retry_row is not None:
        return PersistResult(
            id=int(retry_row["id"]),
            uuid=str(retry_row["uuid"]),
            state=int(retry_row["state"]),
            created=False,
        )

    insert_result = await db_session.execute(
        text(
            """
            INSERT INTO orch_sessions (
                flow_uuid,
                state,
                entity_origin_app,
                entity,
                entity_type,
                entity_address,
                entity_session_id,
                started_at,
                ended_at,
                runtime_variables,
                whatsapp_sent_at,
                whatsapp_delivered_at,
                whatsapp_read_at,
                whatsapp_failed_at,
                dialer_answered_at,
                dialer_busy_at,
                dialer_rejected_at,
                dialer_invalid_number_at,
                dialer_not_answered_at,
                dialer_failed_at,
                created_at,
                updated_at
            )
            VALUES (
                CAST(:flow_uuid AS uuid),
                :state,
                :entity_origin_app,
                :entity,
                :entity_type,
                :entity_address,
                :entity_session_id,
                NOW(),
                :ended_at,
                CAST(:runtime_patch AS jsonb),
                :whatsapp_sent_at,
                :whatsapp_delivered_at,
                :whatsapp_read_at,
                :whatsapp_failed_at,
                :dialer_answered_at,
                :dialer_busy_at,
                :dialer_rejected_at,
                :dialer_invalid_number_at,
                :dialer_not_answered_at,
                :dialer_failed_at,
                NOW(),
                NOW()
            )
            RETURNING id, uuid::text AS uuid, state
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "state": state_update.state,
            "ended_at": state_update.ended_at,
            "entity_origin_app": app_name,
            "entity": entity,
            "entity_type": entity_type,
            "entity_address": entity_address,
            "entity_session_id": entity_session_id,
            "runtime_patch": runtime_patch_json,
            "whatsapp_sent_at": whatsapp_timestamps.whatsapp_sent_at,
            "whatsapp_delivered_at": whatsapp_timestamps.whatsapp_delivered_at,
            "whatsapp_read_at": whatsapp_timestamps.whatsapp_read_at,
            "whatsapp_failed_at": whatsapp_timestamps.whatsapp_failed_at,
            "dialer_answered_at": dialer_timestamps.dialer_answered_at,
            "dialer_busy_at": dialer_timestamps.dialer_busy_at,
            "dialer_rejected_at": dialer_timestamps.dialer_rejected_at,
            "dialer_invalid_number_at": dialer_timestamps.dialer_invalid_number_at,
            "dialer_not_answered_at": dialer_timestamps.dialer_not_answered_at,
            "dialer_failed_at": dialer_timestamps.dialer_failed_at,
        },
    )

    inserted_row = insert_result.mappings().one()
    return PersistResult(
        id=int(inserted_row["id"]),
        uuid=str(inserted_row["uuid"]),
        state=int(inserted_row["state"]),
        created=True,
    )


async def fetch_session_by_uuid(
    db_session: AsyncSession,
    *,
    session_uuid: str,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            f"""
            SELECT {SESSION_SUMMARY_SELECT}
            FROM orch_sessions
            WHERE uuid = CAST(:session_uuid AS uuid)
            LIMIT 1
            """
        ),
        {"session_uuid": session_uuid},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def fetch_sessions_by_flow_uuid(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    limit: int,
    cursor_created_at: datetime | None,
    cursor_id: int | None,
) -> list[dict[str, Any]]:
    query = f"""
        SELECT {SESSION_SUMMARY_SELECT}
        FROM orch_sessions
        WHERE flow_uuid = CAST(:flow_uuid AS uuid)
    """
    params: dict[str, Any] = {"flow_uuid": flow_uuid, "limit": limit}

    if cursor_created_at is not None and cursor_id is not None:
        query += " AND (created_at, id) < (:cursor_created_at, :cursor_id)"
        params["cursor_created_at"] = cursor_created_at
        params["cursor_id"] = cursor_id

    query += " ORDER BY created_at DESC, id DESC LIMIT :limit"
    result = await db_session.execute(
        text(query),
        params,
    )
    return [dict(row) for row in result.mappings().all()]


async def fetch_sessions_by_entity(
    db_session: AsyncSession,
    *,
    entity: str,
    entity_type: str | None,
    entity_address: str | None,
    limit: int,
    cursor_created_at: datetime | None,
    cursor_id: int | None,
) -> list[dict[str, Any]]:
    query = f"""
        SELECT {SESSION_SUMMARY_SELECT}
        FROM orch_sessions
        WHERE entity = :entity
    """
    params: dict[str, Any] = {"entity": entity, "limit": limit}

    if entity_type is not None:
        query += " AND entity_type = :entity_type"
        params["entity_type"] = entity_type

    if entity_address is not None:
        query += " AND entity_address = :entity_address"
        params["entity_address"] = entity_address

    if cursor_created_at is not None and cursor_id is not None:
        query += " AND (created_at, id) < (:cursor_created_at, :cursor_id)"
        params["cursor_created_at"] = cursor_created_at
        params["cursor_id"] = cursor_id

    query += " ORDER BY created_at DESC, id DESC LIMIT :limit"

    result = await db_session.execute(text(query), params)
    return [dict(row) for row in result.mappings().all()]


async def update_session_workflow_position(
    db_session: AsyncSession,
    *,
    session_id: int,
    last_card_uuid: str | None,
    next_card_uuid: str | None,
    runtime_patch_json: str,
) -> None:
    await db_session.execute(
        text(
            """
            UPDATE orch_sessions
            SET
                last_card_uuid = CAST(:last_card_uuid AS uuid),
                next_card_uuid = CAST(:next_card_uuid AS uuid),
                runtime_variables = COALESCE(runtime_variables, '{}'::jsonb) || CAST(:runtime_patch AS jsonb),
                updated_at = NOW()
            WHERE id = :session_id
            """
        ),
        {
            "session_id": session_id,
            "last_card_uuid": last_card_uuid,
            "next_card_uuid": next_card_uuid,
            "runtime_patch": runtime_patch_json,
        },
    )


async def fetch_session_workflow_state(
    db_session: AsyncSession,
    *,
    session_id: int,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            """
            SELECT
                id,
                uuid::text AS uuid,
                flow_uuid::text AS flow_uuid,
                runtime_variables,
                last_card_uuid::text AS last_card_uuid,
                next_card_uuid::text AS next_card_uuid,
                frozen_until,
                state
            FROM orch_sessions
            WHERE id = :session_id
            LIMIT 1
            """
        ),
        {"session_id": session_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def replace_session_workflow_state(
    db_session: AsyncSession,
    *,
    session_id: int,
    runtime_variables: dict[str, Any],
    last_card_uuid: str | None,
    next_card_uuid: str | None,
    frozen_until: datetime | None = None,
    ended_at: datetime | None = None,
    state: int | None = None,
) -> None:
    await db_session.execute(
        text(
            """
            UPDATE orch_sessions
            SET
                runtime_variables = CAST(:runtime_variables AS jsonb),
                last_card_uuid = CAST(:last_card_uuid AS uuid),
                next_card_uuid = CAST(:next_card_uuid AS uuid),
                frozen_until = COALESCE(CAST(:frozen_until AS timestamptz), frozen_until),
                ended_at = COALESCE(CAST(:ended_at AS timestamptz), ended_at),
                state = COALESCE(:state, state),
                updated_at = NOW()
            WHERE id = :session_id
            """
        ),
        {
            "session_id": session_id,
            "runtime_variables": json.dumps(runtime_variables, ensure_ascii=False),
            "last_card_uuid": last_card_uuid,
            "next_card_uuid": next_card_uuid,
            "frozen_until": frozen_until,
            "ended_at": ended_at,
            "state": state,
        },
    )


async def claim_pending_sessions_for_dispatch(
    db_session: AsyncSession,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    result = await db_session.execute(
        text(
            """
            WITH candidates AS (
                SELECT
                    id,
                    uuid,
                    flow_uuid,
                    created_at,
                    updated_at AS pending_since
                FROM orch_sessions
                WHERE
                    state = 0
                    AND next_card_uuid IS NOT NULL
                    AND ended_at IS NULL
                    AND EXISTS (
                        SELECT 1
                        FROM flow_v2 f
                        WHERE f.id = orch_sessions.flow_uuid
                    )
                    AND (
                        frozen_until IS NULL
                        OR frozen_until <= NOW()
                    )
                ORDER BY updated_at ASC, id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            UPDATE orch_sessions AS s
            SET
                state = 1,
                updated_at = NOW()
            FROM candidates
            WHERE s.id = candidates.id
            RETURNING
                s.id,
                candidates.uuid::text AS uuid,
                candidates.flow_uuid::text AS flow_uuid,
                candidates.created_at,
                candidates.pending_since
            """
        ),
        {"limit": max(1, min(limit, 500))},
    )
    return [dict(row) for row in result.mappings().all()]


async def set_session_state(
    db_session: AsyncSession,
    *,
    session_id: int,
    state: int,
    only_if_not_finished: bool = True,
) -> None:
    query = """
        UPDATE orch_sessions
        SET
            state = :state,
            updated_at = NOW()
        WHERE id = :session_id
    """
    if only_if_not_finished:
        query += " AND state NOT IN (:state_finished, :state_stopped_after_unassign)"
    await db_session.execute(
        text(query),
        {
            "session_id": session_id,
            "state": state,
            "state_finished": SESSION_STATE_FINISHED,
            "state_stopped_after_unassign": SESSION_STATE_STOPPED_AFTER_UNASSIGN,
        },
    )


async def mark_session_finished(
    db_session: AsyncSession,
    *,
    session_id: int,
) -> None:
    await db_session.execute(
        text(
            """
            UPDATE orch_sessions
            SET
                state = 3,
                ended_at = COALESCE(ended_at, NOW()),
                updated_at = NOW()
            WHERE id = :session_id
            """
        ),
        {"session_id": session_id},
    )


async def set_session_assigned_at_default(
    db_session: AsyncSession,
    *,
    session_id: int,
) -> None:
    await db_session.execute(
        text(
            """
            UPDATE orch_sessions
            SET
                assigned_at = COALESCE(assigned_at, NOW()),
                updated_at = NOW()
            WHERE id = :session_id
            """
        ),
        {"session_id": session_id},
    )


async def set_unassigned_at_by_flow_and_entity_address(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    entity_address: str,
) -> int:
    result = await db_session.execute(
        text(
            """
            UPDATE orch_sessions
            SET
                state = :state_stopped_after_unassign,
                unassigned_at = NOW(),
                ended_at = COALESCE(ended_at, NOW()),
                updated_at = NOW()
            WHERE flow_uuid = CAST(:flow_uuid AS uuid)
              AND entity_address = :entity_address
              AND unassigned_at IS NULL
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "entity_address": entity_address,
            "state_stopped_after_unassign": SESSION_STATE_STOPPED_AFTER_UNASSIGN,
        },
    )
    return int(result.rowcount or 0)
