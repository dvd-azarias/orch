from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
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
    OrchMigrateAllResponse,
    OrchMigrateWorkspaceResponse,
    OrchSessionListResponse,
    OrchSessionSummary,
    OrchTriggerAccepted,
)
from app.services.alarm_query_service import list_alarms
from app.services.app_detector import APP_ARQUIVOS
from app.services.app_detector import detect_app
from app.services.alarm_service import persist_alarm
from app.services.file_event_ingest_service import expand_arquivos_payload_into_rows
from app.services.fileapp_tipo1_service import (
    is_file_event_in_monitored_folder,
    resolve_mapping_template_uuid,
    resolve_monitored_folders,
)
from app.services.orch_trigger_service import process_single_payload
from app.services.session_extractor import extract_session_fields
from app.services.session_query_service import (
    get_session_by_uuid,
    list_sessions_by_entity,
    list_sessions_by_flow_uuid,
)
from app.services.migration_service import migrate_all_active_workspaces, migrate_workspace
from app.services.session_service import persist_session
from app.services.workspace_service import bind_workspace_context, ensure_active_workspace
from app.tasks.fileapp_ingest_tasks import ingest_fileapp_event_task, ingest_fileapp_tipo1_event_task

router = APIRouter(prefix="/v1/orch", tags=["orch"])
logger = get_logger(__name__)


def _legacy_workspace_context() -> tuple[str | None, str]:
    settings = core_config.get_settings()
    fallback_workspace_uuid = settings.orch_default_workspace_uuid or settings.orch_lab_workspace_uuid
    if fallback_workspace_uuid:
        safe_workspace_uuid = normalize_workspace_uuid(fallback_workspace_uuid)
        return safe_workspace_uuid, workspace_schema_from_uuid(safe_workspace_uuid)
    return None, settings.database_schema


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
    "/{flow_uuid}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OrchTriggerAccepted,
)
async def trigger_orch(
    flow_uuid: UUID,
    payload: dict[str, Any] = Body(...),
    db_session: AsyncSession = Depends(get_db_session),
) -> OrchTriggerAccepted:
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
    await ensure_active_workspace(db_session, workspace_uuid=safe_workspace_uuid)
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
