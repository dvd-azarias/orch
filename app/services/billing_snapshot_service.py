from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from kombu import Connection, Exchange, Producer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def build_billing_snapshot(
    *,
    workspace_uuid: str,
    session_uuid: str,
    created_at: datetime,
    settings: Settings,
) -> dict[str, Any]:
    created_utc = created_at.astimezone(timezone.utc)
    billing_period = created_utc.strftime("%Y-%m")
    snapshot_id = f"orch_usage_{created_utc.strftime('%Y%m')}_{session_uuid}"
    snapshot_at = created_utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "snapshot_id": snapshot_id,
        "workspace_uuid": workspace_uuid,
        "application_code": settings.orch_billing_application_code,
        "billing_period": billing_period,
        "snapshot_at": snapshot_at,
        "currency": "BRL",
        "correction": False,
        "items": [
            {
                "service_code": settings.orch_billing_service_code,
                "metric_code": settings.orch_billing_metric_code,
                "unit": "event",
                "quantity": 1,
            }
        ],
    }


async def create_billing_snapshot_outbox(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    session_id: int,
    session_uuid: str,
    created_at: datetime | None = None,
    settings: Settings | None = None,
) -> str | None:
    effective_settings = settings or get_settings()
    if not effective_settings.orch_billing_snapshot_enabled:
        return None
    snapshot = build_billing_snapshot(
        workspace_uuid=workspace_uuid,
        session_uuid=session_uuid,
        created_at=created_at or datetime.now(timezone.utc),
        settings=effective_settings,
    )
    await db_session.execute(
        text(
            """
            INSERT INTO orch_billing_usage_snapshots (
                snapshot_id, session_id, session_uuid, payload
            )
            VALUES (
                :snapshot_id, :session_id, CAST(:session_uuid AS uuid), CAST(:payload AS jsonb)
            )
            ON CONFLICT (snapshot_id) DO NOTHING
            """
        ),
        {
            "snapshot_id": snapshot["snapshot_id"],
            "session_id": session_id,
            "session_uuid": session_uuid,
            "payload": json.dumps(snapshot, ensure_ascii=False),
        },
    )
    return str(snapshot["snapshot_id"])


async def try_create_billing_snapshot_outbox(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    session_id: int,
    session_uuid: str,
) -> None:
    try:
        async with db_session.begin_nested():
            await create_billing_snapshot_outbox(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=session_id,
                session_uuid=session_uuid,
            )
    except Exception as exc:
        logger.warning(
            "billing outbox creation failed",
            extra={
                "event": "orch.billing.snapshot.outbox_failed",
                "session_id": session_id,
                "workspace_uuid": workspace_uuid,
                "exception_type": type(exc).__name__,
            },
        )


async def claim_pending_billing_snapshots(
    db_session: AsyncSession,
    *,
    batch_size: int,
    max_attempts: int,
) -> list[dict[str, Any]]:
    result = await db_session.execute(
        text(
            """
            WITH candidates AS (
                SELECT id
                FROM orch_billing_usage_snapshots
                WHERE
                    publish_attempts < :max_attempts
                    AND (
                        status = 'pending'
                        OR (status = 'publishing' AND publish_started_at < NOW() - INTERVAL '5 minutes')
                    )
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT :batch_size
            )
            UPDATE orch_billing_usage_snapshots AS snapshot
            SET
                status = 'publishing',
                publish_started_at = NOW(),
                publish_attempts = publish_attempts + 1,
                updated_at = NOW()
            FROM candidates
            WHERE snapshot.id = candidates.id
            RETURNING snapshot.id, snapshot.snapshot_id, snapshot.session_id, snapshot.payload
            """
        ),
        {"batch_size": max(1, int(batch_size)), "max_attempts": max(1, int(max_attempts))},
    )
    return [dict(row) for row in result.mappings().all()]


async def mark_billing_snapshot_published(db_session: AsyncSession, *, snapshot_id: str) -> None:
    await db_session.execute(
        text(
            """
            UPDATE orch_billing_usage_snapshots
            SET status = 'published', published_at = NOW(), last_error = NULL, updated_at = NOW()
            WHERE snapshot_id = :snapshot_id
            """
        ),
        {"snapshot_id": snapshot_id},
    )


async def mark_billing_snapshot_failed(db_session: AsyncSession, *, snapshot_id: str, error: str) -> None:
    await db_session.execute(
        text(
            """
            UPDATE orch_billing_usage_snapshots
            SET status = 'pending', last_error = :error, updated_at = NOW()
            WHERE snapshot_id = :snapshot_id
            """
        ),
        {"snapshot_id": snapshot_id, "error": error[:500]},
    )


def publish_billing_snapshot(*, snapshot: dict[str, Any], settings: Settings) -> None:
    if not settings.orch_billing_rabbitmq_url:
        raise RuntimeError("ORCH_BILLING_RABBITMQ_URL não configurada")
    exchange = Exchange(settings.orch_billing_exchange, type="topic", durable=True)
    snapshot_id = str(snapshot["snapshot_id"])
    with Connection(
        settings.orch_billing_rabbitmq_url,
        connect_timeout=settings.orch_billing_publish_timeout_seconds,
    ) as connection:
        with connection.channel() as channel:
            Producer(channel).publish(
                snapshot,
                exchange=exchange,
                routing_key=settings.orch_billing_routing_key,
                declare=[exchange],
                serializer="json",
                delivery_mode=2,
                message_id=snapshot_id,
                headers={
                    "messageId": snapshot_id,
                    "source_application": settings.orch_billing_application_code,
                    "schema_version": "v1",
                    "workspace_uuid": str(snapshot["workspace_uuid"]),
                },
                retry=True,
                retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 0},
            )
