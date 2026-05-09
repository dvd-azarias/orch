from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.workspace import get_current_workspace_schema
from app.repositories.orch_session_metrics_repository import insert_session_metrics

logger = get_logger(__name__)


async def persist_session_metrics(
    db_session: AsyncSession,
    *,
    metrics: list[dict[str, Any]],
) -> None:
    if not metrics:
        return
    safe_schema = get_current_workspace_schema().replace('"', '""')
    try:
        tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
        async with tx_context:
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            await insert_session_metrics(db_session, metrics=metrics)
    except Exception:
        logger.exception(
            "failed to persist session metrics",
            extra={
                "event": "session.metrics.persist.failure",
            },
        )
