from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.core.logging import get_logger
from app.core.request_context import get_request_id
from app.core.config import get_settings
from app.schemas.orch import (
    OrchAlarmListResponse,
    OrchAlarmSummary,
    OrchSessionListResponse,
    OrchSessionSummary,
    OrchTriggerAccepted,
)
from app.services.alarm_query_service import list_alarms
from app.services.app_detector import detect_app
from app.services.alarm_service import persist_alarm
from app.services.session_extractor import extract_session_fields
from app.services.session_query_service import (
    get_session_by_uuid,
    list_sessions_by_entity,
    list_sessions_by_flow_uuid,
)
from app.services.session_service import persist_session
from app.services.workflow_m2_service import WorkflowExecutionError, execute_workflow_m2_for_session
from app.services.workflow_runtime_service import WorkflowBootstrapError, bootstrap_workflow_for_session
from app.tasks.workflow_tasks import advance_session_task

router = APIRouter(prefix="/v1/orch", tags=["orch"])
logger = get_logger(__name__)


def _m2_alarm_from_stopped_reason(stopped_reason: str) -> tuple[str, str, str] | None:
    if stopped_reason.startswith("component_not_supported"):
        return (
            "warning",
            "workflow_m2_component_not_supported",
            "Componente encontrado fora do escopo implementado do M2.",
        )
    if stopped_reason == "component_not_found":
        return (
            "error",
            "workflow_m2_component_not_found",
            "Componente de fluxo não encontrado na definição durante execução M2.",
        )
    if stopped_reason == "max_steps_reached":
        return (
            "warning",
            "workflow_m2_max_steps_reached",
            "Execução M2 interrompida por limite de passos.",
        )
    if stopped_reason == "session_execution_locked":
        return (
            "warning",
            "workflow_m2_session_execution_locked",
            "Execução M2 ignorada por lock de sessão em andamento.",
        )
    if stopped_reason == "frozen_wait_active":
        return (
            "warning",
            "workflow_m2_frozen_wait_active",
            "Execução M2 aguardando término de janela de espera (frozen_until).",
        )
    if stopped_reason in {"flow_not_found", "revision_not_found", "session_not_found", "no_next_card"}:
        return (
            "warning",
            f"workflow_m2_{stopped_reason}",
            "Execução M2 não avançou por pré-condição de fluxo/sessão.",
        )
    return None


@router.post(
    "/{flow_uuid}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OrchTriggerAccepted,
)
async def trigger_orch(
    flow_uuid: UUID,
    payload: dict[str, Any] = Body(...),
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchTriggerAccepted:
    app_name: str | None = None
    extracted = None
    try:
        app_name = detect_app(payload)
        extracted = extract_session_fields(app_name, payload)
        persisted = await persist_session(
            db_session,
            flow_uuid=str(flow_uuid),
            app_name=app_name,
            extracted=extracted.model_dump(),
            payload=payload,
        )
    except HTTPException as exc:
        alarm_level = "warning" if exc.status_code < 500 else "error"
        await persist_alarm(
            db_session,
            level=alarm_level,
            code="trigger_orch_http_exception",
            message=str(exc.detail),
            details={"status_code": exc.status_code},
            flow_uuid=str(flow_uuid),
            app_name=app_name,
            entity=(extracted.entity if extracted else None),
            entity_type=(extracted.entity_type if extracted else None),
            entity_address=(extracted.entity_address if extracted else None),
        )
        raise
    except Exception as exc:
        await persist_alarm(
            db_session,
            level="error",
            code="trigger_orch_unhandled_exception",
            message="Erro inesperado no processamento do trigger.",
            details={"exception": str(type(exc).__name__)},
            flow_uuid=str(flow_uuid),
            app_name=app_name,
            entity=(extracted.entity if extracted else None),
            entity_type=(extracted.entity_type if extracted else None),
            entity_address=(extracted.entity_address if extracted else None),
        )
        raise

    logger.info(
        "orch trigger accepted",
        extra={
            "event": "orch.trigger.accepted",
            "request_id": get_request_id(),
            "flow_uuid": str(flow_uuid),
            "app": app_name,
            "entity": extracted.entity,
            "session_id": persisted.session_id,
            "session_uuid": persisted.session_uuid,
        },
    )

    workflow_bootstrap = None
    workflow_execution = None
    try:
        workflow_result = await bootstrap_workflow_for_session(
            db_session,
            flow_uuid=str(flow_uuid),
            session_id=persisted.session_id,
            payload=payload,
        )
        workflow_bootstrap = {
            "enabled": workflow_result.enabled,
            "loaded": workflow_result.loaded,
            "reason": workflow_result.reason,
            "flow_id": workflow_result.flow_id,
            "revision_id": workflow_result.revision_id,
            "revision_version": workflow_result.revision_version,
            "revision_mode": workflow_result.revision_mode,
            "next_card_uuid": workflow_result.next_card_uuid,
        }
        if workflow_result.enabled and not workflow_result.loaded:
            await persist_alarm(
                db_session,
                level="warning",
                code="workflow_bootstrap_not_loaded",
                message="Bootstrap do workflow não carregado nesta requisição.",
                details=workflow_bootstrap,
                flow_uuid=str(flow_uuid),
                app_name=app_name,
                entity=extracted.entity,
                entity_type=extracted.entity_type,
                entity_address=extracted.entity_address,
                session_uuid=persisted.session_uuid,
            )
    except WorkflowBootstrapError as exc:
        await persist_alarm(
            db_session,
            level="error",
            code=f"workflow_bootstrap_{exc.code}",
            message=exc.message,
            details={},
            flow_uuid=str(flow_uuid),
            app_name=app_name,
            entity=extracted.entity,
            entity_type=extracted.entity_type,
            entity_address=extracted.entity_address,
            session_uuid=persisted.session_uuid,
        )
    except Exception:
        await persist_alarm(
            db_session,
            level="error",
            code="workflow_bootstrap_unhandled_exception",
            message="Falha inesperada no bootstrap do workflow.",
            details={},
            flow_uuid=str(flow_uuid),
            app_name=app_name,
            entity=extracted.entity,
            entity_type=extracted.entity_type,
            entity_address=extracted.entity_address,
            session_uuid=persisted.session_uuid,
        )

    if workflow_bootstrap and workflow_bootstrap.get("loaded"):
        settings = get_settings()
        if settings.celery_enabled:
            try:
                advance_session_task.delay(
                    flow_uuid=str(flow_uuid),
                    session_id=persisted.session_id,
                )
                workflow_execution = {
                    "mode": "async",
                    "enqueued": True,
                    "dispatcher": "celery",
                }
            except Exception as exc:
                await persist_alarm(
                    db_session,
                    level="error",
                    code="workflow_m2_enqueue_failed",
                    message="Falha ao enfileirar execução assíncrona do workflow.",
                    details={
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    },
                    flow_uuid=str(flow_uuid),
                    app_name=app_name,
                    entity=extracted.entity,
                    entity_type=extracted.entity_type,
                    entity_address=extracted.entity_address,
                    session_uuid=persisted.session_uuid,
                )
                workflow_execution = {
                    "mode": "async",
                    "enqueued": False,
                    "dispatcher": "celery",
                }
        else:
            try:
                execution_result = await execute_workflow_m2_for_session(
                    db_session,
                    flow_uuid=str(flow_uuid),
                    session_id=persisted.session_id,
                )
                workflow_execution = {
                    "enabled": execution_result.enabled,
                    "executed_steps": execution_result.executed_steps,
                    "stopped_reason": execution_result.stopped_reason,
                    "last_card_uuid": execution_result.last_card_uuid,
                    "next_card_uuid": execution_result.next_card_uuid,
                    "mode": "inline_fallback",
                }
                m2_alarm = (
                    _m2_alarm_from_stopped_reason(execution_result.stopped_reason)
                    if execution_result.enabled
                    else None
                )
                if m2_alarm is not None:
                    level, code, message = m2_alarm
                    await persist_alarm(
                        db_session,
                        level=level,
                        code=code,
                        message=message,
                        details=workflow_execution,
                        flow_uuid=str(flow_uuid),
                        app_name=app_name,
                        entity=extracted.entity,
                        entity_type=extracted.entity_type,
                        entity_address=extracted.entity_address,
                        session_uuid=persisted.session_uuid,
                    )
            except WorkflowExecutionError as exc:
                await persist_alarm(
                    db_session,
                    level="error",
                    code=f"workflow_m2_{exc.code}",
                    message=exc.message,
                    details={},
                    flow_uuid=str(flow_uuid),
                    app_name=app_name,
                    entity=extracted.entity,
                    entity_type=extracted.entity_type,
                    entity_address=extracted.entity_address,
                    session_uuid=persisted.session_uuid,
                )
            except Exception as exc:
                logger.exception(
                    "workflow m2 unhandled exception",
                    extra={
                        "event": "orch.workflow.m2.unhandled_exception",
                        "request_id": get_request_id(),
                        "flow_uuid": str(flow_uuid),
                        "session_id": persisted.session_id,
                        "session_uuid": persisted.session_uuid,
                        "exception_type": type(exc).__name__,
                    },
                )
                await persist_alarm(
                    db_session,
                    level="error",
                    code="workflow_m2_unhandled_exception",
                    message="Falha inesperada na execução M2 do workflow.",
                    details={
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    },
                    flow_uuid=str(flow_uuid),
                    app_name=app_name,
                    entity=extracted.entity,
                    entity_type=extracted.entity_type,
                    entity_address=extracted.entity_address,
                    session_uuid=persisted.session_uuid,
                )

    return OrchTriggerAccepted(
        status="accepted",
        accepted=True,
        flow_uuid=str(flow_uuid),
        app=app_name,
        persistence="saved",
        extracted=extracted,
        session_id=persisted.session_id,
        session_uuid=persisted.session_uuid,
        session_state=persisted.session_state,
        session_created=persisted.session_created,
        workflow_bootstrap=workflow_bootstrap,
        workflow_execution=workflow_execution,
    )


@router.get(
    "/sessions/{session_uuid}",
    response_model=OrchSessionSummary,
    status_code=status.HTTP_200_OK,
)
async def get_orch_session(
    session_uuid: UUID,
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchSessionSummary:
    session_data = await get_session_by_uuid(db_session, session_uuid=str(session_uuid))
    if session_data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sessão não encontrada para o UUID informado.",
        )
    return OrchSessionSummary(**session_data)


@router.get(
    "/sessions/by-flow/{flow_uuid}",
    response_model=OrchSessionListResponse,
    status_code=status.HTTP_200_OK,
)
async def get_sessions_by_flow(
    flow_uuid: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchSessionListResponse:
    try:
        page = await list_sessions_by_flow_uuid(
            db_session,
            flow_uuid=str(flow_uuid),
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        await persist_alarm(
            db_session,
            level="warning",
            code="query_flow_invalid_cursor",
            message=str(exc),
            details={"cursor": cursor},
            flow_uuid=str(flow_uuid),
            app_name=None,
        )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return OrchSessionListResponse(
        total=len(page.items),
        items=[OrchSessionSummary(**item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/sessions/by-entity",
    response_model=OrchSessionListResponse,
    status_code=status.HTTP_200_OK,
)
async def get_sessions_by_entity(
    entity: str = Query(..., min_length=1),
    entity_type: str | None = Query(default=None),
    entity_address: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchSessionListResponse:
    try:
        page = await list_sessions_by_entity(
            db_session,
            entity=entity,
            entity_type=entity_type,
            entity_address=entity_address,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        await persist_alarm(
            db_session,
            level="warning",
            code="query_entity_invalid_cursor",
            message=str(exc),
            details={"cursor": cursor},
            flow_uuid=None,
            app_name=None,
            entity=entity,
            entity_type=entity_type,
            entity_address=entity_address,
        )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return OrchSessionListResponse(
        total=len(page.items),
        items=[OrchSessionSummary(**item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/alarms",
    response_model=OrchAlarmListResponse,
    status_code=status.HTTP_200_OK,
)
async def get_alarms(
    level: str | None = Query(default=None, pattern="^(warning|error)$"),
    code: str | None = Query(default=None),
    flow_uuid: UUID | None = Query(default=None),
    session_uuid: UUID | None = Query(default=None),
    app_name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchAlarmListResponse:
    try:
        page = await list_alarms(
            db_session,
            level=level,
            code=code,
            flow_uuid=(str(flow_uuid) if flow_uuid is not None else None),
            session_uuid=(str(session_uuid) if session_uuid is not None else None),
            app_name=app_name,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    return OrchAlarmListResponse(
        total=len(page.items),
        items=[OrchAlarmSummary(**item) for item in page.items],
        next_cursor=page.next_cursor,
    )
