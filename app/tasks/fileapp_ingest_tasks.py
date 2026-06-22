from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.services.app_detector import APP_ARQUIVOS, detect_app
from app.services.file_event_ingest_service import expand_arquivos_payload_into_rows
from app.services.fileapp_tipo1_manual_pipeline_service import (
    FileAppTipo1ManualPipelineError,
    build_file_event_mailing_identity,
    run_tipo1_manual_pipeline,
)
from app.services.fileapp_tipo1_service import resolve_detach_all_files
from app.services.fileapp_processed_file_service import (
    FileAppProcessedFileError,
    move_processed_file_to_processados,
)
from app.services.fileapp_mailing_association_service import associate_mailing_to_flow_from_file_event
from app.services.orch_trigger_service import process_single_payload
from app.repositories.workspaces_repository import fetch_workspace_otima_billing_api_key
from app.services.alarm_service import persist_alarm
from app.services.workspace_service import bind_workspace_context

logger = get_logger(__name__)

_RETRY_DELAYS = (30, 120, 300)


async def _fetch_existing_source_list_names(
    db_session,
    *,
    workspace_schema: str,
    base_slug: str,
) -> list[str]:
    safe_schema = workspace_schema.replace('"', '""')
    result = await db_session.execute(
        text(
            f"""
            SELECT name
            FROM "{safe_schema}".source_lists
            WHERE name = :base_slug
               OR name LIKE :base_slug_prefix
            """
        ),
        {
            "base_slug": base_slug,
            "base_slug_prefix": f"{base_slug}_%",
        },
    )
    return [str(name or "").strip() for name in result.scalars().all() if str(name or "").strip()]


@celery_app.task(name="app.tasks.fileapp.ingest_event", bind=True, ignore_result=True)
def ingest_fileapp_event_task(self, *, workspace_uuid: str, flow_uuid: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    try:
        task = process_fileapp_event_task.apply_async(
            kwargs={
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "payload": payload,
            },
            queue=settings.celery_source_list_ingest_queue,
            routing_key=settings.celery_source_list_ingest_queue,
        )
    except Exception as exc:
        retries = int(self.request.retries or 0)
        countdown = _RETRY_DELAYS[min(retries, len(_RETRY_DELAYS) - 1)]
        raise self.retry(exc=exc, countdown=countdown)

    logger.info(
        "fileapp.ingest_event.enqueued",
        extra={
            "event": "orch.fileapp.ingest_event.enqueued",
            "workspace_uuid": workspace_uuid,
            "flow_uuid": flow_uuid,
            "queue": settings.celery_source_list_ingest_queue,
            "task_id": task.id,
        },
    )
    return {
        "status": "queued",
        "workspace_uuid": workspace_uuid,
        "flow_uuid": flow_uuid,
        "task_id": task.id,
    }


@celery_app.task(name="app.tasks.fileapp.ingest_tipo1_event", bind=True, ignore_result=True)
def ingest_fileapp_tipo1_event_task(
    self,
    *,
    workspace_uuid: str,
    flow_uuid: str,
    payload: dict[str, Any],
    mapping_template_uuid: str,
) -> dict[str, Any]:
    settings = get_settings()
    try:
        task = process_fileapp_tipo1_event_task.apply_async(
            kwargs={
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "payload": payload,
                "mapping_template_uuid": mapping_template_uuid,
            },
            queue=settings.celery_source_list_ingest_queue,
            routing_key=settings.celery_source_list_ingest_queue,
        )
    except Exception as exc:
        retries = int(self.request.retries or 0)
        countdown = _RETRY_DELAYS[min(retries, len(_RETRY_DELAYS) - 1)]
        raise self.retry(exc=exc, countdown=countdown)

    logger.info(
        "fileapp.tipo1.ingest_event.enqueued",
        extra={
            "event": "orch.fileapp.tipo1.ingest_event.enqueued",
            "workspace_uuid": workspace_uuid,
            "flow_uuid": flow_uuid,
            "mapping_template_uuid": mapping_template_uuid,
            "queue": settings.celery_source_list_ingest_queue,
            "task_id": task.id,
        },
    )
    return {
        "status": "queued",
        "workspace_uuid": workspace_uuid,
        "flow_uuid": flow_uuid,
        "mapping_template_uuid": mapping_template_uuid,
        "task_id": task.id,
    }


@celery_app.task(name="app.tasks.fileapp.process_event", ignore_result=True)
def process_fileapp_event_task(*, workspace_uuid: str, flow_uuid: str, payload: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(
        _process_fileapp_event_task(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            payload=payload,
        )
    )


@celery_app.task(name="app.tasks.fileapp.process_tipo1_event", ignore_result=True)
def process_fileapp_tipo1_event_task(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    payload: dict[str, Any],
    mapping_template_uuid: str,
) -> dict[str, Any]:
    return asyncio.run(
        _process_fileapp_tipo1_event_task(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            payload=payload,
            mapping_template_uuid=mapping_template_uuid,
        )
    )


@celery_app.task(name="app.tasks.fileapp.associate_mailing", bind=True, ignore_result=True, max_retries=8)
def associate_fileapp_mailing_task(
    self,
    *,
    workspace_uuid: str,
    flow_uuid: str,
    mailing_uuid: str,
    linked_by: str | None,
) -> dict[str, Any]:
    return asyncio.run(
        _associate_fileapp_mailing_task(
            task=self,
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            mailing_uuid=mailing_uuid,
            linked_by=linked_by,
        )
    )


async def _process_fileapp_event_task(*, workspace_uuid: str, flow_uuid: str, payload: dict[str, Any]) -> dict[str, Any]:
    app_name = detect_app(payload)
    if app_name != APP_ARQUIVOS:
        return {"status": "ignored", "reason": "not_file_app", "workspace_uuid": workspace_uuid, "flow_uuid": flow_uuid}

    settings = get_settings()
    session_factory = get_session_factory()
    processed_rows = 0
    failed_rows = 0
    async with session_factory() as db_session:
        safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)
        payloads = await expand_arquivos_payload_into_rows(payload, settings=settings)
        for item_payload in payloads:
            try:
                await process_single_payload(
                    safe_workspace_uuid=safe_workspace_uuid,
                    workspace_schema=workspace_schema,
                    flow_uuid=flow_uuid,
                    payload=item_payload,
                    db_session=db_session,
                    app_name=app_name,
                )
                processed_rows += 1
            except HTTPException:
                failed_rows += 1
            except Exception:
                failed_rows += 1
                logger.exception(
                    "fileapp.process_event.row_failed",
                    extra={
                        "event": "orch.fileapp.process_event.row_failed",
                        "workspace_uuid": safe_workspace_uuid,
                        "flow_uuid": flow_uuid,
                    },
                )

    logger.info(
        "fileapp.process_event.finished",
        extra={
            "event": "orch.fileapp.process_event.finished",
            "workspace_uuid": workspace_uuid,
            "flow_uuid": flow_uuid,
            "processed_rows": processed_rows,
            "failed_rows": failed_rows,
        },
    )
    return {
        "status": "done",
        "workspace_uuid": workspace_uuid,
        "flow_uuid": flow_uuid,
        "processed_rows": processed_rows,
        "failed_rows": failed_rows,
    }


async def _process_fileapp_tipo1_event_task(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    payload: dict[str, Any],
    mapping_template_uuid: str,
) -> dict[str, Any]:
    app_name = detect_app(payload)
    if app_name != APP_ARQUIVOS:
        return {"status": "ignored", "reason": "not_file_app", "workspace_uuid": workspace_uuid, "flow_uuid": flow_uuid}

    settings = get_settings()
    session_factory = get_session_factory()
    mailing_name: str | None = None
    mailing_description: str | None = None
    file_data = payload.get("file") if isinstance(payload.get("file"), dict) else {}
    original_name = str(file_data.get("original_name") or "").strip()

    async with session_factory() as db_session:
        safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)
        workspace_api_key = await fetch_workspace_otima_billing_api_key(
            db_session,
            workspace_uuid=safe_workspace_uuid,
        )
        if original_name:
            base_identity = build_file_event_mailing_identity(file_name=original_name)
            existing_names = await _fetch_existing_source_list_names(
                db_session,
                workspace_schema=workspace_schema,
                base_slug=base_identity.name,
            )
            identity = build_file_event_mailing_identity(
                file_name=original_name,
                existing_names=existing_names,
            )
            mailing_name = identity.name
            mailing_description = identity.description

    try:
        pipeline_result = await run_tipo1_manual_pipeline(
            settings=settings,
            workspace_uuid=safe_workspace_uuid,
            flow_uuid=flow_uuid,
            payload=payload,
            mapping_template_uuid=mapping_template_uuid,
            workspace_api_key=workspace_api_key,
            mailing_name=mailing_name,
            mailing_description=mailing_description,
            defer_step7_link_flow=True,
        )
    except FileAppTipo1ManualPipelineError as exc:
        logger.warning(
            "fileapp.tipo1.manual_pipeline.failed",
            extra={
                "event": "orch.fileapp.tipo1.manual_pipeline.failed",
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": flow_uuid,
                "mapping_template_uuid": mapping_template_uuid,
                "failed_step": exc.step,
                "error_message": exc.message,
                "details": exc.details,
            },
        )
        return {
            "status": "failed",
            "reason": exc.step,
            "message": exc.message,
            "details": exc.details,
            "workspace_uuid": safe_workspace_uuid,
            "flow_uuid": flow_uuid,
            "mapping_template_uuid": mapping_template_uuid,
        }
    except Exception as exc:
        logger.exception(
            "fileapp.tipo1.manual_pipeline.unexpected_error",
            extra={
                "event": "orch.fileapp.tipo1.manual_pipeline.unexpected_error",
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": flow_uuid,
                "mapping_template_uuid": mapping_template_uuid,
            },
        )
        return {
            "status": "failed",
            "reason": type(exc).__name__,
            "message": str(exc),
            "workspace_uuid": safe_workspace_uuid,
            "flow_uuid": flow_uuid,
            "mapping_template_uuid": mapping_template_uuid,
        }

    logger.info(
        "fileapp.tipo1.process_event.finished",
        extra={
            "event": "orch.fileapp.tipo1.process_event.finished",
            "workspace_uuid": workspace_uuid,
            "flow_uuid": flow_uuid,
            "mapping_template_uuid": mapping_template_uuid,
            "manual_pipeline": pipeline_result,
        },
    )
    try:
        post_process_result = await move_processed_file_to_processados(
            settings=settings,
            workspace_uuid=safe_workspace_uuid,
            payload=payload,
        )
        logger.info(
            "fileapp.tipo1.post_process_file.done",
            extra={
                "event": "orch.fileapp.tipo1.post_process_file.done",
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": flow_uuid,
                "result": post_process_result,
            },
        )
    except FileAppProcessedFileError as exc:
        payload_file = payload.get("file") if isinstance(payload.get("file"), dict) else {}
        logger.warning(
            "fileapp.tipo1.post_process_file.failed",
            extra={
                "event": "orch.fileapp.tipo1.post_process_file.failed",
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": flow_uuid,
                "code": exc.code,
                "error_message": exc.message,
                "details": exc.details,
            },
        )
        async with session_factory() as alarm_session:
            bind_workspace_context(safe_workspace_uuid)
            await persist_alarm(
                alarm_session,
                level="warning",
                code="fileapp_tipo1_post_process_file_failed",
                message="Falha ao mover/renomear arquivo para processados após ingestão tipo1.",
                details={
                    "flow_uuid": flow_uuid,
                    "workspace_uuid": safe_workspace_uuid,
                    "error_code": exc.code,
                    "error_message": exc.message,
                    "error_details": exc.details,
                },
                flow_uuid=flow_uuid,
                app_name="ArquivosApp",
                entity=str(payload_file.get("id") or ""),
                entity_type="file",
                entity_address=str(payload_file.get("folder_path") or ""),
            )
        raise
    except Exception as exc:
        payload_file = payload.get("file") if isinstance(payload.get("file"), dict) else {}
        logger.exception(
            "fileapp.tipo1.post_process_file.unexpected_error",
            extra={
                "event": "orch.fileapp.tipo1.post_process_file.unexpected_error",
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": flow_uuid,
            },
        )
        async with session_factory() as alarm_session:
            bind_workspace_context(safe_workspace_uuid)
            await persist_alarm(
                alarm_session,
                level="warning",
                code="fileapp_tipo1_post_process_file_unexpected_error",
                message="Erro inesperado no pós-processamento de arquivo tipo1 (move/rename).",
                details={
                    "flow_uuid": flow_uuid,
                    "workspace_uuid": safe_workspace_uuid,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
                flow_uuid=flow_uuid,
                app_name="ArquivosApp",
                entity=str(payload_file.get("id") or ""),
                entity_type="file",
                entity_address=str(payload_file.get("folder_path") or ""),
            )
        raise

    payload_file = payload.get("file") if isinstance(payload.get("file"), dict) else {}
    association_task = associate_fileapp_mailing_task.apply_async(
        kwargs={
            "workspace_uuid": safe_workspace_uuid,
            "flow_uuid": flow_uuid,
            "mailing_uuid": str(pipeline_result.get("mailing_uuid") or "").strip(),
            "linked_by": str(payload_file.get("id") or "").strip() or None,
        },
        queue=settings.celery_fileapp_mailing_assoc_queue,
        routing_key=settings.celery_fileapp_mailing_assoc_queue,
        countdown=max(0, int(settings.celery_fileapp_mailing_assoc_delay_seconds)),
    )
    logger.info(
        "fileapp.tipo1.mailing_association.enqueued",
        extra={
            "event": "orch.fileapp.tipo1.mailing_association.enqueued",
            "workspace_uuid": safe_workspace_uuid,
            "flow_uuid": flow_uuid,
            "mailing_uuid": str(pipeline_result.get("mailing_uuid") or "").strip(),
            "queue": settings.celery_fileapp_mailing_assoc_queue,
            "countdown_seconds": int(settings.celery_fileapp_mailing_assoc_delay_seconds),
            "task_id": association_task.id,
        },
    )
    return {
        "status": "done",
        "workspace_uuid": workspace_uuid,
        "flow_uuid": flow_uuid,
        "mapping_template_uuid": mapping_template_uuid,
        "manual_pipeline": pipeline_result,
        "mailing_association": {
            "status": "queued",
            "task_id": association_task.id,
            "queue": settings.celery_fileapp_mailing_assoc_queue,
            "countdown_seconds": int(settings.celery_fileapp_mailing_assoc_delay_seconds),
        },
    }


async def _associate_fileapp_mailing_task(
    *,
    task,  # celery task bind
    workspace_uuid: str,
    flow_uuid: str,
    mailing_uuid: str,
    linked_by: str | None,
) -> dict[str, Any]:
    settings = get_settings()
    session_factory = get_session_factory()
    detach_all_files = False
    async with session_factory() as db_session:
        safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)
        workspace_api_key = await fetch_workspace_otima_billing_api_key(
            db_session,
            workspace_uuid=safe_workspace_uuid,
        )
        detach_all_files = await resolve_detach_all_files(
            db_session,
            workspace_schema=workspace_schema,
            flow_uuid=flow_uuid,
        )
    result = await associate_mailing_to_flow_from_file_event(
        settings=settings,
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
        mailing_uuid=mailing_uuid,
        linked_by=linked_by,
        workspace_api_key=workspace_api_key,
        detach_all_files=detach_all_files,
    )
    if result.get("status") in {"error", "pending"}:
        retries = int(task.request.retries or 0)
        countdown = _RETRY_DELAYS[min(retries, len(_RETRY_DELAYS) - 1)]
        raise task.retry(
            exc=RuntimeError(f"mailing_association_not_ready:{result.get('reason')}"),
            countdown=countdown,
        )
    return {
        "status": "done",
        "workspace_uuid": workspace_uuid,
        "flow_uuid": flow_uuid,
        "mailing_uuid": mailing_uuid,
        "result": result,
    }
