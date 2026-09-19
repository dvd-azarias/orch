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
from app.repositories.orch_sessions_repository import (
    fetch_session_workflow_state,
    replace_session_workflow_state,
)
from app.services.alarm_service import persist_alarm
from app.services.channel_supplier_v2_service import (
    channel_supplier_v2_enabled_for_context,
)
from app.services.dialer_supplier_v2_service import (
    dialer_supplier_v2_enabled_for_context,
)
from app.services.identidade_person_flow_link_service import (
    link_identidade_mailing_to_current_flow,
    link_source_list_membership_mailing_to_current_flow,
)
from app.services.session_metrics_service import persist_session_metrics
from app.services.workspace_service import bind_workspace_context, list_completed_workspaces
from app.services.workflow_dispatcher_service import (
    advance_session_once,
    dispatch_pending_sessions,
    is_terminal_failure_stop_reason,
)

logger = get_logger(__name__)
_IDENTIDADE_PERSON_FLOW_LINK_LOCK_CLASS_ID = 92022
_SOURCE_LIST_MEMBERSHIP_FLOW_LINK_LOCK_CLASS_ID = 92023
_DIALER_SUPPLIER_V2_TERMINAL_LOCK_RETRY_MAX_SECONDS = 30
_CHANNEL_SUPPLIER_V2_CALLBACK_LOCK_RETRY_MAX_SECONDS = 30


@celery_app.task(name="app.tasks.workflow.advance_session", ignore_result=True)
def advance_session_task(*, workspace_uuid: str, flow_uuid: str, session_id: int) -> None:
    asyncio.run(
        _advance_session_task(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            session_id=session_id,
        )
    )


@celery_app.task(
    name="app.tasks.workflow.resume_dialer_supplier_v2_terminal",
    bind=True,
    ignore_result=True,
    max_retries=7,
    acks_late=True,
    reject_on_worker_lost=True,
)
def resume_dialer_supplier_v2_terminal_task(
    self,
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
) -> dict[str, Any]:
    """Resume one terminal Supplier V2 callback without losing lock contention."""

    stopped_reason = asyncio.run(
        _advance_session_task(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            session_id=session_id,
        )
    )
    if stopped_reason != "session_execution_locked":
        return {"status": "completed", "stopped_reason": stopped_reason}

    retries = max(0, int(self.request.retries or 0))
    countdown = min(
        _DIALER_SUPPLIER_V2_TERMINAL_LOCK_RETRY_MAX_SECONDS,
        2**retries,
    )
    logger.warning(
        "dialer Supplier V2 terminal resume delayed by session lock",
        extra={
            "event": "orch.dialer_supplier_v2.terminal_resume.retry",
            "supplier_version": "v2",
            "workspace_uuid": workspace_uuid,
            "flow_uuid": flow_uuid,
            "session_id": session_id,
            "retry": retries + 1,
            "countdown_seconds": countdown,
        },
    )
    raise self.retry(
        exc=RuntimeError("dialer_supplier_v2_terminal_session_locked"),
        countdown=countdown,
    )


@celery_app.task(
    name="app.tasks.workflow.resume_channel_supplier_v2_callback",
    bind=True,
    ignore_result=True,
    max_retries=7,
    acks_late=True,
    reject_on_worker_lost=True,
)
def resume_channel_supplier_v2_callback_task(
    self,
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
) -> dict[str, Any]:
    """Resume one SMS/RCS Supplier V2 callback with bounded lock retries."""

    stopped_reason = asyncio.run(
        _advance_session_task(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            session_id=session_id,
        )
    )
    if stopped_reason != "session_execution_locked":
        return {"status": "completed", "stopped_reason": stopped_reason}

    retries = max(0, int(self.request.retries or 0))
    countdown = min(
        _CHANNEL_SUPPLIER_V2_CALLBACK_LOCK_RETRY_MAX_SECONDS,
        2**retries,
    )
    logger.warning(
        "channel Supplier V2 callback resume delayed by session lock",
        extra={
            "event": "orch.channel_supplier_v2.callback_resume.retry",
            "supplier_version": "v2",
            "workspace_uuid": workspace_uuid,
            "flow_uuid": flow_uuid,
            "session_id": session_id,
            "retry": retries + 1,
            "countdown_seconds": countdown,
        },
    )
    raise self.retry(
        exc=RuntimeError("channel_supplier_v2_callback_session_locked"),
        countdown=countdown,
    )


@celery_app.task(name="app.tasks.workflow.link_identidade_person_mailing", ignore_result=True)
def link_identidade_person_mailing_task(*, workspace_uuid: str, flow_uuid: str, session_id: int) -> None:
    asyncio.run(
        _link_identidade_person_mailing_task(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            session_id=session_id,
        )
    )


@celery_app.task(
    name="app.tasks.workflow.link_source_list_membership_mailing",
    ignore_result=True,
)
def link_source_list_membership_mailing_task(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
) -> None:
    asyncio.run(
        _link_source_list_membership_mailing_task(
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


async def _advance_session_task(
    *, workspace_uuid: str, flow_uuid: str, session_id: int
) -> str | None:
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
            if is_terminal_failure_stop_reason(stopped_reason):
                status = "error"
                await persist_alarm(
                    db_session,
                    level="error",
                    code=f"workflow_m2_{stopped_reason}",
                    message="Sessão encerrada por falha determinística na execução do workflow.",
                    details={
                        "session_id": session_id,
                        "flow_uuid": flow_uuid,
                        "workspace_uuid": safe_workspace_uuid,
                        "stopped_reason": stopped_reason,
                    },
                    flow_uuid=flow_uuid,
                    app_name="Celery",
                )
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
        if db_session.in_transaction():
            await db_session.commit()
    if stopped_reason == "blocked_switch_bot_flow":
        from app.tasks.switch_bot_flow_tasks import process_switch_bot_flow_task

        process_switch_bot_flow_task.apply_async(
            kwargs={
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
            },
            queue=settings.celery_switch_bot_flow_queue,
            routing_key=settings.celery_switch_bot_flow_queue,
        )
    elif stopped_reason == "blocked_identidade_person_flow_link":
        link_identidade_person_mailing_task.apply_async(
            kwargs={
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
            },
            queue=settings.celery_execute_queue,
            routing_key=settings.celery_execute_queue,
        )
    elif stopped_reason == "blocked_source_list_membership_flow_link":
        link_source_list_membership_mailing_task.apply_async(
            kwargs={
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
            },
            queue=settings.celery_execute_queue,
            routing_key=settings.celery_execute_queue,
        )
    elif (
        stopped_reason == "blocked_send_with_dialer_handoff"
        and dialer_supplier_v2_enabled_for_context(
            settings=settings,
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
        )
    ):
        from app.tasks.dialer_supplier_v2_tasks import (
            register_dialer_supplier_v2_cycle_task,
        )

        register_dialer_supplier_v2_cycle_task.apply_async(
            kwargs={
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
            },
            queue=settings.celery_dialer_supplier_v2_queue,
            routing_key=settings.celery_dialer_supplier_v2_queue,
        )
    elif (
        stopped_reason in {"blocked_send_with_sms", "blocked_send_with_rcs"}
        and channel_supplier_v2_enabled_for_context(
            settings=settings,
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
        )
    ):
        from app.tasks.channel_supplier_v2_tasks import (
            register_channel_supplier_v2_dispatch_task,
        )

        register_channel_supplier_v2_dispatch_task.apply_async(
            kwargs={
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
            },
            queue=settings.celery_channel_supplier_v2_queue,
            routing_key=settings.celery_channel_supplier_v2_queue,
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
    return stopped_reason


async def _link_identidade_person_mailing_task(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
) -> None:
    settings = get_settings()
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    _safe_workspace_uuid, workspace_schema = bind_workspace_context(safe_workspace_uuid)
    safe_schema = workspace_schema.replace('"', '""')
    session_factory = get_session_factory()
    should_advance = False
    outcome = "ignored"

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            lock_result = await db_session.execute(
                text("SELECT pg_try_advisory_xact_lock(:class_id, :object_id) AS locked"),
                {"class_id": _IDENTIDADE_PERSON_FLOW_LINK_LOCK_CLASS_ID, "object_id": int(session_id)},
            )
            if not bool(lock_result.scalar_one()):
                return

            session_state = await fetch_session_workflow_state(db_session, session_id=session_id)
            if session_state is None or str(session_state.get("flow_uuid") or "") != str(flow_uuid):
                return
            runtime_variables = session_state.get("runtime_variables")
            if not isinstance(runtime_variables, dict):
                return
            workflow_meta = runtime_variables.get("workflow_v2")
            if not isinstance(workflow_meta, dict):
                return
            link_state = workflow_meta.get("identidade_person_flow_link")
            if not isinstance(link_state, dict) or str(link_state.get("status") or "").lower() != "pending":
                return

            mailing_uuid = str(link_state.get("mailing_uuid") or "").strip()
            try:
                result = await link_identidade_mailing_to_current_flow(
                    settings=settings,
                    workspace_uuid=safe_workspace_uuid,
                    flow_uuid=flow_uuid,
                    mailing_uuid=mailing_uuid,
                )
                now_iso = datetime.now(timezone.utc).isoformat()
                link_state["attempts"] = result.attempts
                link_state["status_code"] = result.status_code
                link_state["completed_at"] = now_iso
                if result.success:
                    link_state["status"] = "completed"
                    link_state["last_error"] = None
                    outcome = "completed"
                else:
                    link_state["status"] = "failed"
                    link_state["last_error"] = {
                        "code": f"identidade_person_flow_link_{result.reason}",
                        "message": result.message or "Falha ao vincular a lista ao fluxo atual.",
                        "status_code": result.status_code,
                        "updated_at": now_iso,
                    }
                    outcome = "failed"
            except Exception as exc:  # pragma: no cover - proteção final da tarefa
                now_iso = datetime.now(timezone.utc).isoformat()
                link_state.update(
                    {
                        "status": "failed",
                        "completed_at": now_iso,
                        "last_error": {
                            "code": "identidade_person_flow_link_unexpected_error",
                            "message": "Falha inesperada ao vincular a lista ao fluxo atual.",
                            "updated_at": now_iso,
                        },
                    }
                )
                outcome = type(exc).__name__

            await replace_session_workflow_state(
                db_session,
                session_id=session_id,
                runtime_variables=runtime_variables,
                last_card_uuid=session_state.get("last_card_uuid"),
                next_card_uuid=session_state.get("next_card_uuid"),
            )
            should_advance = True

    if should_advance:
        advance_session_task.apply_async(
            kwargs={
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
            },
            queue=settings.celery_execute_queue,
            routing_key=settings.celery_execute_queue,
        )
    logger.info(
        "identidade person mailing link processed",
        extra={
            "event": "orch.identidade_person.flow_link.processed",
            "workspace_uuid": safe_workspace_uuid,
            "flow_uuid": flow_uuid,
            "session_id": session_id,
            "outcome": outcome,
        },
    )


async def _link_source_list_membership_mailing_task(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
) -> None:
    settings = get_settings()
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    _safe_workspace_uuid, workspace_schema = bind_workspace_context(
        safe_workspace_uuid
    )
    safe_schema = workspace_schema.replace('"', '""')
    session_factory = get_session_factory()
    should_advance = False
    outcome = "ignored"

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            lock_result = await db_session.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock(:class_id, :object_id) AS locked"
                ),
                {
                    "class_id": _SOURCE_LIST_MEMBERSHIP_FLOW_LINK_LOCK_CLASS_ID,
                    "object_id": int(session_id),
                },
            )
            if not bool(lock_result.scalar_one()):
                return

            session_state = await fetch_session_workflow_state(
                db_session,
                session_id=session_id,
            )
            if session_state is None or str(session_state.get("flow_uuid") or "") != str(
                flow_uuid
            ):
                return
            runtime_variables = session_state.get("runtime_variables")
            if not isinstance(runtime_variables, dict):
                return
            workflow_meta = runtime_variables.get("workflow_v2")
            if not isinstance(workflow_meta, dict):
                return
            link_state = workflow_meta.get("source_list_membership_flow_link")
            if (
                not isinstance(link_state, dict)
                or str(link_state.get("status") or "").lower() != "pending"
            ):
                return

            mailing_uuid = str(link_state.get("mailing_uuid") or "").strip()
            try:
                result = (
                    await link_source_list_membership_mailing_to_current_flow(
                        settings=settings,
                        workspace_uuid=safe_workspace_uuid,
                        flow_uuid=flow_uuid,
                        mailing_uuid=mailing_uuid,
                    )
                )
                now_iso = datetime.now(timezone.utc).isoformat()
                link_state["attempts"] = result.attempts
                link_state["status_code"] = result.status_code
                link_state["completed_at"] = now_iso
                if result.success:
                    link_state["status"] = "completed"
                    link_state["last_error"] = None
                    outcome = "completed"
                else:
                    link_state["status"] = "failed"
                    link_state["last_error"] = {
                        "code": f"source_list_membership_flow_link_{result.reason}",
                        "message": (
                            result.message
                            or "Falha ao disponibilizar a pessoa para uso neste fluxo."
                        ),
                        "status_code": result.status_code,
                        "updated_at": now_iso,
                    }
                    outcome = "failed"
            except Exception as exc:  # pragma: no cover - proteção final da tarefa
                now_iso = datetime.now(timezone.utc).isoformat()
                link_state.update(
                    {
                        "status": "failed",
                        "completed_at": now_iso,
                        "last_error": {
                            "code": "source_list_membership_flow_link_unexpected_error",
                            "message": (
                                "Falha inesperada ao disponibilizar a pessoa para uso "
                                "neste fluxo."
                            ),
                            "updated_at": now_iso,
                        },
                    }
                )
                outcome = type(exc).__name__

            await replace_session_workflow_state(
                db_session,
                session_id=session_id,
                runtime_variables=runtime_variables,
                last_card_uuid=session_state.get("last_card_uuid"),
                next_card_uuid=session_state.get("next_card_uuid"),
            )
            should_advance = True

    if should_advance:
        advance_session_task.apply_async(
            kwargs={
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
            },
            queue=settings.celery_execute_queue,
            routing_key=settings.celery_execute_queue,
        )
    logger.info(
        "source list membership flow link processed",
        extra={
            "event": "orch.source_list_membership.flow_link.processed",
            "workspace_uuid": safe_workspace_uuid,
            "flow_uuid": flow_uuid,
            "session_id": session_id,
            "outcome": outcome,
        },
    )
