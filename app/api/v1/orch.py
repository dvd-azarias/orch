from __future__ import annotations

import asyncio
import re
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.config as core_config
from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.core.request_context import set_workspace_context
from app.core.workspace import normalize_workspace_uuid, workspace_schema_from_uuid
from app.schemas.orch import (
    OrchAlarmListResponse,
    OrchAlarmSummary,
    OrchCreateSessionRequest,
    OrchFlowAliasCreateResponse,
    OrchFlowAliasSummary,
    OrchMigrateAllResponse,
    OrchMigrateWorkspaceResponse,
    OrchSessionListResponse,
    OrchSessionSummary,
    OrchTriggerAccepted,
    OrchUnassignSessionRequest,
    OrchUnassignSessionResponse,
    OrchWhatsappLimitUpsertRequest,
    OrchWhatsappLimitUpsertResponse,
    SessionExtraction,
)
from app.repositories.orch_flow_aliases_repository import (
    create_or_get_flow_alias,
    fetch_active_flow_alias,
    fetch_flow_alias_by_workspace_flow,
)
from app.repositories.orch_whatsapp_limits_repository import register_whatsapp_limit_event
from app.repositories.orch_sessions_repository import (
    set_session_assigned_at_default,
    set_unassigned_at_by_flow_and_entity_address,
)
from app.services.alarm_query_service import list_alarms
from app.services.app_detector import APP_ARQUIVOS
from app.services.app_detector import detect_app
from app.services.alarm_service import persist_alarm
from app.services.discarded_event_service import persist_discarded_event
from app.services.file_event_ingest_service import expand_arquivos_payload_into_rows
from app.services.fileapp_tipo1_service import (
    is_file_event_in_monitored_folder,
    resolve_mapping_template_uuid,
    resolve_monitored_folders,
)
from app.services.orch_trigger_service import m2_alarm_from_stopped_reason, process_single_payload
from app.services.session_extractor import extract_session_fields
from app.services.session_query_service import (
    get_session_by_uuid,
    list_sessions_by_entity,
    list_sessions_by_flow_uuid,
)
from app.services.phone_normalizer import normalize_phone_to_canonical_ani
from app.services.workflow_m2_service import WorkflowExecutionError, execute_workflow_m2_for_session
from app.services.workflow_runtime_service import WorkflowBootstrapError, bootstrap_workflow_for_session
from app.services.migration_service import migrate_all_active_workspaces, migrate_workspace
from app.services.session_service import persist_session
from app.services.workspace_service import (
    bind_workspace_context,
    ensure_active_workspace,
    ensure_workspace_ready_for_orch_migrate,
)
from app.tasks.fileapp_ingest_tasks import ingest_fileapp_event_task, ingest_fileapp_tipo1_event_task
from app.tasks.workflow_tasks import advance_session_task

router = APIRouter(prefix="/v1/orch", tags=["orch"])
logger = get_logger(__name__)
_CELERY_ENQUEUE_TIMEOUT_SECONDS = 3.0
_SUPPORTED_MANUAL_APPS = {"ArquivosApp", "WhatsApp", "DialerApp", "GenericApp"}
_FLOW_ALIAS_PATTERN = re.compile(r"^[0-9a-f]{14}$")


def _legacy_workspace_context() -> tuple[str | None, str]:
    settings = core_config.get_settings()
    fallback_workspace_uuid = settings.orch_default_workspace_uuid or settings.orch_lab_workspace_uuid
    if fallback_workspace_uuid:
        safe_workspace_uuid = normalize_workspace_uuid(fallback_workspace_uuid)
        return safe_workspace_uuid, workspace_schema_from_uuid(safe_workspace_uuid)
    return None, settings.database_schema


def _read_required_text(value: str, *, field_name: str) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Campo obrigatório inválido: '{field_name}'.",
        )
    return text_value


def _build_entity_session_id(*, entity_address: str, flow_uuid: UUID) -> str:
    return f"{entity_address}:::{str(flow_uuid)}"


def _is_short_flow_alias(value: str) -> bool:
    return bool(_FLOW_ALIAS_PATTERN.fullmatch(str(value or "").strip().lower()))


async def _trigger_orch_for_workspace(
    *,
    workspace_uuid: str | None,
    flow_uuid: UUID,
    payload: dict[str, Any],
    db_session: AsyncSession,
    validate_workspace: bool = True,
    schema_override: str | None = None,
) -> OrchTriggerAccepted:
    safe_workspace_uuid: str | None = None
    if schema_override is not None:
        workspace_schema = schema_override
        if workspace_uuid:
            safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
        set_workspace_context(
            workspace_uuid=safe_workspace_uuid or "legacy",
            workspace_schema=workspace_schema,
        )
    else:
        if workspace_uuid is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="workspace_uuid obrigatório para esta operação.",
            )
        safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)

    if validate_workspace:
        if safe_workspace_uuid is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="workspace_uuid obrigatório para validação do workspace.",
            )
        await ensure_active_workspace(
            db_session,
            workspace_uuid=safe_workspace_uuid,
        )

    app_name = detect_app(payload)
    settings = get_settings()
    mapping_template_uuid = None
    if app_name == APP_ARQUIVOS and safe_workspace_uuid is not None:
        monitored_folders = await resolve_monitored_folders(
            db_session,
            workspace_schema=workspace_schema,
            flow_uuid=str(flow_uuid),
        )
        if not is_file_event_in_monitored_folder(payload=payload, monitored_folders=monitored_folders):
            extracted = extract_session_fields(app_name, payload)
            await persist_discarded_event(
                db_session,
                flow_uuid=str(flow_uuid),
                app_name=app_name,
                entity=extracted.entity,
                entity_type=extracted.entity_type,
                entity_address=extracted.entity_address,
                entity_session_id=extracted.entity_session_id,
                discard_reason="unmonitored_folder",
                payload=payload,
            )
            logger.info(
                "fileapp event ignored due to unmonitored folder",
                extra={
                    "event": "orch.fileapp.ingest.ignored.unmonitored_folder",
                    "workspace_uuid": safe_workspace_uuid,
                    "flow_uuid": str(flow_uuid),
                    "folder_path": extracted.entity_address.rsplit("/", 1)[0],
                    "monitored_folders": sorted(monitored_folders),
                },
            )
            return OrchTriggerAccepted(
                status="ignored",
                accepted=False,
                flow_uuid=str(flow_uuid),
                app=app_name,
                persistence="ignored",
                extracted=extracted,
                session_id=0,
                session_uuid="",
                session_state=0,
                session_created=False,
                workflow_execution={
                    "mode": "async",
                    "enqueued": False,
                    "reason": "unmonitored_folder",
                    "monitored_folders": sorted(monitored_folders),
                },
            )
        mapping_template_uuid = await resolve_mapping_template_uuid(
            db_session,
            workspace_schema=workspace_schema,
            flow_uuid=str(flow_uuid),
            payload=payload,
        )

    if (
        app_name == APP_ARQUIVOS
        and safe_workspace_uuid is not None
        and settings.celery_enabled
        and settings.celery_fileapp_ingest_enabled
        and mapping_template_uuid
    ):
        extracted = extract_session_fields(app_name, payload)
        task = ingest_fileapp_tipo1_event_task.apply_async(
            kwargs={
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": str(flow_uuid),
                "payload": payload,
                "mapping_template_uuid": mapping_template_uuid,
            },
            queue=settings.celery_s3_files_ingest_queue,
            routing_key=settings.celery_s3_files_ingest_queue,
        )
        logger.info(
            "fileapp tipo1 ingest accepted",
            extra={
                "event": "orch.fileapp.tipo1.ingest.accepted",
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": str(flow_uuid),
                "queue": settings.celery_s3_files_ingest_queue,
                "task_id": task.id,
                "mapping_template_uuid": mapping_template_uuid,
            },
        )
        return OrchTriggerAccepted(
            status="accepted",
            accepted=True,
            flow_uuid=str(flow_uuid),
            app=app_name,
            persistence="queued",
            extracted=extracted,
            session_id=0,
            session_uuid=str(task.id),
            session_state=0,
            session_created=False,
            workflow_execution={
                "mode": "async",
                "enqueued": True,
                "dispatcher": "celery",
                "workspace_uuid": safe_workspace_uuid,
                "pipeline": "fileapp_tipo1_ingest",
                "task_id": task.id,
                "queue": settings.celery_s3_files_ingest_queue,
                "mapping_template_uuid": mapping_template_uuid,
            },
        )

    if (
        app_name == APP_ARQUIVOS
        and safe_workspace_uuid is not None
        and settings.celery_enabled
        and settings.celery_fileapp_ingest_enabled
        and not mapping_template_uuid
    ):
        extracted = extract_session_fields(app_name, payload)
        persisted = await persist_session(
            db_session,
            flow_uuid=str(flow_uuid),
            app_name=app_name,
            extracted=extracted.model_dump(),
            payload=payload,
        )
        task = ingest_fileapp_event_task.apply_async(
            kwargs={
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": str(flow_uuid),
                "payload": payload,
            },
            queue=settings.celery_s3_files_ingest_queue,
            routing_key=settings.celery_s3_files_ingest_queue,
        )
        logger.info(
            "fileapp tipo2 ingest accepted",
            extra={
                "event": "orch.fileapp.tipo2.ingest.accepted",
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": str(flow_uuid),
                "queue": settings.celery_s3_files_ingest_queue,
                "task_id": task.id,
            },
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
            workflow_execution={
                "mode": "async",
                "enqueued": True,
                "dispatcher": "celery",
                "workspace_uuid": safe_workspace_uuid,
                "pipeline": "fileapp_tipo2_ingest",
                "task_id": task.id,
                "queue": settings.celery_s3_files_ingest_queue,
            },
        )

    payloads_to_process = [payload]
    if app_name == APP_ARQUIVOS:
        payloads_to_process = await expand_arquivos_payload_into_rows(
            payload,
            settings=settings,
        )

    first_response: OrchTriggerAccepted | None = None
    for item_payload in payloads_to_process:
        response = await process_single_payload(
            safe_workspace_uuid=safe_workspace_uuid,
            workspace_schema=workspace_schema,
            flow_uuid=str(flow_uuid),
            payload=item_payload,
            db_session=db_session,
            app_name=app_name,
        )
        if first_response is None:
            first_response = response

    if first_response is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Nenhuma linha válida foi processada para este evento.",
        )

    if len(payloads_to_process) > 1:
        first_response.workflow_execution = {
            **(first_response.workflow_execution or {}),
            "batch_rows": len(payloads_to_process),
            "batch_mode": "file_rows",
        }
    return first_response


@router.post(
    "/{workspace_uuid}/{flow_uuid}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OrchTriggerAccepted,
)
async def trigger_orch_by_workspace(
    workspace_uuid: UUID,
    flow_uuid: UUID,
    payload: dict[str, Any] = Body(...),
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchTriggerAccepted:
    return await _trigger_orch_for_workspace(
        workspace_uuid=str(workspace_uuid),
        flow_uuid=flow_uuid,
        payload=payload,
        db_session=db_session,
    )


@router.post(
    "/{workspace_uuid}/{flow_uuid}/sessions",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OrchTriggerAccepted,
)
async def create_orch_session_by_workspace(
    workspace_uuid: UUID,
    flow_uuid: UUID,
    request: OrchCreateSessionRequest = Body(...),
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchTriggerAccepted:
    safe_workspace_uuid, workspace_schema = bind_workspace_context(str(workspace_uuid))
    await ensure_active_workspace(db_session, workspace_uuid=safe_workspace_uuid)

    app_name = _read_required_text(request.app_name, field_name="app_name")
    if app_name not in _SUPPORTED_MANUAL_APPS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"app_name inválido. Valores aceitos: {', '.join(sorted(_SUPPORTED_MANUAL_APPS))}.",
        )

    entity = _read_required_text(request.entity, field_name="entity")
    entity_type = _read_required_text(request.entity_type, field_name="entity_type")
    entity_address = _read_required_text(request.entity_address, field_name="entity_address")
    extracted = SessionExtraction(
        entity=entity,
        entity_type=entity_type,
        entity_address=entity_address,
        entity_session_id=_build_entity_session_id(
            entity_address=entity_address,
            flow_uuid=flow_uuid,
        ),
    )
    payload = request.payload if isinstance(request.payload, dict) else {}

    persisted = await persist_session(
        db_session,
        flow_uuid=str(flow_uuid),
        app_name=app_name,
        extracted=extracted.model_dump(),
        payload=payload,
    )
    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        safe_schema = workspace_schema.replace('"', '""')
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        await set_session_assigned_at_default(
            db_session,
            session_id=persisted.session_id,
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
                await asyncio.wait_for(
                    asyncio.to_thread(
                        lambda: advance_session_task.delay(
                            workspace_uuid=safe_workspace_uuid,
                            flow_uuid=str(flow_uuid),
                            session_id=persisted.session_id,
                        )
                    ),
                    timeout=_CELERY_ENQUEUE_TIMEOUT_SECONDS,
                )
                workflow_execution = {
                    "mode": "async",
                    "enqueued": True,
                    "dispatcher": "celery",
                    "workspace_uuid": safe_workspace_uuid,
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
                    "workspace_uuid": safe_workspace_uuid,
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
                    m2_alarm_from_stopped_reason(execution_result.stopped_reason)
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

    logger.info(
        "orch manual session accepted",
        extra={
            "event": "orch.manual_session.accepted",
            "workspace_uuid": safe_workspace_uuid,
            "workspace_schema": workspace_schema,
            "flow_uuid": str(flow_uuid),
            "app": app_name,
            "entity": extracted.entity,
            "session_id": persisted.session_id,
            "session_uuid": persisted.session_uuid,
        },
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


@router.post(
    "/{workspace_uuid}/{flow_uuid}/sessions/unassign",
    status_code=status.HTTP_200_OK,
    response_model=OrchUnassignSessionResponse,
)
async def unassign_orch_sessions_by_entity_address(
    workspace_uuid: UUID,
    flow_uuid: UUID,
    request: OrchUnassignSessionRequest = Body(...),
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchUnassignSessionResponse:
    safe_workspace_uuid, workspace_schema = bind_workspace_context(str(workspace_uuid))
    await ensure_active_workspace(db_session, workspace_uuid=safe_workspace_uuid)
    entity_address = _read_required_text(request.entity_address, field_name="entity_address")

    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        safe_schema = workspace_schema.replace('"', '""')
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        updated_count = await set_unassigned_at_by_flow_and_entity_address(
            db_session,
            flow_uuid=str(flow_uuid),
            entity_address=entity_address,
        )
    logger.info(
        "orch sessions unassigned by entity_address",
        extra={
            "event": "orch.sessions.unassign",
            "workspace_uuid": safe_workspace_uuid,
            "workspace_schema": workspace_schema,
            "flow_uuid": str(flow_uuid),
            "entity_address": entity_address,
            "updated_count": updated_count,
        },
    )
    return OrchUnassignSessionResponse(
        status="updated",
        flow_uuid=str(flow_uuid),
        entity_address=entity_address,
        updated_count=updated_count,
    )


@router.post(
    "/{workspace_uuid}/whatsapp/limits",
    status_code=status.HTTP_200_OK,
    response_model=OrchWhatsappLimitUpsertResponse,
)
async def upsert_whatsapp_limit_for_workspace(
    workspace_uuid: UUID,
    request: OrchWhatsappLimitUpsertRequest = Body(...),
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchWhatsappLimitUpsertResponse:
    safe_workspace_uuid, workspace_schema = bind_workspace_context(str(workspace_uuid))
    await ensure_active_workspace(db_session, workspace_uuid=safe_workspace_uuid)

    phone_raw = _read_required_text(request.phone, field_name="phone")
    phone = str(normalize_phone_to_canonical_ani(phone_raw) or "").strip()
    if not phone:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Campo 'phone' inválido após normalização.",
        )
    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        safe_schema = workspace_schema.replace('"', '""')
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        row = await register_whatsapp_limit_event(
            db_session,
            phone=phone,
            allowed_limit=int(request.allowed_limit),
        )

    logger.info(
        "whatsapp limit event registered",
        extra={
            "event": "orch.whatsapp.limit.registered",
            "workspace_uuid": safe_workspace_uuid,
            "workspace_schema": workspace_schema,
            "phone": phone,
            "allowed_limit": int(request.allowed_limit),
            "limit_id": int(row["id"]),
        },
    )
    return OrchWhatsappLimitUpsertResponse(
        status="ok",
        id=int(row["id"]),
        phone=str(row["phone"]),
        allowed_limit=int(row["allowed_limit"]),
        received_from_meta_at=row["received_from_meta_at"],
        in_use=bool(row["in_use"]),
    )


@router.post(
    "/{workspace_uuid}/{flow_uuid}/alias",
    response_model=OrchFlowAliasCreateResponse,
    status_code=status.HTTP_200_OK,
)
async def create_orch_flow_alias(
    workspace_uuid: UUID,
    flow_uuid: UUID,
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchFlowAliasCreateResponse:
    safe_workspace_uuid, _workspace_schema = bind_workspace_context(str(workspace_uuid))
    await ensure_active_workspace(db_session, workspace_uuid=safe_workspace_uuid)
    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        row = await create_or_get_flow_alias(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            flow_uuid=str(flow_uuid),
        )
    return OrchFlowAliasCreateResponse(
        status="ok",
        item=OrchFlowAliasSummary(
            alias=str(row["alias"]),
            workspace_uuid=str(row["workspace_uuid"]),
            flow_uuid=str(row["flow_uuid"]),
            is_active=bool(row["is_active"]),
        ),
    )


@router.get(
    "/aliases/{alias}",
    response_model=OrchFlowAliasSummary,
    status_code=status.HTTP_200_OK,
)
async def get_orch_flow_alias(
    alias: str,
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchFlowAliasSummary:
    safe_alias = str(alias or "").strip().lower()
    if not _is_short_flow_alias(safe_alias):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Alias inválido. Formato esperado: 14 caracteres hexadecimais (lowercase).",
        )
    row = await fetch_active_flow_alias(
        db_session,
        alias=safe_alias,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Alias não encontrado.",
        )
    return OrchFlowAliasSummary(
        alias=str(row["alias"]),
        workspace_uuid=str(row["workspace_uuid"]),
        flow_uuid=str(row["flow_uuid"]),
        is_active=bool(row["is_active"]),
    )


@router.get(
    "/{workspace_uuid}/{flow_uuid}/alias",
    response_model=OrchFlowAliasSummary,
    status_code=status.HTTP_200_OK,
)
async def get_orch_flow_alias_by_workspace_flow(
    workspace_uuid: UUID,
    flow_uuid: UUID,
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchFlowAliasSummary:
    safe_workspace_uuid, _workspace_schema = bind_workspace_context(str(workspace_uuid))
    await ensure_active_workspace(db_session, workspace_uuid=safe_workspace_uuid)
    row = await fetch_flow_alias_by_workspace_flow(
        db_session,
        workspace_uuid=safe_workspace_uuid,
        flow_uuid=str(flow_uuid),
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Alias não encontrado para workspace/flow informados.",
        )
    return OrchFlowAliasSummary(
        alias=str(row["alias"]),
        workspace_uuid=str(row["workspace_uuid"]),
        flow_uuid=str(row["flow_uuid"]),
        is_active=bool(row["is_active"]),
    )


@router.post(
    "/{alias_or_flow_uuid}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OrchTriggerAccepted,
)
async def trigger_orch(
    alias_or_flow_uuid: str,
    payload: dict[str, Any] = Body(...),
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchTriggerAccepted:
    raw_target = str(alias_or_flow_uuid or "").strip().lower()
    if _is_short_flow_alias(raw_target):
        row = await fetch_active_flow_alias(
            db_session,
            alias=raw_target,
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Alias não encontrado.",
            )
        return await _trigger_orch_for_workspace(
            workspace_uuid=str(row["workspace_uuid"]),
            flow_uuid=UUID(str(row["flow_uuid"])),
            payload=payload,
            db_session=db_session,
        )

    try:
        flow_uuid = UUID(raw_target)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Parâmetro inválido: use UUID de flow ou alias hex14.",
        ) from exc

    fallback_workspace_uuid, fallback_workspace_schema = _legacy_workspace_context()
    set_workspace_context(
        workspace_uuid=normalize_workspace_uuid(fallback_workspace_uuid) if fallback_workspace_uuid else "legacy",
        workspace_schema=fallback_workspace_schema,
    )
    return await _trigger_orch_for_workspace(
        workspace_uuid=fallback_workspace_uuid,
        flow_uuid=flow_uuid,
        payload=payload,
        db_session=db_session,
        validate_workspace=False,
        schema_override=fallback_workspace_schema,
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
    fallback_workspace_uuid, fallback_workspace_schema = _legacy_workspace_context()
    set_workspace_context(
        workspace_uuid=normalize_workspace_uuid(fallback_workspace_uuid) if fallback_workspace_uuid else "legacy",
        workspace_schema=fallback_workspace_schema,
    )
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
    fallback_workspace_uuid, fallback_workspace_schema = _legacy_workspace_context()
    set_workspace_context(
        workspace_uuid=normalize_workspace_uuid(fallback_workspace_uuid) if fallback_workspace_uuid else "legacy",
        workspace_schema=fallback_workspace_schema,
    )
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
    fallback_workspace_uuid, fallback_workspace_schema = _legacy_workspace_context()
    set_workspace_context(
        workspace_uuid=normalize_workspace_uuid(fallback_workspace_uuid) if fallback_workspace_uuid else "legacy",
        workspace_schema=fallback_workspace_schema,
    )
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
    fallback_workspace_uuid, fallback_workspace_schema = _legacy_workspace_context()
    set_workspace_context(
        workspace_uuid=normalize_workspace_uuid(fallback_workspace_uuid) if fallback_workspace_uuid else "legacy",
        workspace_schema=fallback_workspace_schema,
    )
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


@router.post(
    "/admin/workspaces/{workspace_uuid}/migrate",
    response_model=OrchMigrateWorkspaceResponse,
    status_code=status.HTTP_200_OK,
)
async def migrate_workspace_orch(
    workspace_uuid: UUID,
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchMigrateWorkspaceResponse:
    safe_workspace_uuid = normalize_workspace_uuid(str(workspace_uuid))
    await ensure_workspace_ready_for_orch_migrate(db_session, workspace_uuid=safe_workspace_uuid)
    result = await migrate_workspace(
        db_session,
        workspace_uuid=safe_workspace_uuid,
    )
    return OrchMigrateWorkspaceResponse(
        workspace_uuid=result.workspace_uuid,
        workspace_schema=result.schema,
        applied_versions=result.applied_versions,
        skipped_versions=result.skipped_versions,
    )


@router.post(
    "/admin/workspaces/migrate-all",
    response_model=OrchMigrateAllResponse,
    status_code=status.HTTP_200_OK,
)
async def migrate_all_workspaces_orch(
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchMigrateAllResponse:
    results = await migrate_all_active_workspaces(db_session)
    items = [
        OrchMigrateWorkspaceResponse(
            workspace_uuid=item.workspace_uuid,
            workspace_schema=item.schema,
            applied_versions=item.applied_versions,
            skipped_versions=item.skipped_versions,
        )
        for item in results
    ]
    return OrchMigrateAllResponse(
        total=len(items),
        items=items,
    )
