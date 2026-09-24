from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.repositories.orch_channel_reporting_repository import (
    register_channel_action,
    update_channel_action,
)

logger = get_logger(__name__)

REPORTING_CHANNELS = {"voice", "whatsapp", "sms", "rcs", "email"}
REPORTING_LIFECYCLE_STATUSES = {
    "prepared",
    "queued",
    "in_progress",
    "accepted",
    "completed",
    "failed",
    "uncertain",
    "cancelled",
    "unknown",
}
REPORTING_MILESTONES = {
    "queued",
    "accepted",
    "sent",
    "delivered",
    "engaged",
    "failed",
    "terminal",
}


def mask_channel_destination(raw_destination: str | None) -> str | None:
    value = str(raw_destination or "").strip()
    if not value:
        return None
    if "@" in value:
        local, separator, domain = value.partition("@")
        if not separator or not domain:
            return "***"
        visible = local[:1] if local else ""
        return f"{visible}***@{domain}"

    digits = "".join(character for character in value if character.isdigit())
    if digits:
        visible_suffix = digits[-4:]
        return f"{'*' * max(4, len(digits) - len(visible_suffix))}{visible_suffix}"
    return "***"


def _normalized_utc(value: datetime | None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        raise ValueError("Timestamp de reporting deve possuir timezone.")
    return resolved.astimezone(timezone.utc)


def _optional_uuid(value: str | None, field_name: str) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return str(UUID(normalized))
    except ValueError as exc:
        raise ValueError(f"{field_name} inválido para reporting.") from exc


def _required_uuid(value: str | None, field_name: str) -> str:
    normalized = _optional_uuid(value, field_name)
    if normalized is None:
        raise ValueError(f"{field_name} é obrigatório para reporting.")
    return normalized


async def register_channel_action_if_enabled(
    db_session: AsyncSession,
    *,
    session_id: int,
    session_uuid: str,
    flow_uuid: str,
    flow_revision_id: str | None,
    component_ref_id: str,
    component_kind: str,
    channel: str,
    action_sequence: int,
    source_kind: str,
    source_id: str,
    person_uuid: str | None = None,
    destination: str | None = None,
    provider_reference: str | None = None,
    lifecycle_status: str = "prepared",
    requested_at: datetime | None = None,
) -> dict[str, Any] | None:
    normalized_channel = str(channel or "").strip().lower()
    normalized_status = str(lifecycle_status or "").strip().lower()
    normalized_flow_uuid = str(flow_uuid or "").strip()

    try:
        if normalized_channel not in REPORTING_CHANNELS:
            raise ValueError(f"Canal de reporting inválido: {normalized_channel}")
        if normalized_status not in REPORTING_LIFECYCLE_STATUSES:
            raise ValueError(f"Status de reporting inválido: {normalized_status}")
        if action_sequence <= 0:
            raise ValueError("action_sequence deve ser maior que zero.")
        if not str(component_ref_id or "").strip():
            raise ValueError("component_ref_id é obrigatório para reporting.")
        if not str(source_kind or "").strip() or not str(source_id or "").strip():
            raise ValueError("source_kind e source_id são obrigatórios para reporting.")

        normalized_session_uuid = str(UUID(str(session_uuid)))
        normalized_flow_uuid = str(UUID(str(flow_uuid)))
        normalized_revision_uuid = _required_uuid(flow_revision_id, "flow_revision_id")
        normalized_person_uuid = _optional_uuid(person_uuid, "person_uuid")
        normalized_requested_at = _normalized_utc(requested_at)

        async with db_session.begin_nested():
            return await register_channel_action(
                db_session,
                session_id=session_id,
                session_uuid=normalized_session_uuid,
                flow_uuid=normalized_flow_uuid,
                flow_revision_id=normalized_revision_uuid,
                component_ref_id=str(component_ref_id).strip(),
                component_kind=str(component_kind or "unknown").strip() or "unknown",
                channel=normalized_channel,
                action_sequence=action_sequence,
                source_kind=str(source_kind).strip(),
                source_id=str(source_id).strip(),
                person_uuid=normalized_person_uuid,
                destination_masked=mask_channel_destination(destination),
                provider_reference=str(provider_reference or "").strip() or None,
                lifecycle_status=normalized_status,
                requested_at=normalized_requested_at,
            )
    except Exception:
        logger.exception(
            "channel reporting action registration failed open",
            extra={
                "event": "orch.channel_reporting.registration_failed_open",
                "session_id": session_id,
                "flow_uuid": normalized_flow_uuid,
                "component_ref_id": str(component_ref_id),
                "channel": normalized_channel,
                "source_kind": str(source_kind),
            },
        )
        return None


async def update_channel_action_if_exists(
    db_session: AsyncSession,
    *,
    source_kind: str,
    source_id: str,
    lifecycle_status: str,
    milestone: str,
    occurred_at: datetime | None = None,
    native_outcome: str | None = None,
    provider_reference: str | None = None,
) -> dict[str, Any] | None:
    normalized_status = str(lifecycle_status or "").strip().lower()
    normalized_milestone = str(milestone or "").strip().lower()

    try:
        if normalized_status not in REPORTING_LIFECYCLE_STATUSES:
            raise ValueError(f"Status de reporting inválido: {normalized_status}")
        if normalized_milestone not in REPORTING_MILESTONES:
            raise ValueError(f"Milestone de reporting inválido: {normalized_milestone}")
        if not str(source_kind or "").strip() or not str(source_id or "").strip():
            raise ValueError("source_kind e source_id são obrigatórios para reporting.")

        async with db_session.begin_nested():
            return await update_channel_action(
                db_session,
                source_kind=str(source_kind or "").strip(),
                source_id=str(source_id or "").strip(),
                lifecycle_status=normalized_status,
                milestone=normalized_milestone,
                occurred_at=_normalized_utc(occurred_at),
                native_outcome=str(native_outcome or "").strip() or None,
                provider_reference=str(provider_reference or "").strip() or None,
            )
    except Exception:
        logger.exception(
            "channel reporting action update failed open",
            extra={
                "event": "orch.channel_reporting.update_failed_open",
                "source_kind": str(source_kind),
                "lifecycle_status": normalized_status,
                "milestone": normalized_milestone,
            },
        )
        return None
