from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.request_context import get_request_id
from app.core.workspace import get_current_workspace_schema
from app.repositories.orch_sessions_alarms_repository import insert_alarm

logger = get_logger(__name__)


async def persist_alarm(
    db_session: AsyncSession,
    *,
    level: str,
    code: str,
    message: str,
    details: dict[str, Any],
    flow_uuid: str | None = None,
    app_name: str | None = None,
    entity: str | None = None,
    entity_type: str | None = None,
    entity_address: str | None = None,
    session_uuid: str | None = None,
) -> None:
    safe_schema = get_current_workspace_schema().replace('"', '""')
    request_id = get_request_id()

    try:
        tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
        async with tx_context:
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            await insert_alarm(
                db_session,
                level=level,
                code=code,
                message=message,
                details=details,
                request_id=request_id,
                flow_uuid=flow_uuid,
                app_name=app_name,
                entity=entity,
                entity_type=entity_type,
                entity_address=entity_address,
                session_uuid=session_uuid,
            )
    except Exception:
        logger.exception(
            "failed to persist alarm",
            extra={
                "event": "alarm.persist.failure",
                "request_id": request_id,
                "alarm_code": code,
                "alarm_level": level,
            },
        )
