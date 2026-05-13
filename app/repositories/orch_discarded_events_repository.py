from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def insert_discarded_event(
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
    request_id: str | None,
) -> None:
    await db_session.execute(
        text(
            """
            INSERT INTO orch_discarded_events (
                flow_uuid,
                app_name,
                entity,
                entity_type,
                entity_address,
                entity_session_id,
                discard_reason,
                payload,
                request_id,
                created_at
            ) VALUES (
                CAST(:flow_uuid AS uuid),
                :app_name,
                :entity,
                :entity_type,
                :entity_address,
                :entity_session_id,
                :discard_reason,
                CAST(:payload AS jsonb),
                :request_id,
                NOW()
            )
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "app_name": app_name,
            "entity": entity,
            "entity_type": entity_type,
            "entity_address": entity_address,
            "entity_session_id": entity_session_id,
            "discard_reason": discard_reason,
            "payload": json.dumps(payload, ensure_ascii=False),
            "request_id": request_id,
        },
    )
