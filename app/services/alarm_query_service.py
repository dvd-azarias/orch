from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.repositories.orch_sessions_alarms_repository import fetch_alarms


@dataclass(frozen=True)
class PagedAlarmsResult:
    items: list[dict]
    next_cursor: str | None


def _sanitize_limit(limit: int) -> int:
    if limit < 1:
        return 1
    if limit > 200:
        return 200
    return limit


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, int | None]:
    if cursor is None:
        return None, None

    try:
        raw = base64.urlsafe_b64decode(cursor.encode("utf-8")).decode("utf-8")
        created_at_raw, id_raw = raw.split("|", 1)
        created_at = datetime.fromisoformat(created_at_raw)
        return created_at, int(id_raw)
    except Exception as exc:
        raise ValueError("Cursor inválido.") from exc


def _encode_cursor(row: dict | None) -> str | None:
    if row is None:
        return None
    created_at = row.get("created_at")
    row_id = row.get("id")
    if created_at is None or row_id is None:
        return None
    raw = f"{created_at.isoformat()}|{int(row_id)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


async def list_alarms(
    db_session: AsyncSession,
    *,
    level: str | None,
    code: str | None,
    flow_uuid: str | None,
    session_uuid: str | None,
    app_name: str | None,
    limit: int,
    cursor: str | None,
) -> PagedAlarmsResult:
    settings = get_settings()
    safe_schema = settings.database_schema.replace('"', '""')
    safe_limit = _sanitize_limit(limit)
    cursor_created_at, cursor_id = _decode_cursor(cursor)

    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        rows = await fetch_alarms(
            db_session,
            level=level,
            code=code,
            flow_uuid=flow_uuid,
            session_uuid=session_uuid,
            app_name=app_name,
            limit=safe_limit + 1,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
        )

        has_more = len(rows) > safe_limit
        page_items = rows[:safe_limit]
        next_cursor = _encode_cursor(page_items[-1]) if has_more and page_items else None
        return PagedAlarmsResult(items=page_items, next_cursor=next_cursor)
