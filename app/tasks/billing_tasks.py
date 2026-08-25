from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import text

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.services.billing_snapshot_service import (
    claim_pending_billing_snapshots,
    mark_billing_snapshot_failed,
    mark_billing_snapshot_published,
    publish_billing_snapshot,
)
from app.services.workspace_service import bind_workspace_context, list_completed_workspaces
from app.core.workspace import workspace_schema_from_uuid

logger = get_logger(__name__)


@celery_app.task(name="app.tasks.billing.publish_pending_snapshots", ignore_result=True)
def publish_pending_billing_snapshots_task() -> dict[str, int]:
    return asyncio.run(_publish_pending_billing_snapshots_task())


async def _publish_pending_billing_snapshots_task() -> dict[str, int]:
    settings = get_settings()
    if not settings.orch_billing_snapshot_enabled:
        return {"published": 0, "failed": 0}

    published = 0
    failed = 0
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        workspaces = await list_completed_workspaces(db_session)
        if db_session.in_transaction():
            await db_session.commit()
        for workspace in workspaces:
            workspace_uuid = str(workspace["workspace_uuid"])
            bind_workspace_context(workspace_uuid)
            async with db_session.begin():
                safe_schema = workspace_schema_from_uuid(workspace_uuid).replace('"', '""')
                await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
                snapshots = await claim_pending_billing_snapshots(
                    db_session,
                    batch_size=settings.orch_billing_publish_batch_size,
                    max_attempts=settings.orch_billing_publish_max_attempts,
                )
            for item in snapshots:
                snapshot = dict(item["payload"])
                snapshot_id = str(item["snapshot_id"])
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            publish_billing_snapshot,
                            snapshot=snapshot,
                            settings=settings,
                        ),
                        timeout=settings.orch_billing_publish_timeout_seconds + 1,
                    )
                except Exception as exc:
                    failed += 1
                    async with db_session.begin():
                        safe_schema = workspace_schema_from_uuid(workspace_uuid).replace('"', '""')
                        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
                        await mark_billing_snapshot_failed(
                            db_session,
                            snapshot_id=snapshot_id,
                            error=type(exc).__name__,
                        )
                    logger.warning(
                        "billing snapshot publication failed",
                        extra={
                            "event": "orch.billing.snapshot.failed",
                            "session_id": int(item["session_id"]),
                            "snapshot_id": snapshot_id,
                            "workspace_uuid": workspace_uuid,
                            "exchange": settings.orch_billing_exchange,
                            "routing_key": settings.orch_billing_routing_key,
                            "exception_type": type(exc).__name__,
                        },
                    )
                    continue
                async with db_session.begin():
                    safe_schema = workspace_schema_from_uuid(workspace_uuid).replace('"', '""')
                    await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
                    await mark_billing_snapshot_published(db_session, snapshot_id=snapshot_id)
                published += 1
                logger.info(
                    "billing snapshot published",
                    extra={
                        "event": "orch.billing.snapshot.published",
                        "session_id": int(item["session_id"]),
                        "snapshot_id": snapshot_id,
                        "workspace_uuid": workspace_uuid,
                        "exchange": settings.orch_billing_exchange,
                        "routing_key": settings.orch_billing_routing_key,
                    },
                )
    return {"published": published, "failed": failed}
