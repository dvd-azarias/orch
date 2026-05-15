from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import redis
from sqlalchemy import text

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.core.workspace import normalize_workspace_uuid
from app.repositories.orch_channel_events_repository import list_stale_pending_channel_event_sessions
from app.services.alarm_service import persist_alarm
from app.services.session_metrics_service import persist_session_metrics
from app.services.workspace_service import bind_workspace_context, list_completed_workspaces
from app.services.workflow_dispatcher_service import advance_session_once, dispatch_pending_sessions

logger = get_logger(__name__)


@celery_app.task(name="app.tasks.workflow.advance_session", ignore_result=True)
def advance_session_task(*, workspace_uuid: str, flow_uuid: str, session_id: int) -> None:
    asyncio.run(
        _advance_session_task(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            session_id=session_id,
        )
    )


@celery_app.task(name="app.tasks.workflow.dispatch_pending_sessions", ignore_result=True)
def dispatch_pending_sessions_task() -> dict[str, int]:
    claimed_count = asyncio.run(_dispatch_pending_sessions_task())
    return {"claimed_count": claimed_count}


@celery_app.task(name="app.tasks.workflow.reconcile_pending_channel_events", ignore_result=True)
def reconcile_pending_channel_events_task() -> dict[str, int]:
    enqueued_count = asyncio.run(_reconcile_pending_channel_events_task())
    return {"enqueued_count": enqueued_count}


@celery_app.task(name="app.tasks.workflow.beat_heartbeat", ignore_result=True)
def beat_heartbeat_task() -> None:
    settings = get_settings()
    backend_url = settings.celery_result_backend
    if not backend_url:
        return
    client = redis.Redis.from_url(backend_url)
    now_ts = datetime.now(timezone.utc).timestamp()
    ttl = max(5, settings.celery_health_heartbeat_ttl_seconds)
    client.set(settings.celery_health_heartbeat_key, str(now_ts), ex=ttl)


async def _dispatch_pending_sessions_task() -> int:
    settings = get_settings()
    if not settings.celery_enabled:
        return 0

    task_started_at = datetime.now(timezone.utc)
    session_factory = get_session_factory()
    total_claimed = 0
    async with session_factory() as db_session:
        try:
            workspaces = await list_completed_workspaces(db_session)
        except Exception as exc:
            await persist_alarm(
                db_session,
                level="error",
                code="workflow_dispatch_task_failed",
                message="Falha inesperada no dispatcher assíncrono.",
                details={
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
                app_name="Celery",
            )
            raise

        scoped_workspace_uuid = (
            normalize_workspace_uuid(settings.celery_dispatch_workspace_uuid)
            if settings.celery_dispatch_workspace_uuid
            else None
        )
        if scoped_workspace_uuid is not None:
            logger.info(
                "workflow dispatcher workspace scope enabled",
                extra={
                    "event": "orch.workflow.dispatcher.scope",
                    "workspace_uuid": scoped_workspace_uuid,
                },
            )

        for workspace in workspaces:
            workspace_uuid = normalize_workspace_uuid(str(workspace["workspace_uuid"]))
            if scoped_workspace_uuid is not None and workspace_uuid != scoped_workspace_uuid:
                continue
            bind_workspace_context(workspace_uuid)
            claimed = await dispatch_pending_sessions(db_session)
            total_claimed += len(claimed)
            metrics: list[dict[str, Any]] = []
            for item in claimed:
                flow_uuid = str(item["flow_uuid"])
                session_id = int(item["id"])
                session_uuid = str(item["uuid"])
                pending_since = item.get("pending_since")
                item_started = datetime.now(timezone.utc)
                queue_lag_ms = 0.0
                if isinstance(pending_since, datetime):
                    pending_dt = pending_since if pending_since.tzinfo else pending_since.replace(tzinfo=timezone.utc)
                    queue_lag_ms = max(0.0, (item_started - pending_dt).total_seconds() * 1000)
                try:
                    advance_session_task.delay(
                        workspace_uuid=workspace_uuid,
                        flow_uuid=flow_uuid,
                        session_id=session_id,
                    )
                    status = "success"
                    stopped_reason = None
                except Exception as exc:
                    status = "error"
                    stopped_reason = "enqueue_failed"
                    await persist_alarm(
                        db_session,
                        level="error",
                        code="workflow_dispatch_enqueue_failed",
                        message="Falha ao enfileirar sessão para execução.",
                        details={
                            "session_id": session_id,
                            "workspace_uuid": workspace_uuid,
                            "flow_uuid": flow_uuid,
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                        },
                        flow_uuid=flow_uuid,
                        app_name="Celery",
                        session_uuid=session_uuid,
                    )

                item_finished = datetime.now(timezone.utc)
                metrics.append(
                    {
                        "session_id": session_id,
                        "session_uuid": session_uuid,
                        "flow_uuid": flow_uuid,
                        "revision_id": None,
                        "metric_type": "dispatch",
                        "step_index": None,
                        "card_uuid": None,
                        "card_cursor": None,
                        "component_kind": "dispatcher",
                        "status": status,
                        "stopped_reason": stopped_reason,
                        "latency_ms": queue_lag_ms,
                        "started_at": item_started,
                        "finished_at": item_finished,
                        "details": {
                            "workspace_uuid": workspace_uuid,
                            "queue_lag_ms": round(queue_lag_ms, 2),
                            "dispatch_duration_ms": round((item_finished - item_started).total_seconds() * 1000, 2),
                        },
                    }
                )

            if claimed:
                await persist_session_metrics(db_session, metrics=metrics)

    total_ms = (datetime.now(timezone.utc) - task_started_at).total_seconds() * 1000
    logger.info(
        "workflow dispatch task finished",
        extra={
            "event": "orch.workflow.dispatch.task",
            "claimed_count": total_claimed,
            "duration_ms": round(total_ms, 2),
        },
    )
    return total_claimed


def _reconcile_lock_key(*, workspace_uuid: str, session_id: int) -> str:
    return f"orch:reconcile:pending-events:{workspace_uuid}:{session_id}"


def _try_acquire_reconcile_lock(
    redis_client: redis.Redis | None,
    *,
    workspace_uuid: str,
    session_id: int,
    cooldown_seconds: int,
) -> bool:
    if redis_client is None:
        return True
    try:
        return bool(
            redis_client.set(
                _reconcile_lock_key(workspace_uuid=workspace_uuid, session_id=session_id),
                "1",
                ex=max(1, int(cooldown_seconds)),
                nx=True,
            )
        )
    except Exception:
        logger.exception(
            "workflow pending-events reconcile lock failed",
            extra={
                "event": "orch.workflow.reconcile.lock_failed",
                "workspace_uuid": workspace_uuid,
                "session_id": session_id,
            },
        )
        return True


async def _reconcile_pending_channel_events_task() -> int:
    settings = get_settings()
    if not settings.celery_enabled or not settings.celery_beat_reconcile_pending_events_enabled:
        return 0

    redis_client: redis.Redis | None = None
    if settings.celery_result_backend:
        try:
            redis_client = redis.Redis.from_url(settings.celery_result_backend)
        except Exception:
            logger.exception(
                "workflow pending-events reconcile redis setup failed",
                extra={"event": "orch.workflow.reconcile.redis_setup_failed"},
            )

    scoped_workspace_uuid = (
        normalize_workspace_uuid(settings.celery_reconcile_pending_events_workspace_uuid)
        if settings.celery_reconcile_pending_events_workspace_uuid
        else None
    )
    session_factory = get_session_factory()
    total_enqueued = 0
    async with session_factory() as db_session:
        try:
            workspaces = await list_completed_workspaces(db_session)
        except Exception as exc:
            await persist_alarm(
                db_session,
                level="error",
                code="workflow_reconcile_pending_events_task_failed",
                message="Falha inesperada no reconciliador de eventos pendentes.",
                details={
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
                app_name="Celery",
            )
            raise

        for workspace in workspaces:
            workspace_uuid = normalize_workspace_uuid(str(workspace["workspace_uuid"]))
            if scoped_workspace_uuid is not None and workspace_uuid != scoped_workspace_uuid:
                continue
            _safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)
            safe_schema = workspace_schema.replace('"', '""')
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            stale_sessions = await list_stale_pending_channel_event_sessions(
                db_session,
                stale_seconds=settings.celery_reconcile_pending_events_stale_seconds,
                limit=settings.celery_reconcile_pending_events_batch_size,
            )
            for item in stale_sessions:
                session_id = int(item["session_id"])
                flow_uuid = str(item["flow_uuid"])
                if not _try_acquire_reconcile_lock(
                    redis_client,
                    workspace_uuid=workspace_uuid,
                    session_id=session_id,
                    cooldown_seconds=settings.celery_reconcile_pending_events_cooldown_seconds,
                ):
                    continue
                try:
                    advance_session_task.delay(
                        workspace_uuid=workspace_uuid,
                        flow_uuid=flow_uuid,
                        session_id=session_id,
                    )
                    total_enqueued += 1
                except Exception as exc:
                    await persist_alarm(
                        db_session,
                        level="error",
                        code="workflow_reconcile_pending_events_enqueue_failed",
                        message="Falha ao reenfileirar sessão com eventos pendentes.",
                        details={
                            "session_id": session_id,
                            "workspace_uuid": workspace_uuid,
                            "flow_uuid": flow_uuid,
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                        },
                        flow_uuid=flow_uuid,
                        app_name="Celery",
                    )

    logger.info(
        "workflow pending-events reconcile finished",
        extra={
            "event": "orch.workflow.reconcile.finished",
            "enqueued_count": total_enqueued,
            "workspace_scope": scoped_workspace_uuid,
        },
    )
    return total_enqueued


async def _advance_session_task(*, workspace_uuid: str, flow_uuid: str, session_id: int) -> None:
    settings = get_settings()
    if not settings.celery_enabled:
        return

    started_at = datetime.now(timezone.utc)
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
        bind_workspace_context(safe_workspace_uuid)
        try:
            stopped_reason = await advance_session_once(
                db_session,
                flow_uuid=flow_uuid,
                session_id=session_id,
            )
            status = "success"
        except Exception as exc:
            stopped_reason = "task_exception"
            status = "error"
            await persist_alarm(
                db_session,
                level="error",
                code="workflow_execute_task_failed",
                message="Falha inesperada na execução assíncrona da sessão.",
                details={
                    "session_id": session_id,
                    "flow_uuid": flow_uuid,
                    "workspace_uuid": safe_workspace_uuid,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
                flow_uuid=flow_uuid,
                app_name="Celery",
            )
            raise
        finally:
            finished_at = datetime.now(timezone.utc)
            await persist_session_metrics(
                db_session,
                metrics=[
                    {
                        "session_id": session_id,
                        "session_uuid": None,
                        "flow_uuid": flow_uuid,
                        "revision_id": None,
                        "metric_type": "executor",
                        "step_index": None,
                        "card_uuid": None,
                        "card_cursor": None,
                        "component_kind": "executor",
                        "status": status if "status" in locals() else "error",
                        "stopped_reason": stopped_reason,
                        "latency_ms": round((finished_at - started_at).total_seconds() * 1000, 2),
                        "started_at": started_at,
                        "finished_at": finished_at,
                        "details": {},
                    }
                ],
            )
    logger.info(
        "workflow session advanced",
        extra={
            "event": "orch.workflow.session.advanced",
            "workspace_uuid": workspace_uuid,
            "flow_uuid": flow_uuid,
            "session_id": session_id,
            "stopped_reason": stopped_reason,
        },
    )
