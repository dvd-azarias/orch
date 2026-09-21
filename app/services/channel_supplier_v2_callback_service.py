from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.orch_channel_events_repository import (
    insert_channel_event,
    mark_channel_event_processed,
)

_MAX_CALLBACK_ITEMS = 100
_MAX_PROVIDER_EVENT_ID_LENGTH = 255
_SMS_SENT_STATUS_CODES = {4, 12, 13}
_SMS_DELIVERED_STATUS_CODES = {1}
_SMS_FAILED_STATUS_CODES = {2}


class ChannelSupplierV2CallbackError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class NormalizedChannelCallback:
    event_type: str
    event_id: str
    event_ts: datetime | None
    provider_payload: dict[str, Any]


@dataclass(frozen=True)
class ChannelCallbackPersistenceResult:
    session_id: int
    session_uuid: str
    accepted_count: int
    inserted_count: int
    idempotent_count: int
    resume_required: bool
    late_callback: bool


def _normalized_token(value: Any) -> str:
    raw = str(value or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", raw)
    without_marks = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", "_", without_marks).strip("_")


def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text_value = str(value).strip()
        if text_value:
            return text_value
    return ""


def _provider_event_id(payload: Mapping[str, Any]) -> str:
    event_id = _first_text(
        payload,
        "message_id",
        "messageid",
        "messageId",
        "id_mensagem",
        "id",
    )
    if not event_id or len(event_id) > _MAX_PROVIDER_EVENT_ID_LENGTH:
        raise ChannelSupplierV2CallbackError(
            "channel_supplier_v2_callback_event_id_invalid",
            "O callback não contém um identificador de mensagem válido.",
        )
    return event_id


def _parse_event_timestamp(payload: Mapping[str, Any]) -> datetime | None:
    raw_value: Any = None
    for key in (
        "date",
        "timestamp",
        "data_hora",
        "datahora",
        "sent_at",
        "delivered_at",
        "created_at",
    ):
        if payload.get(key) not in (None, ""):
            raw_value = payload.get(key)
            break
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
        try:
            return datetime.fromtimestamp(float(raw_value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    raw_text = str(raw_value).strip()
    if not raw_text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", raw_text):
        try:
            return datetime.fromtimestamp(float(raw_text), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(raw_text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _status_code(payload: Mapping[str, Any]) -> int | None:
    raw_code: Any = None
    for key in ("codigo_status", "status_code", "code", "status"):
        candidate = payload.get(key)
        if candidate not in (None, ""):
            raw_code = candidate
            break
    if isinstance(raw_code, bool):
        return None
    try:
        return int(str(raw_code).strip())
    except (TypeError, ValueError):
        return None


def _normalize_sms_event(
    *, event_kind: str, payload: Mapping[str, Any]
) -> str:
    if event_kind == "mo":
        return "response"

    code = _status_code(payload)
    description = _normalized_token(
        _first_text(payload, "descricao", "description", "status_description")
    )
    raw_status = _normalized_token(_first_text(payload, "status", "status_name"))
    combined = f"{raw_status}_{description}"

    if event_kind == "dlr":
        if code in _SMS_DELIVERED_STATUS_CODES or (
            "entregue" in combined and "nao_entregue" not in combined
        ):
            return "delivered"
        if code in _SMS_FAILED_STATUS_CODES or any(
            marker in combined
            for marker in ("nao_entregue", "falha", "failed", "rejeitad")
        ):
            return "not_delivered"
        return "dlr"

    if code in _SMS_SENT_STATUS_CODES or any(
        marker in combined for marker in ("valido", "enviado", "inserido", "accepted")
    ):
        return "sent"
    if any(
        marker in combined
        for marker in ("invalido", "falha", "failed", "rejeitad", "cancelad")
    ):
        return "failed"
    return "status"


def _normalize_rcs_event(
    *, event_kind: str, payload: Mapping[str, Any]
) -> str:
    if event_kind == "mo":
        return "response"

    status_token = _normalized_token(
        _first_text(payload, "status", "description", "status_description", "type")
    )
    if status_token in {"lido", "read"}:
        return "read"
    if status_token in {"entregue", "delivered"}:
        return "delivered"
    if status_token in {"indisponivel", "unavailable"}:
        return "unavailable"
    if status_token in {"expirado", "expirada", "expired"}:
        return "expired"
    if status_token in {
        "blacklist",
        "cancelado",
        "cancelada",
        "invalido",
        "invalida",
        "nao_entregue",
        "nao_enviado",
        "repetido",
        "failed",
        "failure",
    }:
        return "failed"
    if status_token in {
        "v",
        "valido",
        "valida",
        "enviado",
        "sent",
        "queued",
        "accepted",
    }:
        return "sent"
    return "status"


def normalize_channel_supplier_v2_callbacks(
    *,
    channel: str,
    event_kind: str,
    payload: Any,
) -> list[NormalizedChannelCallback]:
    normalized_channel = str(channel or "").strip().lower()
    normalized_kind = str(event_kind or "").strip().lower()
    allowed_kinds = {
        "sms": {"dlr", "mo", "status"},
        "rcs": {"mo", "status"},
    }
    if normalized_kind not in allowed_kinds.get(normalized_channel, set()):
        raise ChannelSupplierV2CallbackError(
            "channel_supplier_v2_callback_route_invalid",
            "O canal ou o tipo de callback não é suportado.",
        )

    items = payload if isinstance(payload, list) else [payload]
    if not items or len(items) > _MAX_CALLBACK_ITEMS:
        raise ChannelSupplierV2CallbackError(
            "channel_supplier_v2_callback_payload_invalid",
            "O callback deve conter de um a 100 eventos.",
        )

    normalized_items: list[NormalizedChannelCallback] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ChannelSupplierV2CallbackError(
                "channel_supplier_v2_callback_payload_invalid",
                "Cada evento do callback deve ser um objeto JSON.",
            )
        event_type = (
            _normalize_sms_event(event_kind=normalized_kind, payload=item)
            if normalized_channel == "sms"
            else _normalize_rcs_event(event_kind=normalized_kind, payload=item)
        )
        normalized_items.append(
            NormalizedChannelCallback(
                event_type=event_type,
                event_id=_provider_event_id(item),
                event_ts=_parse_event_timestamp(item),
                provider_payload=dict(item),
            )
        )
    return normalized_items


def _intent_matches_claims(intent: Any, claims: Mapping[str, Any]) -> bool:
    if not isinstance(intent, Mapping):
        return False
    expected_pairs = (
        ("session_uuid", "session_uuid"),
        ("flow_uuid", "flow_uuid"),
        ("flow_revision_id", "flow_revision_id"),
        ("component_ref_id", "component_ref_id"),
        ("channel", "channel"),
    )
    if any(
        str(intent.get(intent_key) or "").strip().lower()
        != str(claims.get(claim_key) or "").strip().lower()
        for intent_key, claim_key in expected_pairs
    ):
        return False
    try:
        return int(intent.get("dispatch_sequence")) == int(
            claims.get("dispatch_sequence")
        )
    except (TypeError, ValueError):
        return False


async def persist_channel_supplier_v2_callback(
    db_session: AsyncSession,
    *,
    claims: Mapping[str, Any],
    channel: str,
    event_kind: str,
    payload: Any,
) -> ChannelCallbackPersistenceResult | None:
    normalized_items = normalize_channel_supplier_v2_callbacks(
        channel=channel,
        event_kind=event_kind,
        payload=payload,
    )
    session_result = await db_session.execute(
        text(
            """
            SELECT
                id,
                uuid::text AS uuid,
                flow_uuid::text AS flow_uuid,
                state,
                ended_at,
                unassigned_at,
                runtime_variables
            FROM orch_sessions
            WHERE uuid = CAST(:session_uuid AS uuid)
              AND flow_uuid = CAST(:flow_uuid AS uuid)
            LIMIT 1
            FOR UPDATE
            """
        ),
        {
            "session_uuid": claims["session_uuid"],
            "flow_uuid": claims["flow_uuid"],
        },
    )
    session_row = session_result.mappings().first()
    if session_row is None:
        return None

    runtime_variables = session_row.get("runtime_variables")
    if not isinstance(runtime_variables, dict):
        runtime_variables = {}
    workflow_meta = runtime_variables.get("workflow_v2")
    if not isinstance(workflow_meta, dict):
        workflow_meta = {}
    active_intent = workflow_meta.get("channel_dispatch_v2")
    active_match = _intent_matches_claims(active_intent, claims)
    history = workflow_meta.get("channel_dispatch_v2_history")
    history_match = any(
        _intent_matches_claims(candidate, claims)
        for candidate in (history if isinstance(history, list) else [])
    )
    if not active_match and not history_match:
        raise ChannelSupplierV2CallbackError(
            "channel_supplier_v2_callback_identity_mismatch",
            "O callback não corresponde a uma intenção fixada nesta sessão.",
        )

    session_is_active = (
        int(session_row.get("state") or 0) in {0, 1, 2}
        and session_row.get("ended_at") is None
        and session_row.get("unassigned_at") is None
    )
    late_callback = not active_match or not session_is_active
    inserted_count = 0
    for event in normalized_items:
        ledger_payload = {
            "supplier_version": "v2",
            "event_kind": str(event_kind).strip().lower(),
            "normalized_event": event.event_type,
            "dispatch_identity": {
                "session_uuid": claims["session_uuid"],
                "flow_uuid": claims["flow_uuid"],
                "flow_revision_id": claims["flow_revision_id"],
                "component_ref_id": claims["component_ref_id"],
                "channel": claims["channel"],
                "dispatch_sequence": claims["dispatch_sequence"],
            },
            "provider_payload": event.provider_payload,
        }
        was_inserted = await insert_channel_event(
            db_session,
            session_id=int(session_row["id"]),
            flow_uuid=str(session_row["flow_uuid"]),
            channel=str(channel).strip().lower(),
            event_type=event.event_type,
            event_id=event.event_id,
            event_ts=event.event_ts,
            payload=ledger_payload,
        )
        if not was_inserted:
            continue
        inserted_count += 1
        if late_callback:
            pending_result = await db_session.execute(
                text(
                    """
                    SELECT id
                    FROM orch_channel_events
                    WHERE session_id = :session_id
                      AND channel = :channel
                      AND event_type = :event_type
                      AND event_id = :event_id
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ),
                {
                    "session_id": int(session_row["id"]),
                    "channel": str(channel).strip().lower(),
                    "event_type": event.event_type,
                    "event_id": event.event_id,
                },
            )
            event_row_id = pending_result.scalar()
            if event_row_id is not None:
                await mark_channel_event_processed(
                    db_session,
                    event_row_id=int(event_row_id),
                    session_id=int(session_row["id"]),
                    channel=str(channel).strip().lower(),
                    discard_reason="channel_supplier_v2_late_callback",
                )

    accepted_count = len(normalized_items)
    return ChannelCallbackPersistenceResult(
        session_id=int(session_row["id"]),
        session_uuid=str(session_row["uuid"]),
        accepted_count=accepted_count,
        inserted_count=inserted_count,
        idempotent_count=accepted_count - inserted_count,
        resume_required=bool(active_match and session_is_active),
        late_callback=late_callback,
    )


__all__ = [
    "ChannelCallbackPersistenceResult",
    "ChannelSupplierV2CallbackError",
    "NormalizedChannelCallback",
    "normalize_channel_supplier_v2_callbacks",
    "persist_channel_supplier_v2_callback",
]
