from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.workspace import get_current_workspace_schema
from app.repositories.orch_channel_events_repository import insert_channel_event

logger = get_logger(__name__)


@dataclass(frozen=True)
class ChannelEventItem:
    channel: str
    event_type: str
    event_id: str | None
    event_ts: datetime | None
    payload: dict[str, Any]


def _parse_unix_timestamp(raw_value: Any) -> datetime | None:
    if raw_value is None:
        return None
    try:
        parsed = float(str(raw_value).strip())
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(parsed, tz=timezone.utc)


def _extract_whatsapp_channel_events(payload: dict[str, Any]) -> list[ChannelEventItem]:
    if payload.get("object") != "whatsapp_business_account":
        return []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return []

    items: list[ChannelEventItem] = []
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
                event_type = str(status_item.get("status", "")).strip().lower()
                if not event_type:
                    continue
                event_id_raw = status_item.get("id")
                event_id = str(event_id_raw).strip() if event_id_raw is not None else None
                if event_id == "":
                    event_id = None
                items.append(
                    ChannelEventItem(
                        channel="whatsapp",
                        event_type=event_type,
                        event_id=event_id,
                        event_ts=_parse_unix_timestamp(status_item.get("timestamp")),
                        payload=payload,
                    )
                )
    return items


def extract_channel_events(app_name: str, payload: dict[str, Any]) -> list[ChannelEventItem]:
    if app_name == "WhatsApp":
        return _extract_whatsapp_channel_events(payload)
    return []


async def persist_channel_events(
    db_session: AsyncSession,
    *,
    session_id: int,
    flow_uuid: str,
    app_name: str,
    payload: dict[str, Any],
) -> int:
    events = extract_channel_events(app_name, payload)
    if not events:
        return 0

    safe_schema = get_current_workspace_schema().replace('"', '""')
    persisted = 0
    try:
        tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
        async with tx_context:
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            for event in events:
                await insert_channel_event(
                    db_session,
                    session_id=session_id,
                    flow_uuid=flow_uuid,
                    channel=event.channel,
                    event_type=event.event_type,
                    event_id=event.event_id,
                    event_ts=event.event_ts,
                    payload=event.payload,
                )
                persisted += 1
    except Exception:
        logger.exception(
            "failed to persist channel events",
            extra={
                "event": "channel_event.persist.failure",
                "session_id": session_id,
                "flow_uuid": flow_uuid,
                "app_name": app_name,
                "events_count": len(events),
            },
        )
        return 0
    return persisted
