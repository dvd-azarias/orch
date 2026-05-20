from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.workspace import get_current_workspace_schema
from app.services.phone_normalizer import normalize_phone_to_canonical_ani

SESSION_STATE_FINISHED = 3
SESSION_STATE_STOPPED_AFTER_UNASSIGN = 5
WHATSAPP_STATUS_COLUMNS = {
    "sent": "whatsapp_sent_at",
    "delivered": "whatsapp_delivered_at",
    "read": "whatsapp_read_at",
    "failed": "whatsapp_failed_at",
}


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


def _build_callback_runtime_patch(
    *,
    app_name: str,
    payload: dict[str, Any],
    extracted: dict[str, Any],
) -> str:
    callback_payload = {
        "event_name": str(payload.get("event_name", "")).strip().lower(),
        "entity": str(extracted.get("entity", "")).strip(),
        "result": str(payload.get("result", "")).strip().lower(),
        "data": payload.get("data") if isinstance(payload.get("data"), dict) else {},
        "received_at": _now_utc_iso(),
    }
    runtime_patch = {
        "source_app": app_name,
        "last_event_received_at": _now_utc_iso(),
        "last_payload": payload,
        "last_extracted": extracted,
        "callback": callback_payload,
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


def _compute_effective_whatsapp_limit(
    *,
    allowed_limit_raw: Any,
    percentual_consumo: int,
) -> int | None:
    if allowed_limit_raw is None:
        return None

    allowed_limit = int(allowed_limit_raw)
    if allowed_limit < 0:
        return None

    percentual = max(0, min(100, int(percentual_consumo)))
    if percentual <= 0:
        return 0
    return int((allowed_limit * percentual) // 100)


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


def _extract_whatsapp_status_name(payload: dict[str, Any]) -> str | None:
    if payload.get("object") != "whatsapp_business_account":
        return None

    entries = payload.get("entry")
    if not isinstance(entries, list):
        return None

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
                if status_name:
                    return status_name
    return None


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
    whatsapp_status_name = _extract_whatsapp_status_name(payload) if app_name == "WhatsApp" else None
    whatsapp_status_column = WHATSAPP_STATUS_COLUMNS.get(whatsapp_status_name or "")
    keep_whatsapp_session_active = app_name == "WhatsApp" and whatsapp_status_name in {
        "sent",
        "delivered",
        "read",
        "failed",
        "limit_reached",
    }

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
                state = CASE
                    WHEN :keep_whatsapp_session_active THEN 2
                    ELSE GREATEST(state, :state)
                END,
                ended_at = CASE
                    WHEN :keep_whatsapp_session_active THEN NULL
                    ELSE COALESCE(:ended_at, ended_at)
                END,
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
            "keep_whatsapp_session_active": keep_whatsapp_session_active,
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
                state = CASE
                    WHEN :keep_whatsapp_session_active THEN 2
                    ELSE GREATEST(state, :state)
                END,
                ended_at = CASE
                    WHEN :keep_whatsapp_session_active THEN NULL
                    ELSE COALESCE(:ended_at, ended_at)
                END,
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
            "keep_whatsapp_session_active": keep_whatsapp_session_active,
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

    if app_name == "WhatsApp" and whatsapp_status_column is not None:
        closed_pending_result = await db_session.execute(
            text(
                f"""
                UPDATE orch_sessions
                SET
                    entity_session_id = COALESCE(entity_session_id, :entity_session_id),
                    entity_origin_app = COALESCE(entity_origin_app, :entity_origin_app),
                    state = CASE
                        WHEN :keep_whatsapp_session_active THEN 2
                        ELSE GREATEST(state, :state)
                    END,
                    ended_at = CASE
                        WHEN :keep_whatsapp_session_active THEN NULL
                        ELSE COALESCE(:ended_at, ended_at)
                    END,
                    runtime_variables = COALESCE(runtime_variables, '{{}}'::jsonb) || CAST(:runtime_patch AS jsonb),
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
                        AND unassigned_at IS NULL
                        AND {whatsapp_status_column} IS NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                )
                RETURNING id, uuid::text AS uuid, state
                """
            ),
            {
                "flow_uuid": flow_uuid,
                "entity_type": entity_type,
                "entity_address": entity_address,
                "entity_session_id": entity_session_id,
                "entity_origin_app": app_name,
                "state": state_update.state,
                "keep_whatsapp_session_active": keep_whatsapp_session_active,
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
        closed_pending_row = closed_pending_result.mappings().first()
        if closed_pending_row is not None:
            return PersistResult(
                id=int(closed_pending_row["id"]),
                uuid=str(closed_pending_row["uuid"]),
                state=int(closed_pending_row["state"]),
                created=False,
            )

    if app_name == "WhatsApp" and keep_whatsapp_session_active and whatsapp_status_column is None:
        closed_active_result = await db_session.execute(
            text(
                """
                UPDATE orch_sessions
                SET
                    entity_session_id = COALESCE(entity_session_id, :entity_session_id),
                    entity_origin_app = COALESCE(entity_origin_app, :entity_origin_app),
                    state = 2,
                    ended_at = NULL,
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
                        AND unassigned_at IS NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                )
                RETURNING id, uuid::text AS uuid, state
                """
            ),
            {
                "flow_uuid": flow_uuid,
                "entity_type": entity_type,
                "entity_address": entity_address,
                "entity_session_id": entity_session_id,
                "entity_origin_app": app_name,
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
        closed_active_row = closed_active_result.mappings().first()
        if closed_active_row is not None:
            return PersistResult(
                id=int(closed_active_row["id"]),
                uuid=str(closed_active_row["uuid"]),
                state=int(closed_active_row["state"]),
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


async def fetch_latest_session_by_flow_entity_address(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    entity_type: str,
    entity_address: str,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            """
            SELECT
                id,
                uuid::text AS uuid,
                flow_uuid::text AS flow_uuid,
                state,
                entity,
                entity_type,
                entity_address,
                entity_session_id,
                last_card_uuid::text AS last_card_uuid,
                next_card_uuid::text AS next_card_uuid,
                runtime_variables,
                whatsapp_sent_at,
                whatsapp_delivered_at,
                whatsapp_read_at,
                whatsapp_failed_at,
                created_at
            FROM orch_sessions
            WHERE flow_uuid = CAST(:flow_uuid AS uuid)
              AND entity_type = :entity_type
              AND entity_address = :entity_address
              AND unassigned_at IS NULL
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "entity_type": entity_type,
            "entity_address": entity_address,
        },
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def persist_callback_event_for_active_entity(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    app_name: str,
    entity: str,
    payload: dict[str, Any],
    extracted: dict[str, Any],
) -> PersistResult | None:
    lock_key = f"callback|{flow_uuid}|{entity}"
    runtime_patch_json = _build_callback_runtime_patch(
        app_name=app_name,
        payload=payload,
        extracted=extracted,
    )
    safe_schema = get_current_workspace_schema().replace('"', '""')

    await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))

    await db_session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": lock_key},
    )

    update_result = await db_session.execute(
        text(
            """
            UPDATE orch_sessions
            SET
                runtime_variables = COALESCE(runtime_variables, '{}'::jsonb) || CAST(:runtime_patch AS jsonb),
                callback_at = NOW(),
                updated_at = NOW()
            WHERE id = (
                SELECT id
                FROM orch_sessions
                WHERE
                    flow_uuid = CAST(:flow_uuid AS uuid)
                    AND entity = :entity
                    AND unassigned_at IS NULL
                    AND ended_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
            )
            RETURNING id, uuid::text AS uuid, state
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "entity": entity,
            "runtime_patch": runtime_patch_json,
        },
    )
    row = update_result.mappings().first()
    if row is None:
        return None
    return PersistResult(
        id=int(row["id"]),
        uuid=str(row["uuid"]),
        state=int(row["state"]),
        created=False,
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
                whatsapp_sent_at,
                whatsapp_delivered_at,
                whatsapp_read_at,
                whatsapp_failed_at,
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


async def assign_whatsapp_routing_for_session(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    session_id: int,
    numbers: list[str],
    percentual_by_phone: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    def _canonical_phone(value: str | None) -> str:
        return str(normalize_phone_to_canonical_ani(value) or "").strip()

    normalized_numbers: list[str] = []
    seen: set[str] = set()
    for raw in numbers:
        value = _canonical_phone(raw)
        if not value or value in seen:
            continue
        seen.add(value)
        normalized_numbers.append(value)

    lock_key = f"whatsapp-routing|{flow_uuid}"
    await db_session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": lock_key},
    )

    target_result = await db_session.execute(
        text(
            """
            SELECT
                clm.id,
                clm.ani AS previous_ani,
                clm.linked_actuator AS previous_linked_actuator
            FROM contact_list_members clm
            JOIN orch_sessions os
              ON os.entity = clm.contact_identifier
            WHERE os.id = :session_id
              AND os.flow_uuid = CAST(:flow_uuid AS uuid)
              AND os.unassigned_at IS NULL
              AND clm.unassigned_at IS NULL
            ORDER BY clm.created_at DESC, clm.id DESC
            LIMIT 1
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "session_id": session_id,
        },
    )
    target = target_result.mappings().first()
    if target is None:
        return None

    member_id = int(target["id"])
    previous_ani = str(target["previous_ani"]).strip() if target["previous_ani"] is not None else ""
    previous_ani_canonical = _canonical_phone(previous_ani)
    previous_linked_actuator = (
        str(target["previous_linked_actuator"]).strip().lower()
        if target["previous_linked_actuator"] is not None
        else ""
    )

    if not normalized_numbers:
        update_result = await db_session.execute(
            text(
                """
                UPDATE contact_list_members
                SET
                    linked_actuator = 'whatsapp',
                    updated_at = NOW()
                WHERE id = :member_id
                RETURNING id, ani, linked_actuator
                """
            ),
            {"member_id": member_id},
        )
        row = update_result.mappings().first()
        if row is None:
            return None
        return {
            "contact_list_member_id": int(row["id"]),
            "ani": row["ani"],
            "linked_actuator": row["linked_actuator"],
            "mode": "linked_actuator_only",
            "consumption": None,
        }

    candidates_result = await db_session.execute(
        text(
            """
            SELECT
                pool.number AS phone,
                COALESCE(rate.consumed, 0) AS consumed,
                lim.allowed_limit AS allowed_limit
            FROM UNNEST(CAST(:numbers AS text[])) AS pool(number)
            LEFT JOIN (
                SELECT
                    CASE
                        WHEN LEFT(regexp_replace(phone, '\\D', '', 'g'), 2) = '55'
                             AND LENGTH(regexp_replace(phone, '\\D', '', 'g')) IN (12, 13)
                        THEN SUBSTRING(regexp_replace(phone, '\\D', '', 'g') FROM 3)
                        ELSE regexp_replace(phone, '\\D', '', 'g')
                    END AS canonical_phone,
                    SUM(consumed)::bigint AS consumed
                FROM orch_whatsapp_rate_limit_per_flow
                WHERE flow_uuid = CAST(:flow_uuid AS uuid)
                  AND day = CURRENT_DATE
                GROUP BY 1
            ) rate
              ON rate.canonical_phone = pool.number
            LEFT JOIN (
                SELECT canonical_phone, allowed_limit
                FROM (
                    SELECT
                        CASE
                            WHEN LEFT(regexp_replace(phone, '\\D', '', 'g'), 2) = '55'
                                 AND LENGTH(regexp_replace(phone, '\\D', '', 'g')) IN (12, 13)
                            THEN SUBSTRING(regexp_replace(phone, '\\D', '', 'g') FROM 3)
                            ELSE regexp_replace(phone, '\\D', '', 'g')
                        END AS canonical_phone,
                        allowed_limit,
                        ROW_NUMBER() OVER (
                            PARTITION BY CASE
                                WHEN LEFT(regexp_replace(phone, '\\D', '', 'g'), 2) = '55'
                                     AND LENGTH(regexp_replace(phone, '\\D', '', 'g')) IN (12, 13)
                                THEN SUBSTRING(regexp_replace(phone, '\\D', '', 'g') FROM 3)
                                ELSE regexp_replace(phone, '\\D', '', 'g')
                            END
                            ORDER BY received_from_meta_at DESC, id DESC
                        ) AS rn
                    FROM orch_whatsapp_limits
                    WHERE in_use = TRUE
                ) ranked_limits
                WHERE rn = 1
            ) lim
              ON lim.canonical_phone = pool.number
            ORDER BY COALESCE(rate.consumed, 0) ASC, pool.number ASC
            """
        ),
        {
            "numbers": normalized_numbers,
            "flow_uuid": flow_uuid,
        },
    )
    candidates = [dict(row) for row in candidates_result.mappings().all()]
    percentual_map = percentual_by_phone if isinstance(percentual_by_phone, dict) else {}

    def _normalized_percentual(phone: str) -> int:
        raw = percentual_map.get(phone, 0)
        try:
            value = int(float(str(raw).strip()))
        except Exception:
            value = 0
        if value < 0:
            return 0
        if value > 100:
            return 100
        return value

    def _effective_limit(candidate: dict[str, Any]) -> int | None:
        percentual = _normalized_percentual(str(candidate.get("phone") or "").strip())
        return _compute_effective_whatsapp_limit(
            allowed_limit_raw=candidate.get("allowed_limit"),
            percentual_consumo=percentual,
        )

    def _has_remaining_limit(candidate: dict[str, Any]) -> bool:
        effective_limit = _effective_limit(candidate)
        if effective_limit is None:
            return True
        consumed_value = int(candidate.get("consumed") or 0)
        return consumed_value < effective_limit

    def _is_rate_limit_block(candidate: dict[str, Any]) -> bool:
        effective_limit = _effective_limit(candidate)
        if effective_limit is None:
            return False
        consumed_value = int(candidate.get("consumed") or 0)
        return consumed_value >= effective_limit

    candidate_by_phone = {str(item.get("phone")): item for item in candidates}
    chosen_phone: str | None = None
    mode = "balanced_ani"

    if previous_linked_actuator == "whatsapp" and previous_ani_canonical in candidate_by_phone:
        previous_candidate = candidate_by_phone[previous_ani_canonical]
        if _has_remaining_limit(previous_candidate):
            chosen_phone = previous_ani_canonical
            mode = "reuse_previous_with_limit"

    if chosen_phone is None:
        for item in candidates:
            if _has_remaining_limit(item):
                chosen_phone = str(item.get("phone") or "").strip()
                if chosen_phone:
                    mode = "balanced_with_limit"
                    break

    if chosen_phone is None:
        fallback_phone = ""
        if previous_ani_canonical in candidate_by_phone:
            fallback_phone = previous_ani_canonical
        elif candidates:
            fallback_phone = str(candidates[0].get("phone") or "").strip()
        blocked_by_rate_limit = any(_is_rate_limit_block(item) for item in candidates)
        limit_exhausted_actuator = (
            "whatsapp_without_limit_by_rate_limit"
            if blocked_by_rate_limit
            else "whatsapp_without_limit"
        )

        update_result = await db_session.execute(
            text(
                """
                UPDATE contact_list_members
                SET
                    ani = :ani,
                    linked_actuator = CAST(:linked_actuator AS linked_actuator_enum),
                    updated_at = NOW()
                WHERE id = :member_id
                RETURNING id, ani, linked_actuator
                """
            ),
            {
                "member_id": member_id,
                "ani": fallback_phone or None,
                "linked_actuator": limit_exhausted_actuator,
            },
        )
        row = update_result.mappings().first()
        if row is None:
            return None
        return {
            "contact_list_member_id": int(row["id"]),
            "ani": row["ani"],
            "linked_actuator": row["linked_actuator"],
            "mode": "all_numbers_without_limit",
            "numbers_pool": normalized_numbers,
            "consumption": None,
            "limit_candidates": candidates,
        }

    update_result = await db_session.execute(
        text(
            """
            UPDATE contact_list_members
            SET
                ani = :ani,
                linked_actuator = 'whatsapp',
                updated_at = NOW()
            WHERE id = :member_id
            RETURNING id, ani, linked_actuator
            """
        ),
        {
            "member_id": member_id,
            "ani": chosen_phone,
        },
    )
    row = update_result.mappings().first()
    if row is None:
        return None

    current_ani = _canonical_phone(str(row["ani"]).strip()) if row["ani"] is not None else ""
    current_linked_actuator = str(row["linked_actuator"]).strip().lower() if row["linked_actuator"] is not None else ""
    should_increment_consumption = bool(current_ani) and current_linked_actuator == "whatsapp"
    consumption = None
    if should_increment_consumption:
        consumption = await increment_whatsapp_rate_limit_per_flow(
            db_session,
            flow_uuid=flow_uuid,
            phone=current_ani,
        )
    return {
        "contact_list_member_id": int(row["id"]),
        "ani": row["ani"],
        "linked_actuator": row["linked_actuator"],
        "mode": mode,
        "numbers_pool": normalized_numbers,
        "consumption": consumption,
        "limit_candidates": candidates,
    }


async def assign_dialer_routing_for_session(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    session_id: int,
) -> dict[str, Any] | None:
    target_result = await db_session.execute(
        text(
            """
            SELECT
                clm.id
            FROM contact_list_members clm
            JOIN orch_sessions os
              ON os.entity = clm.contact_identifier
            WHERE os.id = :session_id
              AND os.flow_uuid = CAST(:flow_uuid AS uuid)
              AND os.unassigned_at IS NULL
              AND clm.unassigned_at IS NULL
            ORDER BY clm.created_at DESC, clm.id DESC
            LIMIT 1
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "session_id": session_id,
        },
    )
    target = target_result.mappings().first()
    if target is None:
        return None

    member_id = int(target["id"])
    update_result = await db_session.execute(
        text(
            """
            UPDATE contact_list_members
            SET
                linked_actuator = 'dialer',
                updated_at = NOW()
            WHERE id = :member_id
            RETURNING id, ani, linked_actuator
            """
        ),
        {"member_id": member_id},
    )
    row = update_result.mappings().first()
    if row is None:
        return None

    return {
        "contact_list_member_id": int(row["id"]),
        "ani": row["ani"],
        "linked_actuator": row["linked_actuator"],
        "mode": "dialer",
        "consumption": None,
    }


async def fetch_contact_runtime_context_for_session(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    session_id: int,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            """
            SELECT
                clm.id AS contact_list_member_id,
                clm.contact_identifier,
                clm.contact_name,
                clm.contact_full_name,
                clm.contact_gender,
                clm.contact_country,
                clm.contact_province,
                clm.contact_city,
                clm.contact_birth_date,
                clm.contact_age,
                clm.contact_channel_type,
                clm.contact_channel_label,
                clm.contact_channel_address,
                clm.contact_channel_extra_data,
                clm.person_uuid::text AS person_uuid
            FROM contact_list_members clm
            JOIN orch_sessions os
              ON os.entity = clm.contact_identifier
            WHERE os.id = :session_id
              AND os.flow_uuid = CAST(:flow_uuid AS uuid)
              AND os.unassigned_at IS NULL
              AND clm.unassigned_at IS NULL
            ORDER BY clm.created_at DESC, clm.id DESC
            LIMIT 1
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "session_id": session_id,
        },
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


def _detect_channel_type_for_create_contact(address: str) -> str:
    token = str(address or "").strip()
    if not token:
        return "phone"
    if "@" in token:
        return "email"
    return "phone"


async def ensure_default_source_list_for_create_contact(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
) -> dict[str, Any]:
    existing_result = await db_session.execute(
        text(
            """
            SELECT
                id,
                public_id::text AS public_id
            FROM source_lists
            WHERE
                name = 'default_list'
                AND origin = 'orch_create_contact'
                AND file_path = :flow_uuid
                AND status = 'READY_TO_INGEST'
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"flow_uuid": flow_uuid},
    )
    existing = existing_result.mappings().first()
    if existing is not None:
        return {
            "id": int(existing["id"]),
            "public_id": str(existing["public_id"]),
            "created": False,
        }

    inserted_result = await db_session.execute(
        text(
            """
            INSERT INTO source_lists (
                name,
                description,
                status,
                origin,
                file_name,
                file_path,
                rows_total,
                rows_processed,
                rows_discarded,
                rows_error,
                rows_without_channel,
                created_at,
                updated_at
            )
            VALUES (
                'default_list',
                :description,
                'READY_TO_INGEST',
                'orch_create_contact',
                'default_list',
                :flow_uuid,
                0,
                0,
                0,
                0,
                0,
                NOW(),
                NOW()
            )
            RETURNING id, public_id::text AS public_id
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "description": f"Lista padrão automática do componente create_contact ({flow_uuid})",
        },
    )
    inserted = inserted_result.mappings().one()
    return {
        "id": int(inserted["id"]),
        "public_id": str(inserted["public_id"]),
        "created": True,
    }


async def increment_source_list_counters_for_create_contact(
    db_session: AsyncSession,
    *,
    source_list_id: int,
    created_members: int,
) -> None:
    increment_value = max(0, int(created_members))
    if increment_value <= 0:
        return
    await db_session.execute(
        text(
            """
            UPDATE source_lists
            SET
                rows_total = COALESCE(rows_total, 0) + :created_members,
                rows_processed = COALESCE(rows_processed, 0) + :created_members,
                updated_at = NOW()
            WHERE id = :source_list_id
            """
        ),
        {
            "source_list_id": source_list_id,
            "created_members": increment_value,
        },
    )


async def upsert_person_for_create_contact(
    db_session: AsyncSession,
    *,
    identifier: str,
    address: str,
    full_name: str | None,
    extras: dict[str, Any],
    source_list_id: int,
) -> dict[str, Any]:
    canonical_identifier = str(identifier or "").strip()
    canonical_address = str(address or "").strip()
    normalized_phone = str(normalize_phone_to_canonical_ani(canonical_address) or "").strip()
    primary_channel_value = normalized_phone or canonical_address
    primary_channel_type = _detect_channel_type_for_create_contact(primary_channel_value)
    channels = [{"type": primary_channel_type, "value": primary_channel_value, "label": "create_contact"}]

    result = await db_session.execute(
        text(
            """
            INSERT INTO persons (
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
                full_name = COALESCE(EXCLUDED.full_name, persons.full_name),
                primary_channel_type = EXCLUDED.primary_channel_type,
                primary_channel_value = EXCLUDED.primary_channel_value,
                primary_channel_label = EXCLUDED.primary_channel_label,
                channels = EXCLUDED.channels,
                extras = COALESCE(persons.extras, '{}'::jsonb) || EXCLUDED.extras,
                last_source_list_id = EXCLUDED.last_source_list_id,
                last_mailing_id = EXCLUDED.last_mailing_id,
                last_seen_at = NOW(),
                updated_at = NOW()
            RETURNING id, uuid::text AS uuid
            """
        ),
        {
            "identifier": canonical_identifier,
            "full_name": str(full_name).strip() if full_name is not None and str(full_name).strip() else None,
            "primary_channel_type": primary_channel_type,
            "primary_channel_value": primary_channel_value,
            "primary_channel_label": "create_contact",
            "channels": json.dumps(channels, ensure_ascii=False),
            "extras": json.dumps(extras or {}, ensure_ascii=False),
            "source_list_id": source_list_id,
        },
    )
    row = result.mappings().one()
    return {
        "id": int(row["id"]),
        "uuid": str(row["uuid"]),
        "channel_type": primary_channel_type,
        "channel_value": primary_channel_value,
    }


async def ensure_contact_list_member_for_create_contact(
    db_session: AsyncSession,
    *,
    source_list_id: int,
    person_uuid: str,
    identifier: str,
    address: str,
    full_name: str | None,
    extras: dict[str, Any],
) -> dict[str, Any]:
    canonical_identifier = str(identifier or "").strip()
    canonical_address = str(address or "").strip()
    normalized_phone = str(normalize_phone_to_canonical_ani(canonical_address) or "").strip()
    channel_address = normalized_phone or canonical_address
    channel_type = _detect_channel_type_for_create_contact(channel_address)
    contact_name = str(full_name or "").strip() or None
    extra_payload = extras if isinstance(extras, dict) else {}

    existing_result = await db_session.execute(
        text(
            """
            SELECT id
            FROM contact_list_members
            WHERE
                contact_identifier = :contact_identifier
                AND mailing_id = :mailing_id
                AND unassigned_at IS NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ),
        {
            "contact_identifier": canonical_identifier,
            "mailing_id": source_list_id,
        },
    )
    existing = existing_result.mappings().first()
    if existing is not None:
        member_id = int(existing["id"])
        await db_session.execute(
            text(
                """
                UPDATE contact_list_members
                SET
                    contact_name = COALESCE(:contact_name, contact_name),
                    contact_full_name = COALESCE(:contact_full_name, contact_full_name),
                    contact_channel_type = :contact_channel_type,
                    contact_channel_label = :contact_channel_label,
                    contact_channel_address = :contact_channel_address,
                    contact_channel_extra_data = CAST(:contact_channel_extra_data AS jsonb),
                    person_uuid = CAST(:person_uuid AS uuid),
                    ani = :ani,
                    updated_at = NOW()
                WHERE id = :member_id
                """
            ),
            {
                "member_id": member_id,
                "contact_name": contact_name,
                "contact_full_name": contact_name,
                "contact_channel_type": channel_type,
                "contact_channel_label": "create_contact",
                "contact_channel_address": channel_address,
                "contact_channel_extra_data": json.dumps(extra_payload, ensure_ascii=False),
                "person_uuid": person_uuid,
                "ani": normalized_phone or None,
            },
        )
        return {
            "id": member_id,
            "created": False,
            "contact_channel_address": channel_address,
            "contact_channel_type": channel_type,
        }

    insert_result = await db_session.execute(
        text(
            """
            INSERT INTO contact_list_members (
                contact_identifier,
                contact_name,
                contact_full_name,
                contact_channel_type,
                contact_channel_label,
                contact_channel_address,
                contact_channel_extra_data,
                contact_channel_id,
                mailing_id,
                linked_actuator,
                created_at,
                updated_at,
                person_uuid,
                ani
            ) VALUES (
                :contact_identifier,
                :contact_name,
                :contact_full_name,
                :contact_channel_type,
                :contact_channel_label,
                :contact_channel_address,
                CAST(:contact_channel_extra_data AS jsonb),
                gen_random_uuid(),
                :mailing_id,
                NULL,
                NOW(),
                NOW(),
                CAST(:person_uuid AS uuid),
                :ani
            )
            RETURNING id
            """
        ),
        {
            "contact_identifier": canonical_identifier,
            "contact_name": contact_name,
            "contact_full_name": contact_name,
            "contact_channel_type": channel_type,
            "contact_channel_label": "create_contact",
            "contact_channel_address": channel_address,
            "contact_channel_extra_data": json.dumps(extra_payload, ensure_ascii=False),
            "mailing_id": source_list_id,
            "person_uuid": person_uuid,
            "ani": normalized_phone or None,
        },
    )
    row = insert_result.mappings().one()
    return {
        "id": int(row["id"]),
        "created": True,
        "contact_channel_address": channel_address,
        "contact_channel_type": channel_type,
    }


async def ensure_session_for_created_contact(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    entity: str,
    entity_type: str,
    entity_address: str,
    entity_session_id: str,
    last_card_uuid: str,
    next_card_uuid: str,
    runtime_variables: dict[str, Any],
) -> dict[str, Any]:
    existing_result = await db_session.execute(
        text(
            """
            SELECT id, uuid::text AS uuid
            FROM orch_sessions
            WHERE
                flow_uuid = CAST(:flow_uuid AS uuid)
                AND entity = :entity
                AND entity_type = :entity_type
                AND entity_address = :entity_address
                AND unassigned_at IS NULL
                AND ended_at IS NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "entity": entity,
            "entity_type": entity_type,
            "entity_address": entity_address,
        },
    )
    existing = existing_result.mappings().first()
    if existing is not None:
        return {
            "id": int(existing["id"]),
            "uuid": str(existing["uuid"]),
            "created": False,
        }

    inserted_result = await db_session.execute(
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
                last_card_uuid,
                next_card_uuid,
                assigned_at,
                created_at,
                updated_at
            )
            VALUES (
                CAST(:flow_uuid AS uuid),
                0,
                'CreateContact',
                :entity,
                :entity_type,
                :entity_address,
                :entity_session_id,
                NOW(),
                NULL,
                CAST(:runtime_variables AS jsonb),
                CAST(:last_card_uuid AS uuid),
                CAST(:next_card_uuid AS uuid),
                NOW(),
                NOW(),
                NOW()
            )
            RETURNING id, uuid::text AS uuid
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "entity": entity,
            "entity_type": entity_type,
            "entity_address": entity_address,
            "entity_session_id": entity_session_id,
            "runtime_variables": json.dumps(runtime_variables, ensure_ascii=False),
            "last_card_uuid": last_card_uuid,
            "next_card_uuid": next_card_uuid,
        },
    )
    row = inserted_result.mappings().one()
    return {
        "id": int(row["id"]),
        "uuid": str(row["uuid"]),
        "created": True,
    }


async def increment_whatsapp_rate_limit_per_flow(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    phone: str,
) -> dict[str, Any]:
    canonical_phone = str(normalize_phone_to_canonical_ani(phone) or "").strip()
    if not canonical_phone:
        raise ValueError("phone inválido para incremento de consumo de WhatsApp.")

    result = await db_session.execute(
        text(
            """
            INSERT INTO orch_whatsapp_rate_limit_per_flow (
                flow_uuid,
                phone,
                consumed,
                day,
                created_at,
                updated_at
            )
            VALUES (
                CAST(:flow_uuid AS uuid),
                :phone,
                1,
                CURRENT_DATE,
                NOW(),
                NOW()
            )
            ON CONFLICT (flow_uuid, phone, day)
            DO UPDATE
            SET
                consumed = orch_whatsapp_rate_limit_per_flow.consumed + 1,
                updated_at = NOW()
            RETURNING
                flow_uuid::text AS flow_uuid,
                phone,
                consumed,
                day
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "phone": canonical_phone,
        },
    )
    row = dict(result.mappings().one())
    raw_day = row.get("day")
    if raw_day is not None and hasattr(raw_day, "isoformat"):
        row["day"] = raw_day.isoformat()
    return row
