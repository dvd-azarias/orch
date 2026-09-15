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
    patch_session_dialer_supplier_v2_registration,
)
from app.services.alarm_service import persist_alarm
from app.services.dialer_supplier_v2_service import (
    DialerSupplierV2RegistrationError,
    dialer_supplier_v2_enabled_for_context,
    parse_dialer_cycle_intent,
    register_dialer_cycle,
)
from app.services.workspace_service import bind_workspace_context, list_completed_workspaces


logger = get_logger(__name__)
_WORKFLOW_LOCK_CLASS_ID = 92021
_PENDING_RETRY_RECOVERY_SECONDS = 300


@celery_app.task(
    name="app.tasks.dialer_supplier_v2.register_cycle",
    bind=True,
    ignore_result=True,
    max_retries=7,
    acks_late=True,
    reject_on_worker_lost=True,
)
def register_dialer_supplier_v2_cycle_task(
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
        _register_dialer_supplier_v2_cycle_task(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            session_id=session_id,
            attempt=attempt,
        )
    )
    if result.get("status") != "retry":
        return result

    effective_attempt = int(result.get("attempt") or attempt)
    backoff = float(settings.dialer_supplier_v2_retry_backoff_seconds)
    countdown = min(300.0, backoff * (2 ** max(0, effective_attempt - 1)))
    raise self.retry(
        exc=RuntimeError(
            f"dialer_supplier_v2_cycle_registration_retry:{result.get('reason')}"
        ),
        countdown=countdown,
    )


@celery_app.task(
    name="app.tasks.dialer_supplier_v2.reconcile_pending_cycles",
    ignore_result=True,
)
def reconcile_pending_dialer_supplier_v2_cycles_task() -> dict[str, int]:
    return asyncio.run(_reconcile_pending_dialer_supplier_v2_cycles_task())


async def _register_dialer_supplier_v2_cycle_task(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
    attempt: int,
) -> dict[str, Any]:
    settings = get_settings()
    if not dialer_supplier_v2_enabled_for_context(
        settings=settings,
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
    ):
        return {"status": "disabled"}

    prepared = await _claim_registration_attempt(
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
        session_id=session_id,
        attempt=attempt,
        registration_lease_seconds=int(
            settings.dialer_supplier_v2_registration_lease_seconds
        ),
    )
    if prepared.get("status") != "claimed":
        if prepared.get("status") == "locked":
            # A tarefa concorrente que detém o lock concluirá o registro. Se
            # ela morrer antes de persistir o claim, o reconciliador encontrará
            # o intent ainda pending; se morrer depois, o lease de registering
            # permite a retomada. Não consumir o orçamento de retry por disputa.
            return {"status": "in_progress"}
        return prepared

    intent = prepared["intent"]
    effective_attempt = int(prepared.get("attempt") or attempt)
    final_attempt = effective_attempt >= int(
        settings.dialer_supplier_v2_max_attempts
    )
    try:
        result = await asyncio.to_thread(
            register_dialer_cycle,
            workspace_uuid=workspace_uuid,
            intent=intent,
            settings=settings,
        )
    except DialerSupplierV2RegistrationError as exc:
        will_retry = exc.retryable and not final_attempt
        stored = await _store_registration_error(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            session_id=session_id,
            intent=intent,
            attempt=effective_attempt,
            error=exc,
            will_retry=will_retry,
        )
        if not stored:
            return {"status": "stale"}
        logger.warning(
            "dialer Supplier V2 cycle registration failed",
            extra={
                "event": "orch.dialer_supplier_v2.cycle.failed",
                "supplier_version": "v2",
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
                "component_ref_id": intent.get("component_ref_id"),
                "contact_list_member_id": intent.get("contact_list_member_id"),
                "attempt": effective_attempt,
                "error_code": exc.code,
                "status_code": exc.status_code,
                "retryable": exc.retryable,
                "will_retry": will_retry,
            },
        )
        return {
            "status": "retry" if will_retry else "failed",
            "reason": exc.code,
            "attempt": effective_attempt,
        }
    except Exception as exc:  # pragma: no cover - proteção final da integração
        wrapped = DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_unexpected_error",
            "Falha inesperada ao registrar o ciclo no Supplier V2.",
            retryable=True,
        )
        will_retry = not final_attempt
        stored = await _store_registration_error(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            session_id=session_id,
            intent=intent,
            attempt=effective_attempt,
            error=wrapped,
            will_retry=will_retry,
        )
        if not stored:
            return {"status": "stale"}
        logger.exception(
            "unexpected dialer Supplier V2 cycle registration failure",
            extra={
                "event": "orch.dialer_supplier_v2.cycle.unexpected_failure",
                "supplier_version": "v2",
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
                "attempt": effective_attempt,
                "exception_type": type(exc).__name__,
                "will_retry": will_retry,
            },
        )
        return {
            "status": "retry" if will_retry else "failed",
            "reason": wrapped.code,
            "attempt": effective_attempt,
        }

    stored = await _store_registration_success(
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
        session_id=session_id,
        intent=intent,
        attempt=effective_attempt,
        result=result.runtime_payload(),
    )
    if not stored:
        return {"status": "stale"}
    logger.info(
        "dialer Supplier V2 cycle registered",
        extra={
            "event": "orch.dialer_supplier_v2.cycle.ready",
            "supplier_version": "v2",
            "workspace_uuid": workspace_uuid,
            "flow_uuid": flow_uuid,
            "session_id": session_id,
            "cycle_id": result.cycle_id,
            "component_ref_id": intent.get("component_ref_id"),
            "contact_list_member_id": intent.get("contact_list_member_id"),
            "dial_profile_revision_id": result.dial_profile_revision_id,
            "attempt_policy_id": result.attempt_policy_id,
            "attempt_limit_id": result.attempt_limit_id,
            "attempt": effective_attempt,
            "replayed": result.replayed,
        },
    )
    return {"status": "ready", "cycle_id": result.cycle_id}


async def _claim_registration_attempt(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
    attempt: int,
    registration_lease_seconds: int,
) -> dict[str, Any]:
    _safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)
    safe_schema = workspace_schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            lock_result = await db_session.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock(:class_id, :object_id) AS locked"
                ),
                {"class_id": _WORKFLOW_LOCK_CLASS_ID, "object_id": int(session_id)},
            )
            if not bool(lock_result.scalar_one()):
                return {"status": "locked"}

            session_state = await fetch_session_workflow_state(
                db_session, session_id=session_id
            )
            if session_state is None:
                return {"status": "missing_session"}
            if str(session_state.get("flow_uuid") or "") != str(flow_uuid):
                return {"status": "flow_mismatch"}
            runtime_variables = session_state.get("runtime_variables")
            if not isinstance(runtime_variables, dict):
                return {"status": "missing_runtime"}
            workflow_meta = runtime_variables.get("workflow_v2")
            if not isinstance(workflow_meta, dict):
                return {"status": "missing_workflow"}
            raw_intent = workflow_meta.get("dialer_supplier_v2")
            try:
                intent = parse_dialer_cycle_intent(raw_intent)
            except DialerSupplierV2RegistrationError as exc:
                await persist_alarm(
                    db_session,
                    level="error",
                    code="dialer_supplier_v2_cycle_intent_invalid",
                    message="A intenção persistida do ciclo Supplier V2 é inválida.",
                    details={
                        "session_id": session_id,
                        "flow_uuid": flow_uuid,
                        "workspace_uuid": workspace_uuid,
                        "error_code": exc.code,
                    },
                    flow_uuid=flow_uuid,
                    app_name="Celery",
                )
                return {"status": "invalid_intent", "reason": exc.code}

            current_status = str(intent.get("status") or "").strip().lower()
            current_attempts = int(intent.get("attempts") or 0)
            if (
                current_status == "terminal_received"
                or isinstance(intent.get("terminal_delivery"), dict)
            ):
                return {
                    "status": "terminal_received",
                    "cycle_id": intent.get("cycle_id"),
                }
            if current_status == "ready":
                return {"status": "ready", "cycle_id": intent.get("cycle_id")}
            if current_status == "failed":
                return {"status": "failed"}
            effective_attempt = attempt
            if current_status == "registering":
                started_at = _parse_iso_datetime(intent.get("registration_started_at"))
                lease_expired = (
                    started_at is None
                    or (
                        datetime.now(timezone.utc) - started_at
                    ).total_seconds()
                    >= registration_lease_seconds
                )
                if not lease_expired:
                    return {"status": "in_progress"}
                effective_attempt = max(attempt, current_attempts + 1)
            elif current_status == "pending_retry" and current_attempts >= attempt:
                return {"status": "awaiting_retry"}

            now_iso = datetime.now(timezone.utc).isoformat()
            claimed = dict(intent)
            claimed.update(
                {
                    "status": "registering",
                    "attempts": max(current_attempts, effective_attempt),
                    "registration_started_at": now_iso,
                    "updated_at": now_iso,
                }
            )
            stored = await patch_session_dialer_supplier_v2_registration(
                db_session,
                session_id=session_id,
                idempotency_key=str(intent["idempotency_key"]),
                registration=claimed,
            )
            if not stored:
                return {"status": "stale"}
            return {
                "status": "claimed",
                "intent": claimed,
                "attempt": effective_attempt,
            }


async def _store_registration_success(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
    intent: dict[str, Any],
    attempt: int,
    result: dict[str, Any],
) -> bool:
    ready_at = datetime.now(timezone.utc).isoformat()
    registration = dict(intent)
    registration.update(result)
    registration.update(
        {
            "status": "ready",
            "attempts": max(int(intent.get("attempts") or 0), attempt),
            "completed_at": ready_at,
            "updated_at": ready_at,
            "last_error": None,
        }
    )
    return await _patch_registration_state(
        workspace_uuid=workspace_uuid,
        session_id=session_id,
        idempotency_key=str(intent["idempotency_key"]),
        registration=registration,
    )


async def _store_registration_error(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    session_id: int,
    intent: dict[str, Any],
    attempt: int,
    error: DialerSupplierV2RegistrationError,
    will_retry: bool,
) -> bool:
    failed_at = datetime.now(timezone.utc).isoformat()
    registration = dict(intent)
    registration.update(
        {
            "status": "pending_retry" if will_retry else "failed",
            "attempts": max(int(intent.get("attempts") or 0), attempt),
            "updated_at": failed_at,
            "last_error": {
                "code": error.code,
                "message": error.message,
                "status_code": error.status_code,
                "retryable": error.retryable,
                "updated_at": failed_at,
            },
        }
    )
    if not will_retry:
        registration["failed_at"] = failed_at

    _safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)
    safe_schema = workspace_schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            await db_session.execute(
                text("SELECT pg_advisory_xact_lock(:class_id, :object_id)"),
                {"class_id": _WORKFLOW_LOCK_CLASS_ID, "object_id": int(session_id)},
            )
            stored = await patch_session_dialer_supplier_v2_registration(
                db_session,
                session_id=session_id,
                idempotency_key=str(intent["idempotency_key"]),
                registration=registration,
            )
            if not stored:
                return False
            await persist_alarm(
                db_session,
                level="warning" if will_retry else "error",
                code=(
                    "dialer_supplier_v2_cycle_registration_retry"
                    if will_retry
                    else "dialer_supplier_v2_cycle_registration_failed"
                ),
                message=(
                    "O registro do ciclo Supplier V2 será tentado novamente."
                    if will_retry
                    else "O registro do ciclo Supplier V2 falhou de forma terminal."
                ),
                details={
                    "session_id": session_id,
                    "flow_uuid": flow_uuid,
                    "workspace_uuid": workspace_uuid,
                    "component_ref_id": intent.get("component_ref_id"),
                    "contact_list_member_id": intent.get(
                        "contact_list_member_id"
                    ),
                    "attempt": attempt,
                    "error_code": error.code,
                    "status_code": error.status_code,
                    "retryable": error.retryable,
                    "will_retry": will_retry,
                },
                flow_uuid=flow_uuid,
                app_name="Celery",
            )
            return stored


async def _patch_registration_state(
    *,
    workspace_uuid: str,
    session_id: int,
    idempotency_key: str,
    registration: dict[str, Any],
) -> bool:
    _safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)
    safe_schema = workspace_schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            await db_session.execute(
                text("SELECT pg_advisory_xact_lock(:class_id, :object_id)"),
                {"class_id": _WORKFLOW_LOCK_CLASS_ID, "object_id": int(session_id)},
            )
            return await patch_session_dialer_supplier_v2_registration(
                db_session,
                session_id=session_id,
                idempotency_key=idempotency_key,
                registration=registration,
            )


async def _reconcile_pending_dialer_supplier_v2_cycles_task() -> dict[str, int]:
    settings = get_settings()
    if not settings.dialer_supplier_v2_enabled:
        return {"scanned": 0, "enqueued": 0}

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        workspaces = await list_completed_workspaces(db_session)
        if db_session.in_transaction():
            await db_session.commit()

    scanned = 0
    enqueued = 0
    flow_allowlist = [
        str(item) for item in settings.dialer_supplier_v2_flow_allowlist
    ]
    allowed_workspaces = {
        str(item) for item in settings.dialer_supplier_v2_workspace_allowlist
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
                rows = await _list_reconcilable_dialer_supplier_v2_cycles(
                    db_session,
                    flow_allowlist=flow_allowlist,
                    registration_lease_seconds=int(
                        settings.dialer_supplier_v2_registration_lease_seconds
                    ),
                    limit=int(settings.dialer_supplier_v2_reconcile_batch_size),
                )

        scanned += len(rows)
        for row in rows:
            register_dialer_supplier_v2_cycle_task.apply_async(
                kwargs={
                    "workspace_uuid": workspace_uuid,
                    "flow_uuid": str(row["flow_uuid"]),
                    "session_id": int(row["id"]),
                    "recovery_attempt": max(1, int(row["attempts"]) + 1),
                },
                queue=settings.celery_dialer_supplier_v2_queue,
                routing_key=settings.celery_dialer_supplier_v2_queue,
            )
            enqueued += 1

    logger.info(
        "dialer Supplier V2 pending cycles reconciled",
        extra={
            "event": "orch.dialer_supplier_v2.reconcile.finished",
            "supplier_version": "v2",
            "scanned": scanned,
            "enqueued": enqueued,
        },
    )
    return {"scanned": scanned, "enqueued": enqueued}


async def _list_reconcilable_dialer_supplier_v2_cycles(
    db_session: Any,
    *,
    flow_allowlist: list[str],
    registration_lease_seconds: int,
    limit: int,
    _table_name: str = "orch_sessions",
) -> list[dict[str, Any]]:
    """Select pending or lease-expired cycle intents in the current schema."""

    if _table_name not in {
        "orch_sessions",
        "dialer_supplier_v2_reconcile_test_sessions",
    }:
        raise ValueError("invalid dialer Supplier V2 reconciliation table")
    quoted_table = f'"{_table_name}"'

    query_sql = """
            SELECT
                id,
                flow_uuid::text AS flow_uuid,
                CASE
                    WHEN runtime_variables #>> '{workflow_v2,dialer_supplier_v2,attempts}' ~ '^[0-9]+$'
                    THEN (runtime_variables #>> '{workflow_v2,dialer_supplier_v2,attempts}')::integer
                    ELSE 0
                END AS attempts
            FROM __ORCH_SESSIONS_TABLE__
            WHERE state = 1
              AND ended_at IS NULL
              AND flow_uuid = ANY(CAST(:flow_uuids AS uuid[]))
              AND (
                    runtime_variables #>> '{workflow_v2,dialer_supplier_v2,status}' = 'pending'
                    OR (
                        runtime_variables #>> '{workflow_v2,dialer_supplier_v2,status}' = 'registering'
                        AND updated_at <= NOW() - make_interval(
                            secs => :lease_seconds
                        )
                    )
                    OR (
                        runtime_variables #>> '{workflow_v2,dialer_supplier_v2,status}' = 'pending_retry'
                        AND updated_at <= NOW() - make_interval(
                            secs => :pending_retry_recovery_seconds
                        )
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


def _parse_iso_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    "register_dialer_supplier_v2_cycle_task",
    "reconcile_pending_dialer_supplier_v2_cycles_task",
    "_register_dialer_supplier_v2_cycle_task",
    "_reconcile_pending_dialer_supplier_v2_cycles_task",
    "_list_reconcilable_dialer_supplier_v2_cycles",
]
