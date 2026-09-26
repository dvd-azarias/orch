from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import redis
from sqlalchemy import text

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.core.workspace import normalize_workspace_uuid
from app.services.journey_workspace_aggregation_service import (
    build_journey_workspace_snapshot_payload,
)
from app.services.journey_dashboard_websocket_service import (
    journey_workspace_notification_channel,
)
from app.services.journey_workspace_snapshot_service import (
    JourneyWorkspaceSnapshotClaim,
    claim_journey_workspace_snapshot,
    cleanup_expired_journey_facts,
    fail_claimed_journey_workspace_snapshot,
    journey_workspace_snapshot_is_due,
    mark_journey_workspace_snapshot_notified,
    store_claimed_journey_workspace_snapshot,
)
from app.services.workspace_service import bind_workspace_context, list_completed_workspaces


logger = get_logger(__name__)


@celery_app.task(
    name="app.tasks.journey_dashboard.scan_dirty_workspaces",
    ignore_result=True,
)
def scan_dirty_journey_workspaces_task() -> dict[str, int]:
    return asyncio.run(_scan_dirty_journey_workspaces())


@celery_app.task(
    name="app.tasks.journey_dashboard.build_workspace_snapshot",
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def build_journey_workspace_snapshot_task(
    *,
    workspace_uuid: str,
    workspace_name: str,
) -> dict[str, Any]:
    return asyncio.run(
        _build_journey_workspace_snapshot(
            workspace_uuid=workspace_uuid,
            workspace_name=workspace_name,
        )
    )


async def _scan_dirty_journey_workspaces() -> dict[str, int]:
    settings = get_settings()
    if not (
        settings.celery_enabled
        and settings.orch_journey_dashboard_enabled
        and settings.celery_beat_journey_snapshot_enabled
    ):
        return {"workspaces_scanned": 0, "workspaces_enqueued": 0}

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        workspaces = await list_completed_workspaces(db_session)
        if db_session.in_transaction():
            await db_session.commit()

    workspace_scope = (
        normalize_workspace_uuid(settings.celery_journey_snapshot_workspace_uuid)
        if settings.celery_journey_snapshot_workspace_uuid
        else None
    )
    scanned = 0
    enqueued = 0
    checked_at = datetime.now(UTC)
    for workspace in workspaces:
        workspace_uuid = normalize_workspace_uuid(str(workspace["workspace_uuid"]))
        if workspace_scope is not None and workspace_uuid != workspace_scope:
            continue
        scanned += 1
        _safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)
        safe_schema = workspace_schema.replace('"', '""')
        try:
            async with session_factory() as db_session:
                async with db_session.begin():
                    await db_session.execute(
                        text(f'SET LOCAL search_path TO "{safe_schema}"')
                    )
                    is_due = await journey_workspace_snapshot_is_due(
                        db_session,
                        checked_at=checked_at,
                    )
        except Exception as exc:
            logger.warning(
                "journey dashboard workspace scan failed",
                extra={
                    "event": "orch.journey_dashboard.scan.workspace_failed",
                    "workspace_uuid": workspace_uuid,
                    "exception_type": type(exc).__name__,
                },
            )
            continue
        if not is_due:
            continue
        build_journey_workspace_snapshot_task.apply_async(
            kwargs={
                "workspace_uuid": workspace_uuid,
                "workspace_name": str(workspace.get("name") or workspace_uuid),
            },
            queue=settings.celery_journey_snapshot_queue,
            routing_key=settings.celery_journey_snapshot_queue,
        )
        enqueued += 1

    logger.info(
        "journey dashboard dirty workspaces scanned",
        extra={
            "event": "orch.journey_dashboard.scan.finished",
            "workspaces_scanned": scanned,
            "workspaces_enqueued": enqueued,
        },
    )
    return {"workspaces_scanned": scanned, "workspaces_enqueued": enqueued}


async def _build_journey_workspace_snapshot(
    *,
    workspace_uuid: str,
    workspace_name: str,
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.orch_journey_dashboard_enabled:
        return {"status": "disabled"}

    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    _safe_workspace_uuid, workspace_schema = bind_workspace_context(
        safe_workspace_uuid
    )
    safe_schema = workspace_schema.replace('"', '""')
    session_factory = get_session_factory()
    claim: JourneyWorkspaceSnapshotClaim | None = None
    claimed_at = datetime.now(UTC)
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            claim = await claim_journey_workspace_snapshot(
                db_session,
                claimed_at=claimed_at,
                lease_seconds=settings.orch_journey_snapshot_lease_seconds,
            )
    if claim is None:
        return {"status": "not_due"}

    snapshot_id = uuid4()
    generated_at = datetime.now(UTC)
    try:
        async with session_factory() as db_session:
            async with db_session.begin():
                await db_session.execute(
                    text(f'SET LOCAL search_path TO "{safe_schema}"')
                )
                payload = await build_journey_workspace_snapshot_payload(
                    db_session,
                    workspace_uuid=safe_workspace_uuid,
                    workspace_name=workspace_name,
                    generated_at=generated_at,
                )
                snapshot = {
                    "type": "orchestration_workspace_snapshot",
                    "meta": {
                        "version": "1.0",
                        "snapshot_id": str(snapshot_id),
                        "snapshot_sequence": claim.next_snapshot_sequence,
                        "generated_at": generated_at.isoformat().replace(
                            "+00:00", "Z"
                        ),
                        "reason": "update",
                        "source_application": "orch",
                    },
                    "payload": payload,
                }
                stored = await store_claimed_journey_workspace_snapshot(
                    db_session,
                    claim=claim,
                    snapshot=snapshot,
                    snapshot_id=snapshot_id,
                    built_at=generated_at,
                )
                if stored.snapshot_sequence != claim.next_snapshot_sequence:
                    raise RuntimeError("journey_snapshot_sequence_mismatch")
                expired_removed = await cleanup_expired_journey_facts(
                    db_session,
                    cleaned_at=generated_at,
                )
    except Exception as exc:
        try:
            async with session_factory() as db_session:
                async with db_session.begin():
                    await db_session.execute(
                        text(f'SET LOCAL search_path TO "{safe_schema}"')
                    )
                    await fail_claimed_journey_workspace_snapshot(
                        db_session,
                        claim=claim,
                        error_code=f"snapshot_build_{type(exc).__name__}"[:120],
                        failed_at=datetime.now(UTC),
                    )
        except Exception:
            logger.exception(
                "journey dashboard failed to release snapshot claim",
                extra={
                    "event": "orch.journey_dashboard.snapshot.claim_release_failed",
                    "workspace_uuid": safe_workspace_uuid,
                },
            )
        logger.exception(
            "journey dashboard snapshot build failed",
            extra={
                "event": "orch.journey_dashboard.snapshot.failed",
                "workspace_uuid": safe_workspace_uuid,
                "claim_generation": claim.generation,
            },
        )
        return {"status": "failed", "error_code": type(exc).__name__}

    notified = await _publish_snapshot_notification(
        redis_url=settings.orch_journey_redis_url,
        workspace_uuid=safe_workspace_uuid,
        snapshot_sequence=stored.snapshot_sequence,
    )
    if notified:
        try:
            async with session_factory() as db_session:
                async with db_session.begin():
                    await db_session.execute(
                        text(f'SET LOCAL search_path TO "{safe_schema}"')
                    )
                    await mark_journey_workspace_snapshot_notified(
                        db_session,
                        snapshot_sequence=stored.snapshot_sequence,
                        notified_at=datetime.now(UTC),
                    )
        except Exception:
            logger.exception(
                "journey dashboard notification marker failed",
                extra={
                    "event": "orch.journey_dashboard.snapshot.notification_marker_failed",
                    "workspace_uuid": safe_workspace_uuid,
                    "snapshot_sequence": stored.snapshot_sequence,
                },
            )

    logger.info(
        "journey dashboard snapshot stored",
        extra={
            "event": "orch.journey_dashboard.snapshot.stored",
            "workspace_uuid": safe_workspace_uuid,
            "snapshot_sequence": stored.snapshot_sequence,
            "snapshot_bytes": stored.snapshot_bytes,
            "remains_dirty": stored.remains_dirty,
            "redis_notified": notified,
            "expired_removed": expired_removed,
        },
    )
    return {
        "status": "stored",
        "snapshot_id": str(stored.snapshot_id),
        "snapshot_sequence": stored.snapshot_sequence,
        "snapshot_bytes": stored.snapshot_bytes,
        "remains_dirty": stored.remains_dirty,
        "redis_notified": notified,
        "expired_removed": expired_removed,
    }


async def _publish_snapshot_notification(
    *,
    redis_url: str | None,
    workspace_uuid: str,
    snapshot_sequence: int,
) -> bool:
    safe_url = str(redis_url or "").strip()
    if not safe_url:
        return False

    message = json.dumps(
        {
            "workspace_uuid": normalize_workspace_uuid(workspace_uuid),
            "snapshot_sequence": int(snapshot_sequence),
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    def _publish() -> None:
        client = redis.Redis.from_url(
            safe_url,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        try:
            client.publish(
                journey_workspace_notification_channel(workspace_uuid),
                message,
            )
        finally:
            client.close()

    try:
        await asyncio.to_thread(_publish)
        return True
    except Exception as exc:
        logger.warning(
            "journey dashboard redis notification failed",
            extra={
                "event": "orch.journey_dashboard.snapshot.redis_failed",
                "workspace_uuid": normalize_workspace_uuid(workspace_uuid),
                "snapshot_sequence": int(snapshot_sequence),
                "exception_type": type(exc).__name__,
            },
        )
        return False
