from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workspace import get_current_workspace_schema
from app.repositories.orch_sessions_repository import PersistResult, upsert_active_session


@dataclass(frozen=True)
class SessionPersistResponse:
    session_id: int
    session_uuid: str
    session_state: int
    session_created: bool


async def persist_session(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    app_name: str,
    extracted: dict[str, Any],
    payload: dict[str, Any],
) -> SessionPersistResponse:
    safe_schema = get_current_workspace_schema().replace('"', '""')

    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        persisted: PersistResult = await upsert_active_session(
            db_session,
            flow_uuid=flow_uuid,
            app_name=app_name,
            entity=str(extracted["entity"]),
            entity_type=str(extracted["entity_type"]),
            entity_address=str(extracted["entity_address"]),
            entity_session_id=str(extracted["entity_session_id"]),
            payload=payload,
            extracted=extracted,
        )

    return SessionPersistResponse(
        session_id=persisted.id,
        session_uuid=persisted.uuid,
        session_state=persisted.state,
        session_created=persisted.created,
    )
