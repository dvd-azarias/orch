from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def insert_session_metrics(
    db_session: AsyncSession,
    *,
    metrics: list[dict[str, Any]],
) -> None:
    if not metrics:
        return

    statement = text(
        """
        INSERT INTO orch_session_metrics (
            session_id,
            session_uuid,
            flow_uuid,
            revision_id,
            metric_type,
            step_index,
            card_uuid,
            card_cursor,
            component_kind,
            status,
            stopped_reason,
            latency_ms,
            started_at,
            finished_at,
            details,
            created_at
        ) VALUES (
            :session_id,
            CAST(:session_uuid AS uuid),
            CAST(:flow_uuid AS uuid),
            CAST(:revision_id AS uuid),
            :metric_type,
            :step_index,
            CAST(:card_uuid AS uuid),
            :card_cursor,
            :component_kind,
            :status,
            :stopped_reason,
            :latency_ms,
            :started_at,
            :finished_at,
            CAST(:details AS jsonb),
            NOW()
        )
        """
    )

    params: list[dict[str, Any]] = []
    for item in metrics:
        params.append(
            {
                "session_id": int(item["session_id"]),
                "session_uuid": item.get("session_uuid"),
                "flow_uuid": item.get("flow_uuid"),
                "revision_id": item.get("revision_id"),
                "metric_type": str(item.get("metric_type") or "workflow"),
                "step_index": item.get("step_index"),
                "card_uuid": item.get("card_uuid"),
                "card_cursor": item.get("card_cursor"),
                "component_kind": item.get("component_kind"),
                "status": str(item.get("status") or "success"),
                "stopped_reason": item.get("stopped_reason"),
                "latency_ms": float(item.get("latency_ms") or 0.0),
                "started_at": item.get("started_at") or datetime.utcnow(),
                "finished_at": item.get("finished_at") or datetime.utcnow(),
                "details": json.dumps(item.get("details") or {}, ensure_ascii=False),
            }
        )

    await db_session.execute(statement, params)
