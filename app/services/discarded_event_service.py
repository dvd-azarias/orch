from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.request_context import get_request_id
from app.core.workspace import get_current_workspace_schema
from app.repositories.orch_discarded_events_repository import insert_discarded_event

logger = get_logger(__name__)


async def persist_discarded_event(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    app_name: str,
    entity: str | None,
    entity_type: str | None,
    entity_address: str | None,
    entity_session_id: str | None,
    discard_reason: str,
    payload: dict[str, Any],
) -> None:
    safe_schema = get_current_workspace_schema().replace('"', '""')
    request_id = get_request_id()

    try:
        tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
        async with tx_context:
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            await insert_discarded_event(
                db_session,
                flow_uuid=flow_uuid,
                app_name=app_name,
                entity=entity,
                entity_type=entity_type,
                entity_address=entity_address,
                entity_session_id=entity_session_id,
                discard_reason=discard_reason,
                payload=payload,
                request_id=request_id,
            )
    except Exception:
        logger.exception(
            "failed to persist discarded event",
            extra={
                "event": "discarded_event.persist.failure",
                "request_id": request_id,
                "discard_reason": discard_reason,
                "flow_uuid": flow_uuid,
                "app_name": app_name,
            },
        )
