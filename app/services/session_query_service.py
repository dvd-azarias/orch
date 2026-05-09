from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime

from app.core.workspace import get_current_workspace_schema
from app.repositories.orch_sessions_repository import (
    fetch_session_by_uuid,
    fetch_sessions_by_entity,
    fetch_sessions_by_flow_uuid,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class PagedSessionsResult:
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


async def get_session_by_uuid(db_session: AsyncSession, *, session_uuid: str) -> dict | None:
    safe_schema = get_current_workspace_schema().replace('"', '""')

    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        return await fetch_session_by_uuid(db_session, session_uuid=session_uuid)


async def list_sessions_by_flow_uuid(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    limit: int,
    cursor: str | None,
) -> PagedSessionsResult:
    safe_schema = get_current_workspace_schema().replace('"', '""')
    safe_limit = _sanitize_limit(limit)
    cursor_created_at, cursor_id = _decode_cursor(cursor)

    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        rows = await fetch_sessions_by_flow_uuid(
            db_session,
            flow_uuid=flow_uuid,
            limit=safe_limit + 1,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
        )
        has_more = len(rows) > safe_limit
        page_items = rows[:safe_limit]
        next_cursor = _encode_cursor(page_items[-1]) if has_more and page_items else None
        return PagedSessionsResult(items=page_items, next_cursor=next_cursor)


async def list_sessions_by_entity(
    db_session: AsyncSession,
    *,
    entity: str,
    entity_type: str | None,
    entity_address: str | None,
    limit: int,
    cursor: str | None,
) -> PagedSessionsResult:
    safe_schema = get_current_workspace_schema().replace('"', '""')
    safe_limit = _sanitize_limit(limit)
    cursor_created_at, cursor_id = _decode_cursor(cursor)

    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        rows = await fetch_sessions_by_entity(
            db_session,
            entity=entity,
            entity_type=entity_type,
            entity_address=entity_address,
            limit=safe_limit + 1,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
        )
        has_more = len(rows) > safe_limit
        page_items = rows[:safe_limit]
        next_cursor = _encode_cursor(page_items[-1]) if has_more and page_items else None
        return PagedSessionsResult(items=page_items, next_cursor=next_cursor)
