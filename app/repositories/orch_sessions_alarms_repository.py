from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ALARM_SUMMARY_SELECT = """
id,
uuid::text AS uuid,
session_uuid::text AS session_uuid,
flow_uuid::text AS flow_uuid,
app_name,
entity,
entity_type,
entity_address,
level,
code,
message,
details,
request_id,
created_at
"""


async def insert_alarm(
    db_session: AsyncSession,
    *,
    level: str,
    code: str,
    message: str,
    details: dict[str, Any],
    request_id: str | None,
    flow_uuid: str | None,
    app_name: str | None,
    entity: str | None,
    entity_type: str | None,
    entity_address: str | None,
    session_uuid: str | None,
) -> None:
    await db_session.execute(
        text(
            """
            INSERT INTO orch_sessions_alarms (
                session_uuid,
                flow_uuid,
                app_name,
                entity,
                entity_type,
                entity_address,
                level,
                code,
                message,
                details,
                request_id,
                created_at
            ) VALUES (
                CAST(:session_uuid AS uuid),
                CAST(:flow_uuid AS uuid),
                :app_name,
                :entity,
                :entity_type,
                :entity_address,
                :level,
                :code,
                :message,
                CAST(:details AS jsonb),
                :request_id,
                NOW()
            )
            """
        ),
        {
            "session_uuid": session_uuid,
            "flow_uuid": flow_uuid,
            "app_name": app_name,
            "entity": entity,
            "entity_type": entity_type,
            "entity_address": entity_address,
            "level": level,
            "code": code,
            "message": message,
            "details": json.dumps(details, ensure_ascii=False),
            "request_id": request_id,
        },
    )


async def fetch_alarms(
    db_session: AsyncSession,
    *,
    level: str | None,
    code: str | None,
    flow_uuid: str | None,
    session_uuid: str | None,
    app_name: str | None,
    limit: int,
    cursor_created_at: datetime | None,
    cursor_id: int | None,
) -> list[dict[str, Any]]:
    query = f"""
        SELECT {ALARM_SUMMARY_SELECT}
        FROM orch_sessions_alarms
        WHERE 1=1
    """
    params: dict[str, Any] = {"limit": limit}

    if level is not None:
        query += " AND level = :level"
        params["level"] = level
    if code is not None:
        query += " AND code = :code"
        params["code"] = code
    if flow_uuid is not None:
        query += " AND flow_uuid = CAST(:flow_uuid AS uuid)"
        params["flow_uuid"] = flow_uuid
    if session_uuid is not None:
        query += " AND session_uuid = CAST(:session_uuid AS uuid)"
        params["session_uuid"] = session_uuid
    if app_name is not None:
        query += " AND app_name = :app_name"
        params["app_name"] = app_name
    if cursor_created_at is not None and cursor_id is not None:
        query += " AND (created_at, id) < (:cursor_created_at, :cursor_id)"
        params["cursor_created_at"] = cursor_created_at
        params["cursor_id"] = cursor_id

    query += " ORDER BY created_at DESC, id DESC LIMIT :limit"

    result = await db_session.execute(text(query), params)
    return [dict(row) for row in result.mappings().all()]
