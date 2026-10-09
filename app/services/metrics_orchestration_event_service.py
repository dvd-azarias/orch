from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.workspace import get_current_workspace_uuid
from app.services.metrics_event_outbox_service import (
    enqueue_metrics_event,
    metrics_events_enabled_for_workspace,
)


logger = get_logger(__name__)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str | None:
    for raw_value in values:
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        if value:
            return value
    return None


def _first_uuid(*values: Any) -> str | None:
    for value in values:
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            continue
    return None


def _first_positive_int(*values: Any) -> str | None:
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return str(parsed)
    return None


def _utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw_value = str(value or "").strip()
        if not raw_value:
            return None
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_dispatched_at(
    *,
    observed_at: datetime,
    action_row: dict[str, Any],
    runtime: dict[str, Any],
    channel: str,
    component_ref_id: str,
) -> datetime:
    sent_at = _utc_datetime(action_row.get("sent_at"))
    if sent_at is not None:
        return sent_at
    if channel == "whatsapp":
        outbound = _mapping(runtime.get("whatsapp_hsm_outbound"))
        if str(outbound.get("component_ref_id") or "") == component_ref_id:
            prepared_at = _utc_datetime(outbound.get("prepared_at"))
            if prepared_at is not None:
                return prepared_at
    accepted_at = _utc_datetime(action_row.get("accepted_at"))
    if accepted_at is not None:
        return accepted_at
    candidates = [
        _utc_datetime(action_row.get("requested_at")),
        _utc_datetime(observed_at),
    ]
    return min(candidate for candidate in candidates if candidate is not None)


def _dispatch_duration_seconds(
    *,
    channel: str,
    canonical_status: str,
    metadata: dict[str, Any],
) -> Any:
    if channel == "voice":
        if canonical_status == "answered":
            return metadata.get("duration_seconds")
        return 0
    return None


def _metrics_channel(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    aliases = {
        "voice": "VOZ",
        "phone": "VOZ",
        "dialer": "VOZ",
        "voz": "VOZ",
        "whatsapp": "WHATSAPP",
        "sms": "SMS",
        "rcs": "RCS",
        "email": "EMAIL",
        "e-mail": "EMAIL",
    }
    return aliases.get(normalized)


def _catalog_scalar(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("name", "label", "code", "id", "value"):
            if value.get(key) not in (None, ""):
                return value[key]
        return None
    if isinstance(value, list):
        return _catalog_scalar(value[0]) if value else None
    return value


def _component_from_definition(
    definition: Any,
    *,
    component_ref_id: str,
) -> dict[str, Any]:
    if not isinstance(definition, dict):
        return {}
    components = definition.get("components")
    if not isinstance(components, list):
        return {}
    for component in components:
        if (
            isinstance(component, dict)
            and str(component.get("ref_id") or "") == component_ref_id
        ):
            return component
    return {}


def _whatsapp_template_name(
    component: dict[str, Any],
    runtime: dict[str, Any],
) -> str | None:
    outbound = _mapping(runtime.get("whatsapp_hsm_outbound"))
    if str(outbound.get("component_ref_id") or "") == str(
        component.get("ref_id") or ""
    ):
        template_name = _first_text(outbound.get("template_name"))
        if template_name:
            return template_name

    params = _mapping(component.get("parameters"))
    config = _mapping(params.get("whatsapp_interactive_config"))
    numbers = config.get("numbers")
    if isinstance(numbers, list):
        for item in numbers:
            item_value = _mapping(_mapping(item).get("value"))
            selected = _mapping(_mapping(item_value.get("template")).get("selected"))
            template_name = _first_text(selected.get("name"))
            if template_name:
                return template_name
            meta_template = _mapping(_mapping(item_value.get("meta_payload")).get("template"))
            template_name = _first_text(meta_template.get("name"))
            if template_name:
                return template_name
    return _first_text(
        _catalog_scalar(params.get("template_name")),
        _catalog_scalar(params.get("template")),
    )


def _dispatch_template_name(
    *,
    channel: str,
    component: dict[str, Any],
    runtime: dict[str, Any],
) -> str | None:
    normalized_channel = str(channel or "").strip().lower()
    if normalized_channel == "whatsapp":
        return _whatsapp_template_name(component, runtime)
    params = _mapping(component.get("parameters"))
    if normalized_channel == "rcs":
        return _first_text(
            _catalog_scalar(params.get("template_code")),
            _catalog_scalar(params.get("template_name")),
        )
    if normalized_channel == "email":
        return _first_text(
            _catalog_scalar(params.get("template_name")),
            _catalog_scalar(params.get("template_id")),
            _catalog_scalar(params.get("template")),
        )
    return None


def _dispatch_destination(*, channel: str, contact: dict[str, Any]) -> str | None:
    destination = _first_text(
        contact.get("contact_email") if channel == "email" else None,
        contact.get("destination"),
    )
    if destination is None:
        return None
    if channel == "email":
        return destination
    digits = re.sub(r"\D+", "", destination)
    if digits.startswith("00"):
        digits = digits[2:]
    return f"+{digits}" if digits else None


def _canonical_dispatch_status(*, channel: str, native_status: str) -> str | None:
    normalized_channel = str(channel or "").strip().lower()
    normalized_native = str(native_status or "").strip().lower()
    mappings = {
        "whatsapp": {
            "sent": "sent",
            "delivered": "delivered",
            "read": "read",
            "failed": "failed",
            "message": "replied",
        },
        "sms": {
            "accepted": "sent",
            "sent": "sent",
            "delivered": "delivered",
            "response": "replied",
            "not_delivered": "failed",
            "failed": "failed",
            "expired": "failed",
            "rejected": "failed",
        },
        "rcs": {
            "accepted": "sent",
            "sent": "sent",
            "delivered": "delivered",
            "read": "read",
            "response": "replied",
            "unavailable": "failed",
            "expired": "failed",
            "failed": "failed",
        },
        "voice": {
            "dialing": "dialing",
            "answered": "answered",
            "busy": "busy",
            "machine": "machine",
            "no_answer": "no_answer",
            "rejected": "rejected",
            "invalid_number": "invalid_number",
            "failed": "failed",
        },
    }
    if normalized_channel == "whatsapp" and normalized_native.startswith("message:"):
        return "replied"
    return mappings.get(normalized_channel, {}).get(normalized_native)


async def _load_or_create_dispatch_snapshot(
    db_session: AsyncSession,
    *,
    action_id: str,
    observed_at: datetime,
) -> dict[str, Any] | None:
    row = (
        await db_session.execute(
            text(
                """
                SELECT
                    action.action_id::text AS action_id,
                    action.channel,
                    action.component_ref_id::text AS component_ref_id,
                    action.component_kind,
                    action.requested_at,
                    action.accepted_at,
                    action.sent_at,
                    journey.source_session_id,
                    journey.session_uuid::text AS session_uuid,
                    journey.flow_uuid::text AS flow_uuid,
                    journey.flow_revision_id::text AS flow_revision_id,
                    source.entity,
                    source.entity_type,
                    source.entity_address,
                    source.runtime_variables,
                    flow.display_name AS flow_name,
                    revision.definition,
                    dispatch_snapshot.snapshot AS existing_snapshot
                FROM orch_journey_channel_actions action
                JOIN orch_journey_sessions journey
                  ON journey.id = action.journey_session_id
                JOIN orch_sessions source
                  ON source.id = journey.source_session_id
                LEFT JOIN flow_v2 flow
                  ON flow.id = journey.flow_uuid
                LEFT JOIN flow_v2_revision revision
                  ON revision.id = journey.flow_revision_id
                LEFT JOIN orch_metrics_dispatch_snapshots dispatch_snapshot
                  ON dispatch_snapshot.action_id = action.action_id
                WHERE action.action_id = CAST(:action_id AS uuid)
                FOR UPDATE OF action
                """
            ),
            {"action_id": action_id},
        )
    ).mappings().first()
    if row is None:
        return None

    session_row = dict(row)
    runtime = _mapping(session_row.get("runtime_variables"))
    contact = resolve_metrics_contact_snapshot(session_row)
    component_ref_id = str(session_row["component_ref_id"])
    component = _component_from_definition(
        session_row.get("definition"),
        component_ref_id=component_ref_id,
    )
    channel = str(session_row.get("channel") or "").strip().lower()
    dispatched_at = _resolve_dispatched_at(
        observed_at=observed_at,
        action_row=session_row,
        runtime=runtime,
        channel=channel,
        component_ref_id=component_ref_id,
    )
    existing = session_row.get("existing_snapshot")
    if isinstance(existing, dict):
        existing_snapshot = dict(existing)
        existing_dispatched_at = _utc_datetime(
            existing_snapshot.get("dispatched_at")
        )
        if existing_dispatched_at == dispatched_at:
            return existing_snapshot
        already_published = bool(
            (
                await db_session.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM orch_metrics_event_outbox
                            WHERE event_type = 'flow.dispatch.updated.v1'
                              AND envelope #>> '{payload,dispatch_id}' = :action_id
                              AND published_at IS NOT NULL
                        )
                        """
                    ),
                    {"action_id": action_id},
                )
            ).scalar_one()
        )
        if already_published:
            return existing_snapshot
        dispatched_at_text = dispatched_at.isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        existing_snapshot["dispatched_at"] = dispatched_at_text
        await db_session.execute(
            text(
                """
                UPDATE orch_metrics_dispatch_snapshots
                SET dispatched_at = CAST(:dispatched_at AS timestamptz),
                    snapshot = CAST(:snapshot AS jsonb)
                WHERE action_id = CAST(:action_id AS uuid)
                """
            ),
            {
                "action_id": action_id,
                "dispatched_at": dispatched_at,
                "snapshot": json.dumps(existing_snapshot, ensure_ascii=False),
            },
        )
        await db_session.execute(
            text(
                """
                UPDATE orch_metrics_event_outbox
                SET envelope = jsonb_set(
                        envelope,
                        '{payload,dispatched_at}',
                        to_jsonb(CAST(:dispatched_at_text AS text)),
                        true
                    )
                WHERE event_type = 'flow.dispatch.updated.v1'
                  AND envelope #>> '{payload,dispatch_id}' = :action_id
                  AND published_at IS NULL
                """
            ),
            {
                "action_id": action_id,
                "dispatched_at_text": dispatched_at_text,
            },
        )
        return existing_snapshot
    snapshot = {
        "dispatch_id": str(session_row["action_id"]),
        "dispatched_at": dispatched_at.isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "flow_id": str(session_row["flow_uuid"]),
        "node_id": component_ref_id,
        "interaction_id": str(session_row["session_uuid"]),
        "contact_id": contact.get("contact_id"),
        "channel": _metrics_channel(channel),
        "destination": _dispatch_destination(channel=channel, contact=contact),
        "contact_name": contact.get("contact_name"),
        "contact_email": contact.get("contact_email"),
        "contact_identifier": contact.get("contact_identifier"),
        "flow_name": _first_text(session_row.get("flow_name")),
        "template_name": _dispatch_template_name(
            channel=channel,
            component=component,
            runtime=runtime,
        ),
    }
    if not all(
        snapshot.get(field)
        for field in (
            "dispatch_id",
            "flow_id",
            "node_id",
            "interaction_id",
            "contact_id",
            "channel",
            "destination",
        )
    ):
        logger.warning(
            "metrics dispatch snapshot incomplete",
            extra={
                "event": "orch.metrics_events.dispatch_snapshot_incomplete",
                "action_id": action_id,
                "flow_uuid": snapshot.get("flow_id"),
                "session_uuid": snapshot.get("interaction_id"),
                "component_ref_id": snapshot.get("node_id"),
            },
        )
        return None
    inserted = (
        await db_session.execute(
            text(
                """
                INSERT INTO orch_metrics_dispatch_snapshots (
                    action_id, dispatch_id, dispatched_at, snapshot
                )
                VALUES (
                    CAST(:action_id AS uuid), CAST(:dispatch_id AS uuid),
                    CAST(:dispatched_at AS timestamptz), CAST(:snapshot AS jsonb)
                )
                ON CONFLICT (action_id) DO NOTHING
                RETURNING snapshot
                """
            ),
            {
                "action_id": action_id,
                "dispatch_id": snapshot["dispatch_id"],
                "dispatched_at": dispatched_at,
                "snapshot": json.dumps(snapshot, ensure_ascii=False),
            },
        )
    ).scalar_one_or_none()
    if isinstance(inserted, dict):
        return dict(inserted)
    concurrent = (
        await db_session.execute(
            text(
                """
                SELECT snapshot
                FROM orch_metrics_dispatch_snapshots
                WHERE action_id = CAST(:action_id AS uuid)
                """
            ),
            {"action_id": action_id},
        )
    ).scalar_one_or_none()
    return dict(concurrent) if isinstance(concurrent, dict) else None


async def try_enqueue_metrics_dispatch_event(
    db_session: AsyncSession,
    *,
    action_id: str,
    event_key: str,
    native_status: str,
    occurred_at: datetime,
    metadata: dict[str, Any] | None = None,
) -> None:
    settings = get_settings()
    safe_metadata = metadata if isinstance(metadata, dict) else {}
    try:
        workspace_uuid = get_current_workspace_uuid()
        if not metrics_events_enabled_for_workspace(
            workspace_uuid,
            settings=settings,
        ):
            return
        async with db_session.begin_nested():
            action = (
                await db_session.execute(
                    text(
                        """
                        SELECT channel
                        FROM orch_journey_channel_actions
                        WHERE action_id = CAST(:action_id AS uuid)
                        """
                    ),
                    {"action_id": action_id},
                )
            ).mappings().first()
            if action is None:
                return
            channel = str(action["channel"] or "").strip().lower()
            canonical_status = _canonical_dispatch_status(
                channel=channel,
                native_status=native_status,
            )
            if canonical_status is None:
                return
            snapshot = await _load_or_create_dispatch_snapshot(
                db_session,
                action_id=action_id,
                observed_at=occurred_at,
            )
            if snapshot is None:
                return
            context = {
                "flow_id": snapshot["flow_id"],
                "flow_type": "orchestration",
                "node_id": snapshot["node_id"],
                "interaction_id": snapshot["interaction_id"],
                "contact_id": snapshot["contact_id"],
                "channel": snapshot["channel"],
            }
            provider_status = _first_text(
                safe_metadata.get("provider_status"),
                native_status,
            )
            payload = {
                "dispatch_id": snapshot["dispatch_id"],
                "dispatched_at": snapshot["dispatched_at"],
                "status": canonical_status,
                "destination": snapshot["destination"],
                "contact_name": snapshot.get("contact_name"),
                "contact_email": snapshot.get("contact_email"),
                "contact_identifier": snapshot.get("contact_identifier"),
                "flow_name": snapshot.get("flow_name"),
                "template_name": snapshot.get("template_name"),
                "provider_status": provider_status,
                "error_code": safe_metadata.get("error_code"),
                "error_message": _first_text(safe_metadata.get("error_message")),
                "duration_seconds": _dispatch_duration_seconds(
                    channel=channel,
                    canonical_status=canonical_status,
                    metadata=safe_metadata,
                ),
            }
            await enqueue_metrics_event(
                db_session,
                workspace_uuid=workspace_uuid,
                idempotency_key=f"dispatch:{action_id}:{event_key}",
                event_type="flow.dispatch.updated.v1",
                context=context,
                payload=payload,
                occurred_at=occurred_at,
                flow_uuid=snapshot["flow_id"],
                session_uuid=snapshot["interaction_id"],
            )
    except Exception as exc:
        logger.warning(
            "metrics dispatch outbox failed",
            extra={
                "event": "orch.metrics_events.dispatch_outbox_failed",
                "action_id": action_id,
                "native_status": native_status,
                "exception_type": type(exc).__name__,
            },
        )


def resolve_metrics_contact_snapshot(session_row: dict[str, Any]) -> dict[str, Any]:
    runtime = _mapping(session_row.get("runtime_variables"))
    workflow = _mapping(runtime.get("workflow_v2"))
    selected = _mapping(workflow.get("selected_contact_channel"))
    adoption = _mapping(workflow.get("person_adoption"))
    operational_scope = _mapping(
        workflow.get("source_list_membership_operational_scope")
    )
    variables = _mapping(runtime.get("variables"))
    contact = _mapping(variables.get("contact"))
    input_payload = _mapping(runtime.get("input_payload"))
    last_payload = _mapping(runtime.get("last_payload"))
    session_identity = _mapping(runtime.get("session_identity"))

    person_uuid = _first_uuid(
        selected.get("person_uuid"),
        adoption.get("person_uuid"),
        operational_scope.get("person_uuid"),
        contact.get("person_uuid"),
        contact.get("uuid"),
        input_payload.get("person_uuid"),
        last_payload.get("person_uuid"),
    )
    member_id = _first_positive_int(
        selected.get("contact_list_member_id"),
        session_identity.get("contact_list_member_id"),
        input_payload.get("contact_list_member_id"),
        last_payload.get("contact_list_member_id"),
    )
    draft_id = _first_text(
        contact.get("contact_draft_id"),
        contact.get("draft_id"),
        input_payload.get("contact_draft_id"),
        last_payload.get("contact_draft_id"),
    )
    external_identifier = _first_text(
        contact.get("identifier"),
        input_payload.get("contact_identifier"),
        input_payload.get("identifier"),
        input_payload.get("customer_code"),
        last_payload.get("contact_identifier"),
        session_row.get("entity"),
    )
    contact_id = person_uuid or member_id or draft_id or external_identifier
    channel = _metrics_channel(
        selected.get("type")
        or input_payload.get("channel_type")
        or session_row.get("entity_type")
    )
    destination = _first_text(
        selected.get("address"),
        contact.get("channel", {}).get("address")
        if isinstance(contact.get("channel"), dict)
        else None,
        input_payload.get("channel_address"),
        session_row.get("entity_address"),
    )
    return {
        "contact_id": contact_id,
        "contact_name": _first_text(
            contact.get("full_name"),
            contact.get("name"),
            input_payload.get("full_name"),
            input_payload.get("name"),
        ),
        "contact_email": _first_text(
            contact.get("email"),
            input_payload.get("email"),
        ),
        "contact_identifier": external_identifier,
        "channel": channel,
        "destination": destination,
    }


async def _source_session_row(
    db_session: AsyncSession,
    *,
    source_session_id: int,
) -> dict[str, Any] | None:
    row = (
        await db_session.execute(
            text(
                """
                SELECT
                    id,
                    uuid::text AS session_uuid,
                    flow_uuid::text AS flow_uuid,
                    state,
                    entity,
                    entity_type,
                    entity_address,
                    runtime_variables,
                    started_at,
                    ended_at,
                    abandoned_at,
                    created_at
                FROM orch_sessions
                WHERE id = :source_session_id
                """
            ),
            {"source_session_id": source_session_id},
        )
    ).mappings().first()
    return dict(row) if row is not None else None


def _base_context(
    *,
    flow_uuid: str,
    session_uuid: str,
    contact: dict[str, Any],
    node_id: str | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "flow_id": flow_uuid,
        "flow_type": "orchestration",
        "interaction_id": session_uuid,
    }
    if contact.get("contact_id"):
        context["contact_id"] = contact["contact_id"]
    if contact.get("channel"):
        context["channel"] = contact["channel"]
    if node_id:
        context["node_id"] = node_id
    return context


async def _enqueue_session_open_events(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    source_session_id: int,
    flow_uuid: str,
    session_uuid: str,
) -> None:
    session_row = await _source_session_row(
        db_session,
        source_session_id=source_session_id,
    )
    if session_row is None:
        return
    contact = resolve_metrics_contact_snapshot(session_row)
    context = _base_context(
        flow_uuid=flow_uuid,
        session_uuid=session_uuid,
        contact=contact,
    )
    occurred_at = (
        session_row.get("started_at")
        or session_row.get("created_at")
        or datetime.now(timezone.utc)
    )
    await enqueue_metrics_event(
        db_session,
        workspace_uuid=workspace_uuid,
        idempotency_key=f"session:{session_uuid}:started",
        event_type="interaction.session.started.v1",
        context=context,
        payload={
            "direction": "OUTBOUND",
            "entry_point": contact.get("channel"),
            "contact_phone": (
                contact.get("destination")
                if contact.get("channel") in {"VOZ", "WHATSAPP", "SMS", "RCS"}
                else None
            ),
            "contact_email": contact.get("contact_email"),
            "contact_name": contact.get("contact_name"),
            "campaign_id": flow_uuid,
            "initial_context": {},
        },
        occurred_at=occurred_at,
        flow_uuid=flow_uuid,
        session_uuid=session_uuid,
    )
    await enqueue_metrics_event(
        db_session,
        workspace_uuid=workspace_uuid,
        idempotency_key=f"execution:{session_uuid}:started",
        event_type="flow.execution.started.v1",
        context=context,
        payload={"trigger_type": "event", "execution_id": session_uuid},
        occurred_at=occurred_at,
        flow_uuid=flow_uuid,
        session_uuid=session_uuid,
    )


async def try_enqueue_metrics_session_open_events(
    db_session: AsyncSession,
    *,
    source_session_id: int,
    flow_uuid: str,
    session_uuid: str,
) -> None:
    settings = get_settings()
    try:
        workspace_uuid = get_current_workspace_uuid()
        if not metrics_events_enabled_for_workspace(
            workspace_uuid,
            settings=settings,
        ):
            return
        async with db_session.begin_nested():
            await _enqueue_session_open_events(
                db_session,
                workspace_uuid=workspace_uuid,
                source_session_id=source_session_id,
                flow_uuid=flow_uuid,
                session_uuid=session_uuid,
            )
    except Exception as exc:
        logger.warning(
            "metrics session open outbox failed",
            extra={
                "event": "orch.metrics_events.session_open_outbox_failed",
                "source_session_id": source_session_id,
                "flow_uuid": flow_uuid,
                "session_uuid": session_uuid,
                "exception_type": type(exc).__name__,
            },
        )


async def try_enqueue_metrics_node_entered(
    db_session: AsyncSession,
    *,
    source_session_id: int,
    flow_uuid: str,
    session_uuid: str,
    component_ref_id: str,
    component_kind: str,
    visit_number: int,
    occurred_at: datetime,
) -> None:
    settings = get_settings()
    try:
        workspace_uuid = get_current_workspace_uuid()
        if not metrics_events_enabled_for_workspace(
            workspace_uuid,
            settings=settings,
        ):
            return
        async with db_session.begin_nested():
            session_row = await _source_session_row(
                db_session,
                source_session_id=source_session_id,
            )
            if session_row is None:
                return
            contact = resolve_metrics_contact_snapshot(session_row)
            await enqueue_metrics_event(
                db_session,
                workspace_uuid=workspace_uuid,
                idempotency_key=(
                    f"node:{session_uuid}:{component_ref_id}:{visit_number}:entered"
                ),
                event_type="flow.node.entered.v1",
                context=_base_context(
                    flow_uuid=flow_uuid,
                    session_uuid=session_uuid,
                    contact=contact,
                    node_id=component_ref_id,
                ),
                payload={
                    "node_type": component_kind or "unknown",
                    "execution_id": session_uuid,
                },
                occurred_at=occurred_at,
                flow_uuid=flow_uuid,
                session_uuid=session_uuid,
            )
    except Exception as exc:
        logger.warning(
            "metrics node entered outbox failed",
            extra={
                "event": "orch.metrics_events.node_entered_outbox_failed",
                "source_session_id": source_session_id,
                "flow_uuid": flow_uuid,
                "session_uuid": session_uuid,
                "component_ref_id": component_ref_id,
                "exception_type": type(exc).__name__,
            },
        )


async def try_enqueue_metrics_node_exited(
    db_session: AsyncSession,
    *,
    source_session_id: int,
    flow_uuid: str,
    session_uuid: str,
    visit_id: int,
    component_ref_id: str,
    component_kind: str,
    occurred_at: datetime,
) -> None:
    settings = get_settings()
    try:
        workspace_uuid = get_current_workspace_uuid()
        if not metrics_events_enabled_for_workspace(
            workspace_uuid,
            settings=settings,
        ):
            return
        async with db_session.begin_nested():
            session_row = await _source_session_row(
                db_session,
                source_session_id=source_session_id,
            )
            visit_row = (
                await db_session.execute(
                    text(
                        """
                        SELECT entered_at, visit_number
                        FROM orch_journey_stage_visits
                        WHERE id = :visit_id
                        """
                    ),
                    {"visit_id": visit_id},
                )
            ).mappings().first()
            if session_row is None or visit_row is None:
                return
            contact = resolve_metrics_contact_snapshot(session_row)
            entered_at = visit_row["entered_at"]
            duration_ms = max(
                0,
                int((occurred_at - entered_at).total_seconds() * 1000),
            )
            visit_number = int(visit_row["visit_number"])
            await enqueue_metrics_event(
                db_session,
                workspace_uuid=workspace_uuid,
                idempotency_key=(
                    f"node:{session_uuid}:{component_ref_id}:{visit_number}:exited"
                ),
                event_type="flow.node.exited.v1",
                context=_base_context(
                    flow_uuid=flow_uuid,
                    session_uuid=session_uuid,
                    contact=contact,
                    node_id=component_ref_id,
                ),
                payload={
                    "node_type": component_kind or "unknown",
                    "execution_id": session_uuid,
                    "duration_ms": duration_ms,
                },
                occurred_at=occurred_at,
                flow_uuid=flow_uuid,
                session_uuid=session_uuid,
            )
    except Exception as exc:
        logger.warning(
            "metrics node exited outbox failed",
            extra={
                "event": "orch.metrics_events.node_exited_outbox_failed",
                "source_session_id": source_session_id,
                "flow_uuid": flow_uuid,
                "session_uuid": session_uuid,
                "component_ref_id": component_ref_id,
                "exception_type": type(exc).__name__,
            },
        )


async def try_enqueue_metrics_session_terminal_events(
    db_session: AsyncSession,
    *,
    source_session_id: int,
    flow_uuid: str,
    session_uuid: str,
    lifecycle_status: str,
    terminal_outcome: str | None,
    occurred_at: datetime,
) -> None:
    settings = get_settings()
    try:
        workspace_uuid = get_current_workspace_uuid()
        if not metrics_events_enabled_for_workspace(
            workspace_uuid,
            settings=settings,
        ):
            return
        async with db_session.begin_nested():
            session_row = await _source_session_row(
                db_session,
                source_session_id=source_session_id,
            )
            if session_row is None:
                return
            contact = resolve_metrics_contact_snapshot(session_row)
            context = _base_context(
                flow_uuid=flow_uuid,
                session_uuid=session_uuid,
                contact=contact,
            )
            started_at = session_row.get("started_at") or session_row.get("created_at")
            duration_ms = (
                max(0, int((occurred_at - started_at).total_seconds() * 1000))
                if started_at is not None
                else 0
            )
            execution_event_type = (
                "flow.execution.failed.v1"
                if lifecycle_status == "failed"
                else "flow.execution.completed.v1"
            )
            execution_payload = (
                {
                    "error_code": terminal_outcome or "workflow_failed",
                    "error_message": terminal_outcome,
                    "execution_id": session_uuid,
                    "duration_ms": duration_ms,
                }
                if lifecycle_status == "failed"
                else {
                    "status": lifecycle_status,
                    "execution_id": session_uuid,
                    "duration_ms": duration_ms,
                }
            )
            await enqueue_metrics_event(
                db_session,
                workspace_uuid=workspace_uuid,
                idempotency_key=f"execution:{session_uuid}:terminal",
                event_type=execution_event_type,
                context=context,
                payload=execution_payload,
                occurred_at=occurred_at,
                flow_uuid=flow_uuid,
                session_uuid=session_uuid,
            )
            await enqueue_metrics_event(
                db_session,
                workspace_uuid=workspace_uuid,
                idempotency_key=f"session:{session_uuid}:terminal",
                event_type="interaction.session.ended.v1",
                context=context,
                payload={
                    "status": lifecycle_status,
                    "duration_seconds": duration_ms // 1000,
                    "messages_exchanged": 0,
                },
                occurred_at=occurred_at,
                flow_uuid=flow_uuid,
                session_uuid=session_uuid,
            )
    except Exception as exc:
        logger.warning(
            "metrics session terminal outbox failed",
            extra={
                "event": "orch.metrics_events.session_terminal_outbox_failed",
                "source_session_id": source_session_id,
                "flow_uuid": flow_uuid,
                "session_uuid": session_uuid,
                "lifecycle_status": lifecycle_status,
                "exception_type": type(exc).__name__,
            },
        )
