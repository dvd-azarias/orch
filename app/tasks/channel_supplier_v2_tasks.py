from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.repositories.orch_sessions_repository import (
    fetch_session_workflow_state,
    patch_session_channel_supplier_v2_registration,
)
from app.services.alarm_service import persist_alarm
from app.services.channel_supplier_v2_service import (
    ChannelSupplierV2RegistrationError,
    channel_supplier_v2_enabled_for_context,
    get_channel_dispatch,
    parse_channel_dispatch_intent,
    register_channel_dispatch,
)
from app.services.workspace_service import bind_workspace_context, list_completed_workspaces


logger = get_logger(__name__)
_WORKFLOW_LOCK_CLASS_ID = 92041
_PENDING_RETRY_RECOVERY_SECONDS = 300


@celery_app.task(
    name="app.tasks.channel_supplier_v2.register_dispatch",
    bind=True,
    ignore_result=True,
    max_retries=7,
    acks_late=True,
    reject_on_worker_lost=True,
)
def register_channel_supplier_v2_dispatch_task(
    self,
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
    recovery_attempt: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    attempt = max(1, int(recovery_attempt or 1)) + int(self.request.retries or 0)
    result = asyncio.run(
        _register_channel_supplier_v2_dispatch_task(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            session_id=session_id,
            attempt=attempt,
        )
    )
    if result.get("status") != "retry":
        return result
    countdown = min(
        300.0,
        float(settings.channel_supplier_v2_retry_backoff_seconds)
        * (2 ** max(0, attempt - 1)),
    )
    raise self.retry(
        exc=RuntimeError(
            f"channel_supplier_v2_registration_retry:{result.get('reason')}"
        ),
        countdown=countdown,
    )


@celery_app.task(
    name="app.tasks.channel_supplier_v2.reconcile_pending_dispatches",
    ignore_result=True,
)
def reconcile_pending_channel_supplier_v2_dispatches_task() -> dict[str, int]:
    return asyncio.run(_reconcile_pending_channel_supplier_v2_dispatches_task())


async def _register_channel_supplier_v2_dispatch_task(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
    attempt: int,
) -> dict[str, Any]:
    settings = get_settings()
    if not channel_supplier_v2_enabled_for_context(
        settings=settings,
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
    ):
        return {"status": "disabled"}
    prepared = await _claim_registration(
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
        session_id=session_id,
        attempt=attempt,
        registration_lease_seconds=int(
            settings.channel_supplier_v2_registration_lease_seconds
        ),
    )
    if prepared.get("status") == "registered":
        return await _sync_registered_dispatch(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            session_id=session_id,
            intent=prepared["intent"],
            settings=settings,
        )
    if prepared.get("status") != "claimed":
        return prepared
    intent = prepared["intent"]
    effective_attempt = int(prepared.get("attempt") or attempt)
    try:
        result = await asyncio.to_thread(
            register_channel_dispatch,
            workspace_uuid=workspace_uuid,
            intent=intent,
            settings=settings,
        )
    except ChannelSupplierV2RegistrationError as exc:
        will_retry = (
            exc.retryable
            and effective_attempt < int(settings.channel_supplier_v2_max_attempts)
        )
        await _store_error(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            session_id=session_id,
            intent=intent,
            attempt=effective_attempt,
            error=exc,
            will_retry=will_retry,
        )
        return {
            "status": "retry" if will_retry else "failed",
            "reason": exc.code,
        }
    except Exception as exc:  # pragma: no cover - final integration guard
        logger.exception(
            "unexpected channel Supplier V2 registration failure",
            extra={
                "event": "orch.channel_supplier_v2.registration.unexpected",
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
                "exception_type": type(exc).__name__,
            },
        )
        wrapped = ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_unexpected_error",
            "Falha inesperada ao registrar o dispatch na Supplier V2.",
            retryable=True,
        )
        will_retry = effective_attempt < int(settings.channel_supplier_v2_max_attempts)
        await _store_error(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            session_id=session_id,
            intent=intent,
            attempt=effective_attempt,
            error=wrapped,
            will_retry=will_retry,
        )
        return {
            "status": "retry" if will_retry else "failed",
            "reason": wrapped.code,
        }

    completed_at = datetime.now(timezone.utc).isoformat()
    registration = dict(intent)
    registration.update(result.runtime_payload())
    registration.update(
        {
            "status": "registered",
            "attempts": max(int(intent.get("attempts") or 0), effective_attempt),
            "registered_at": completed_at,
            "updated_at": completed_at,
            "last_error": None,
        }
    )
    stored = await _patch_registration(
        workspace_uuid=workspace_uuid,
        session_id=session_id,
        idempotency_key=str(intent["idempotency_key"]),
        registration=registration,
    )
    if not stored:
        return {"status": "stale"}
    logger.info(
        "channel Supplier V2 dispatch registered",
        extra={
            "event": "orch.channel_supplier_v2.registration.ready",
            "workspace_uuid": workspace_uuid,
            "flow_uuid": flow_uuid,
            "session_id": session_id,
            "dispatch_id": result.dispatch_id,
            "channel": intent.get("channel"),
            "component_ref_id": intent.get("component_ref_id"),
            "replayed": result.replayed,
        },
    )
    return await _sync_registered_dispatch(
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
        session_id=session_id,
        intent=registration,
        settings=settings,
    )


async def _claim_registration(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
    attempt: int,
    registration_lease_seconds: int,
) -> dict[str, Any]:
    _safe_workspace_uuid, schema = bind_workspace_context(workspace_uuid)
    safe_schema = schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            locked = await db_session.execute(
                text("SELECT pg_try_advisory_xact_lock(:class_id, :object_id)"),
                {"class_id": _WORKFLOW_LOCK_CLASS_ID, "object_id": int(session_id)},
            )
            if not bool(locked.scalar_one()):
                return {"status": "in_progress"}
            state = await fetch_session_workflow_state(db_session, session_id=session_id)
            if state is None:
                return {"status": "missing_session"}
            if str(state.get("flow_uuid") or "") != str(flow_uuid):
                return {"status": "flow_mismatch"}
            runtime = state.get("runtime_variables")
            workflow = runtime.get("workflow_v2") if isinstance(runtime, dict) else None
            raw = workflow.get("channel_dispatch_v2") if isinstance(workflow, dict) else None
            try:
                intent = parse_channel_dispatch_intent(raw)
            except ChannelSupplierV2RegistrationError as exc:
                await persist_alarm(
                    db_session,
                    level="error",
                    code="channel_supplier_v2_intent_invalid",
                    message="A intenção persistida de SMS/RCS V2 é inválida.",
                    details={
                        "workspace_uuid": workspace_uuid,
                        "flow_uuid": flow_uuid,
                        "session_id": session_id,
                        "error_code": exc.code,
                    },
                    flow_uuid=flow_uuid,
                    app_name="Celery",
                )
                return {"status": "invalid_intent", "reason": exc.code}
            current_status = str(intent.get("status") or "").lower()
            current_attempts = int(intent.get("attempts") or 0)
            if current_status == "registered":
                return {
                    "status": "registered",
                    "dispatch_id": intent.get("dispatch_id"),
                    "intent": intent,
                }
            if current_status == "failed":
                return {"status": "failed"}
            effective_attempt = max(attempt, current_attempts + 1)
            if current_status == "registering":
                started_at = _parse_iso(intent.get("registration_started_at"))
                if started_at is not None and (
                    datetime.now(timezone.utc) - started_at
                ).total_seconds() < max(30, int(registration_lease_seconds)):
                    return {"status": "in_progress"}
            now = datetime.now(timezone.utc).isoformat()
            claimed = dict(intent)
            claimed.update(
                {
                    "status": "registering",
                    "attempts": effective_attempt,
                    "registration_started_at": now,
                    "updated_at": now,
                }
            )
            stored = await patch_session_channel_supplier_v2_registration(
                db_session,
                session_id=session_id,
                idempotency_key=str(intent["idempotency_key"]),
                registration=claimed,
            )
            return (
                {"status": "claimed", "intent": claimed, "attempt": effective_attempt}
                if stored
                else {"status": "stale"}
            )


async def _store_error(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
    intent: dict[str, Any],
    attempt: int,
    error: ChannelSupplierV2RegistrationError,
    will_retry: bool,
) -> None:
    updated_at = datetime.now(timezone.utc).isoformat()
    registration = dict(intent)
    registration.update(
        {
            "status": "pending_retry" if will_retry else "failed",
            "attempts": attempt,
            "updated_at": updated_at,
            "last_error": {
                "code": error.code,
                "message": error.message,
                "status_code": error.status_code,
                "retryable": error.retryable,
                "updated_at": updated_at,
            },
        }
    )
    await _patch_registration(
        workspace_uuid=workspace_uuid,
        session_id=session_id,
        idempotency_key=str(intent["idempotency_key"]),
        registration=registration,
    )


async def _sync_registered_dispatch(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
    intent: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            get_channel_dispatch,
            workspace_uuid=workspace_uuid,
            intent=intent,
            settings=settings,
        )
    except ChannelSupplierV2RegistrationError as exc:
        logger.warning(
            "channel Supplier V2 dispatch state unavailable",
            extra={
                "event": "orch.channel_supplier_v2.dispatch_state.unavailable",
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
                "dispatch_id": intent.get("dispatch_id"),
                "error_code": exc.code,
                "retryable": exc.retryable,
            },
        )
        return {
            "status": "registered",
            "dispatch_id": intent.get("dispatch_id"),
            "reason": exc.code,
        }

    if result.state in {"pending", "dispatching"}:
        return {
            "status": "registered",
            "dispatch_id": result.dispatch_id,
            "dispatch_state": result.state,
        }

    registration = dict(intent)
    registration.update(result.runtime_payload())
    registration.update(
        {
            "status": f"provider_{result.state}",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    transitioned = await _transition_registered_dispatch(
        workspace_uuid=workspace_uuid,
        session_id=session_id,
        idempotency_key=str(intent["idempotency_key"]),
        registration=registration,
    )
    if result.state == "accepted" and transitioned:
        from app.tasks.workflow_tasks import (
            resume_channel_supplier_v2_acceptance_task,
        )

        resume_channel_supplier_v2_acceptance_task.apply_async(
            kwargs={
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
            },
            queue=settings.celery_execute_queue,
            routing_key=settings.celery_execute_queue,
        )
        logger.info(
            "channel Supplier V2 provider acceptance scheduled for workflow resume",
            extra={
                "event": "orch.channel_supplier_v2.acceptance.resume_scheduled",
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
                "dispatch_id": result.dispatch_id,
            },
        )
    elif result.state in {"failed", "uncertain"} and transitioned:
        logger.error(
            "channel Supplier V2 dispatch reached a non-accepted terminal state",
            extra={
                "event": "orch.channel_supplier_v2.dispatch_state.terminal_failure",
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
                "dispatch_id": result.dispatch_id,
                "dispatch_state": result.state,
                "error_code": result.last_error_code,
            },
        )
    return {
        "status": registration["status"],
        "dispatch_id": result.dispatch_id,
        "transitioned": transitioned,
    }


async def _patch_registration(
    *,
    workspace_uuid: str,
    session_id: int,
    idempotency_key: str,
    registration: dict[str, Any],
) -> bool:
    _safe_workspace_uuid, schema = bind_workspace_context(workspace_uuid)
    safe_schema = schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            await db_session.execute(
                text("SELECT pg_advisory_xact_lock(:class_id, :object_id)"),
                {"class_id": _WORKFLOW_LOCK_CLASS_ID, "object_id": int(session_id)},
            )
            return await patch_session_channel_supplier_v2_registration(
                db_session,
                session_id=session_id,
                idempotency_key=idempotency_key,
                registration=registration,
            )


async def _transition_registered_dispatch(
    *,
    workspace_uuid: str,
    session_id: int,
    idempotency_key: str,
    registration: dict[str, Any],
) -> bool:
    _safe_workspace_uuid, schema = bind_workspace_context(workspace_uuid)
    safe_schema = schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            await db_session.execute(
                text("SELECT pg_advisory_xact_lock(:class_id, :object_id)"),
                {"class_id": _WORKFLOW_LOCK_CLASS_ID, "object_id": int(session_id)},
            )
            state = await fetch_session_workflow_state(
                db_session,
                session_id=session_id,
            )
            runtime = state.get("runtime_variables") if isinstance(state, dict) else None
            workflow = runtime.get("workflow_v2") if isinstance(runtime, dict) else None
            current = (
                workflow.get("channel_dispatch_v2")
                if isinstance(workflow, dict)
                else None
            )
            if (
                not isinstance(current, dict)
                or str(current.get("idempotency_key") or "") != idempotency_key
                or str(current.get("status") or "").lower() != "registered"
            ):
                return False
            return await patch_session_channel_supplier_v2_registration(
                db_session,
                session_id=session_id,
                idempotency_key=idempotency_key,
                registration=registration,
            )


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _reconcile_pending_channel_supplier_v2_dispatches_task() -> dict[str, int]:
    settings = get_settings()
    if not settings.channel_supplier_v2_enabled:
        return {"scanned": 0, "enqueued": 0}

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        workspaces = await list_completed_workspaces(db_session)
        if db_session.in_transaction():
            await db_session.commit()

    scanned = 0
    enqueued = 0
    flow_allowlist = [
        str(item) for item in settings.channel_supplier_v2_flow_allowlist
    ]
    allowed_workspaces = {
        str(item) for item in settings.channel_supplier_v2_workspace_allowlist
    }
    for workspace in workspaces:
        workspace_uuid = str(workspace["workspace_uuid"])
        if workspace_uuid not in allowed_workspaces:
            continue
        _safe_workspace_uuid, workspace_schema = bind_workspace_context(
            workspace_uuid
        )
        safe_schema = workspace_schema.replace('"', '""')
        async with session_factory() as db_session:
            async with db_session.begin():
                await db_session.execute(
                    text(f'SET LOCAL search_path TO "{safe_schema}"')
                )
                rows = await _list_reconcilable_channel_supplier_v2_dispatches(
                    db_session,
                    flow_allowlist=flow_allowlist,
                    registration_lease_seconds=int(
                        settings.channel_supplier_v2_registration_lease_seconds
                    ),
                    limit=int(settings.channel_supplier_v2_reconcile_batch_size),
                )

        scanned += len(rows)
        for row in rows:
            register_channel_supplier_v2_dispatch_task.apply_async(
                kwargs={
                    "workspace_uuid": workspace_uuid,
                    "flow_uuid": str(row["flow_uuid"]),
                    "session_id": int(row["id"]),
                    "recovery_attempt": max(1, int(row["attempts"]) + 1),
                },
                queue=settings.celery_channel_supplier_v2_queue,
                routing_key=settings.celery_channel_supplier_v2_queue,
            )
            enqueued += 1

    logger.info(
        "channel Supplier V2 pending dispatches reconciled",
        extra={
            "event": "orch.channel_supplier_v2.reconcile.finished",
            "supplier_version": "v2",
            "scanned": scanned,
            "enqueued": enqueued,
        },
    )
    return {"scanned": scanned, "enqueued": enqueued}


async def _list_reconcilable_channel_supplier_v2_dispatches(
    db_session: Any,
    *,
    flow_allowlist: list[str],
    registration_lease_seconds: int,
    limit: int,
    _table_name: str = "orch_sessions",
) -> list[dict[str, Any]]:
    """Select pending or lease-expired SMS/RCS intents in the current schema."""

    if _table_name not in {
        "orch_sessions",
        "channel_supplier_v2_reconcile_test_sessions",
    }:
        raise ValueError("invalid channel Supplier V2 reconciliation table")
    quoted_table = f'"{_table_name}"'
    query_sql = """
            SELECT
                id,
                flow_uuid::text AS flow_uuid,
                CASE
                    WHEN runtime_variables #>> '{workflow_v2,channel_dispatch_v2,attempts}' ~ '^[0-9]+$'
                    THEN (runtime_variables #>> '{workflow_v2,channel_dispatch_v2,attempts}')::integer
                    ELSE 0
                END AS attempts
            FROM __ORCH_SESSIONS_TABLE__
            WHERE state = 1
              AND ended_at IS NULL
              AND flow_uuid = ANY(CAST(:flow_uuids AS uuid[]))
              AND (
                    runtime_variables #>> '{workflow_v2,channel_dispatch_v2,status}' = 'pending'
                    OR (
                        runtime_variables #>> '{workflow_v2,channel_dispatch_v2,status}' = 'registering'
                        AND updated_at <= NOW() - make_interval(secs => :lease_seconds)
                    )
                    OR (
                        runtime_variables #>> '{workflow_v2,channel_dispatch_v2,status}' = 'pending_retry'
                        AND updated_at <= NOW() - make_interval(
                            secs => :pending_retry_recovery_seconds
                        )
                    )
                    OR (
                        runtime_variables #>> '{workflow_v2,channel_dispatch_v2,status}' = 'registered'
                        AND runtime_variables #>> '{workflow_v2,channel_dispatch_v2,channel}' = 'sms'
                        AND runtime_variables #>> '{workflow_v2,blocking_stop_reason}' = 'blocked_send_with_sms'
                    )
              )
            ORDER BY updated_at ASC, id ASC
            LIMIT :limit
            """.replace("__ORCH_SESSIONS_TABLE__", quoted_table)
    result = await db_session.execute(
        text(query_sql),
        {
            "flow_uuids": flow_allowlist,
            "lease_seconds": max(30, int(registration_lease_seconds)),
            "pending_retry_recovery_seconds": _PENDING_RETRY_RECOVERY_SECONDS,
            "limit": max(1, int(limit)),
        },
    )
    return [dict(row) for row in result.mappings().all()]


__all__ = [
    "register_channel_supplier_v2_dispatch_task",
    "reconcile_pending_channel_supplier_v2_dispatches_task",
    "_register_channel_supplier_v2_dispatch_task",
    "_reconcile_pending_channel_supplier_v2_dispatches_task",
    "_list_reconcilable_channel_supplier_v2_dispatches",
    "_sync_registered_dispatch",
]
