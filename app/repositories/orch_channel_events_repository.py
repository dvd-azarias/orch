from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

logger = get_logger(__name__)

async def insert_channel_event(
    db_session: AsyncSession,
    *,
    session_id: int,
    flow_uuid: str,
    channel: str,
    event_type: str,
    event_id: str | None,
    event_ts: datetime | None,
    payload: dict[str, Any],
) -> None:
    await db_session.execute(
        text(
            """
            INSERT INTO orch_channel_events (
                session_id,
                flow_uuid,
                channel,
                event_type,
                event_id,
                event_ts,
                payload,
                received_at,
                created_at
            ) VALUES (
                :session_id,
                CAST(:flow_uuid AS uuid),
                :channel,
                :event_type,
                :event_id,
                CAST(:event_ts AS timestamptz),
                CAST(:payload AS jsonb),
                NOW(),
                NOW()
            )
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "session_id": session_id,
            "flow_uuid": flow_uuid,
            "channel": channel,
            "event_type": event_type,
            "event_id": event_id,
            "event_ts": event_ts,
            "payload": json.dumps(payload, ensure_ascii=False),
        },
    )


async def has_pending_channel_events(
    db_session: AsyncSession,
    *,
    session_id: int,
    channel: str,
) -> bool:
    try:
        result = await db_session.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM orch_channel_events
                    WHERE session_id = :session_id
                      AND channel = :channel
                      AND processed_at IS NULL
                ) AS has_pending
                """
            ),
            {"session_id": session_id, "channel": channel},
        )
        return bool(result.scalar())
    except Exception:
        logger.exception(
            "failed to check pending channel events",
            extra={"event": "orch.channel_events.pending_check_failed", "session_id": session_id, "channel": channel},
        )
        return False


async def claim_next_pending_channel_event(
    db_session: AsyncSession,
    *,
    session_id: int,
    channel: str,
) -> dict[str, Any] | None:
    try:
        result = await db_session.execute(
            text(
                """
                WITH candidate AS (
                    SELECT e.id
                    FROM orch_channel_events e
                    WHERE e.session_id = :session_id
                      AND e.channel = :channel
                      AND e.processed_at IS NULL
                    ORDER BY
                        CASE
                            WHEN :channel = 'whatsapp' THEN
                                CASE e.event_type
                                    WHEN 'sent' THEN 1
                                    WHEN 'delivered' THEN 2
                                    WHEN 'read' THEN 3
                                    WHEN 'failed' THEN 4
                                    WHEN 'limit_reached' THEN 5
                                    ELSE 99
                                END
                            ELSE 50
                        END,
                        COALESCE(e.event_ts, e.received_at),
                        e.id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE orch_channel_events e
                SET
                    processed_at = NOW()
                FROM candidate
                WHERE e.id = candidate.id
                RETURNING
                    e.id,
                    e.channel,
                    e.event_type,
                    e.event_id,
                    e.event_ts,
                    e.payload
                """
            ),
            {"session_id": session_id, "channel": channel},
        )
        row = result.mappings().first()
        return dict(row) if row is not None else None
    except Exception:
        logger.exception(
            "failed to claim pending channel event",
            extra={"event": "orch.channel_events.claim_failed", "session_id": session_id, "channel": channel},
        )
        return None
