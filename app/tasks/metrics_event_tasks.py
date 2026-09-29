from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.core.workspace import normalize_workspace_uuid
from app.services.metrics_event_outbox_service import (
    claim_metrics_events,
    mark_metrics_events_failed,
    mark_metrics_events_published,
    metrics_retry_delay_seconds,
    publish_metrics_events_batch,
)
from app.services.workspace_service import bind_workspace_context, list_completed_workspaces


logger = get_logger(__name__)


@celery_app.task(
    name="app.tasks.metrics_events.publish_pending",
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def publish_pending_metrics_events_task() -> dict[str, int]:
    return asyncio.run(_publish_pending_metrics_events())


async def _publish_pending_metrics_events() -> dict[str, int]:
    settings = get_settings()
    if not settings.orch_metrics_events_enabled:
        return {
            "workspaces_scanned": 0,
            "claimed": 0,
            "published": 0,
            "retried": 0,
            "dead": 0,
        }

    allowed_workspaces = {
        normalize_workspace_uuid(value)
        for value in settings.orch_metrics_events_workspace_allowlist
    }
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        workspaces = await list_completed_workspaces(db_session)
        if db_session.in_transaction():
            await db_session.commit()

    counters = {
        "workspaces_scanned": 0,
        "claimed": 0,
        "published": 0,
        "retried": 0,
        "dead": 0,
    }
    for workspace in workspaces:
        workspace_uuid = normalize_workspace_uuid(str(workspace["workspace_uuid"]))
        if workspace_uuid not in allowed_workspaces:
            continue
        counters["workspaces_scanned"] += 1
        _safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)
        safe_schema = workspace_schema.replace('"', '""')
        claim_token = str(uuid4())
        try:
            async with session_factory() as db_session:
                async with db_session.begin():
                    await db_session.execute(
                        text(f'SET LOCAL search_path TO "{safe_schema}"')
                    )
                    claimed = await claim_metrics_events(
                        db_session,
                        claim_token=claim_token,
                        batch_size=settings.orch_metrics_events_batch_size,
                        lease_seconds=settings.orch_metrics_events_lease_seconds,
                        max_attempts=settings.orch_metrics_events_max_attempts,
                    )
            if not claimed:
                continue
            counters["claimed"] += len(claimed)
            result = await asyncio.to_thread(
                publish_metrics_events_batch,
                events=[item.envelope for item in claimed],
                claim_token=claim_token,
                settings=settings,
            )
            item_ids = [item.id for item in claimed]
            async with session_factory() as db_session:
                async with db_session.begin():
                    await db_session.execute(
                        text(f'SET LOCAL search_path TO "{safe_schema}"')
                    )
                    if result.success:
                        counters["published"] += await mark_metrics_events_published(
                            db_session,
                            claim_token=claim_token,
                            item_ids=item_ids,
                            http_status=int(result.status_code or 200),
                        )
                    else:
                        delay_seconds = metrics_retry_delay_seconds(
                            attempt=max(item.attempts for item in claimed),
                            initial_seconds=(
                                settings.orch_metrics_events_retry_initial_seconds
                            ),
                            maximum_seconds=(
                                settings.orch_metrics_events_retry_max_seconds
                            ),
                        )
                        retried, dead = await mark_metrics_events_failed(
                            db_session,
                            claim_token=claim_token,
                            item_ids=item_ids,
                            retryable=result.retryable,
                            retry_delay_seconds=delay_seconds,
                            max_attempts=settings.orch_metrics_events_max_attempts,
                            http_status=result.status_code,
                            error=(
                                f"metrics_http_{result.status_code}"
                                if result.status_code is not None
                                else "metrics_network_error"
                            ),
                        )
                        counters["retried"] += retried
                        counters["dead"] += dead
            logger.info(
                "metrics events batch processed",
                extra={
                    "event": "orch.metrics_events.batch_processed",
                    "workspace_uuid": workspace_uuid,
                    "claimed": len(claimed),
                    "http_status": result.status_code,
                    "success": result.success,
                    "retryable": result.retryable,
                },
            )
        except Exception as exc:
            logger.exception(
                "metrics events workspace publication failed",
                extra={
                    "event": "orch.metrics_events.workspace_failed",
                    "workspace_uuid": workspace_uuid,
                    "exception_type": type(exc).__name__,
                },
            )
            # A lease libera claims abandonados sem tocar no runtime funcional.
            continue
    return counters
