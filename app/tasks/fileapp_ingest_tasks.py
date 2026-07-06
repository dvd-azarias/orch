from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
import redis
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
    download_file_bytes_for_file_event,
    run_tipo1_manual_pipeline,
)
from app.services.fileapp_tipo1_service import resolve_detach_all_files
from app.services.fileapp_processed_file_service import (
    FileAppProcessedFileError,
    move_processed_file_to_processados,
    quarantine_file_to_falha,
)
from app.services.fileapp_mailing_association_service import associate_mailing_to_flow_from_file_event
from app.services.orch_trigger_service import process_single_payload
from app.repositories.workspaces_repository import fetch_workspace_otima_billing_api_key
from app.services.alarm_service import persist_alarm
from app.services.workspace_service import bind_workspace_context, list_completed_workspaces

logger = get_logger(__name__)

_RETRY_DELAYS = (30, 120, 300)
_STEP1_UPLOAD_RETRY_DELAYS = (15, 30, 60, 120, 300, 600)
_STEP6_IMPORT_RETRY_DELAYS = (5, 15, 30, 60, 120, 300)


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


def _is_retryable_step1_upload_failure(result: dict[str, Any]) -> bool:
    if str(result.get("status") or "").strip().lower() != "failed":
        return False
    if str(result.get("reason") or "").strip().lower() != "step1_upload":
        return False
    details = result.get("details")
    status_code: int | None = None
    if isinstance(details, dict):
        raw_status = details.get("status_code")
        if isinstance(raw_status, int):
            status_code = raw_status
        elif isinstance(raw_status, str) and raw_status.strip().isdigit():
            status_code = int(raw_status.strip())
    if status_code is None:
        return True
    if status_code == 429:
        return True
    return 500 <= status_code <= 599


def _is_retryable_step6_import_conflict(result: dict[str, Any]) -> bool:
    if str(result.get("status") or "").strip().lower() != "failed":
        return False
    if str(result.get("reason") or "").strip().lower() != "step6_import":
        return False
    details = result.get("details")
    status_code: int | None = None
    response_body = ""
    if isinstance(details, dict):
        raw_status = details.get("status_code")
        if isinstance(raw_status, int):
            status_code = raw_status
        elif isinstance(raw_status, str) and raw_status.strip().isdigit():
            status_code = int(raw_status.strip())
        response_body = str(details.get("response_body") or "")
    if status_code != 409:
        return False
    lowered = response_body.lower()
    return "processo de ingest" in lowered or "already ingest" in lowered or not lowered


async def _persist_step1_retry_alarm(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    payload: dict[str, Any],
    result: dict[str, Any],
    code: str,
    message: str,
    details: dict[str, Any],
    level: str = "warning",
) -> None:
    session_factory = get_session_factory()
    file_data = payload.get("file") if isinstance(payload.get("file"), dict) else {}
    file_id = str(file_data.get("id") or "").strip()
    file_folder_path = str(file_data.get("folder_path") or "").strip()
    file_original_name = str(file_data.get("original_name") or "").strip()
    file_url = str(file_data.get("url") or "").strip()
    async with session_factory() as db_session:
        bind_workspace_context(workspace_uuid)
        await persist_alarm(
            db_session,
            level=level,
            code=code,
            message=message,
            details={
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "result": result,
                "file": {
                    "id": file_id,
                    "folder_path": file_folder_path,
                    "original_name": file_original_name,
                    "url": file_url,
                },
                **details,
            },
            flow_uuid=flow_uuid,
            app_name="ArquivosApp",
            entity=str(file_data.get("id") or ""),
            entity_type="file",
            entity_address=str(file_data.get("folder_path") or ""),
        )


def _extract_file_id_from_url(file_url: str) -> str | None:
    parsed = urlparse(str(file_url or "").strip())
    path = parsed.path.strip("/")
    if not path:
        return None
    file_id = path.rsplit("/", 1)[-1].strip()
    return file_id or None


def _fileapp_post_process_lock_key(*, workspace_uuid: str, source_list_id: int) -> str:
    return f"orch:fileapp:post-process:{workspace_uuid}:{source_list_id}"


def _fileapp_post_process_file_lock_key(*, workspace_uuid: str, file_id: str) -> str:
    return f"orch:fileapp:post-process:file:{workspace_uuid}:{file_id}"


def _try_acquire_fileapp_post_process_lock(
    redis_client: redis.Redis | None,
    *,
    workspace_uuid: str,
    source_list_id: int,
    cooldown_seconds: int,
) -> bool:
    if redis_client is None:
        return True


def _try_acquire_fileapp_post_process_file_lock(
    redis_client: redis.Redis | None,
    *,
    workspace_uuid: str,
    file_id: str,
    cooldown_seconds: int,
) -> bool:
    if redis_client is None:
        return True
    try:
        return bool(
            redis_client.set(
                _fileapp_post_process_file_lock_key(workspace_uuid=workspace_uuid, file_id=file_id),
                "1",
                ex=max(1, int(cooldown_seconds)),
                nx=True,
            )
        )
    except Exception:
        logger.exception(
            "fileapp.post_process_reconcile.file_lock_failed",
            extra={
                "event": "orch.fileapp.post_process_reconcile.file_lock_failed",
                "workspace_uuid": workspace_uuid,
                "file_id": file_id,
            },
        )
        return True
    try:
        return bool(
            redis_client.set(
                _fileapp_post_process_lock_key(workspace_uuid=workspace_uuid, source_list_id=source_list_id),
                "1",
                ex=max(1, int(cooldown_seconds)),
                nx=True,
            )
        )
    except Exception:
        logger.exception(
            "fileapp.post_process_reconcile.lock_failed",
            extra={
                "event": "orch.fileapp.post_process_reconcile.lock_failed",
                "workspace_uuid": workspace_uuid,
                "source_list_id": source_list_id,
            },
        )
        return True


async def _fetch_fileapp_post_process_candidates(
    db_session,
    *,
    workspace_schema: str,
    limit: int,
) -> list[dict[str, Any]]:
    safe_schema = workspace_schema.replace('"', '""')
    result = await db_session.execute(
        text(
            f"""
            SELECT
                id,
                file_name,
                file_path,
                file_url
            FROM "{safe_schema}".source_lists
            WHERE UPPER(COALESCE(status, '')) = 'PROCESSED'
              AND COALESCE(file_name, '') <> ''
              AND COALESCE(file_path, '') <> ''
              AND COALESCE(file_url, '') <> ''
            ORDER BY COALESCE(updated_at, created_at) DESC
            LIMIT :limit
            """
        ),
        {"limit": max(1, int(limit))},
    )
    rows: list[dict[str, Any]] = []
    for row in result.mappings().all():
        rows.append(
            {
                "id": int(row["id"]),
                "file_name": str(row["file_name"] or "").strip(),
                "file_path": str(row["file_path"] or "").strip(),
                "file_url": str(row["file_url"] or "").strip(),
            }
        )
    return rows


async def _fetch_exhausted_quarantine_candidates(
    db_session,
    *,
    workspace_schema: str,
    limit: int,
) -> list[dict[str, Any]]:
    safe_schema = workspace_schema.replace('"', '""')
    result = await db_session.execute(
        text(
            f"""
            SELECT id, code, details
            FROM "{safe_schema}".orch_sessions_alarms
            WHERE code IN (
                'fileapp_tipo1_step1_upload_retry_exhausted',
                'fileapp_tipo1_step6_import_retry_exhausted'
            )
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": max(1, int(limit))},
    )
    rows: list[dict[str, Any]] = []
    seen_file_ids: set[str] = set()
    for row in result.mappings().all():
        details = row["details"] if isinstance(row["details"], dict) else {}
        file_info = details.get("file") if isinstance(details.get("file"), dict) else {}
        file_id = str(file_info.get("id") or "").strip()
        folder_path = str(file_info.get("folder_path") or "").strip()
        original_name = str(file_info.get("original_name") or "").strip()
        file_url = str(file_info.get("url") or "").strip()
        if not file_id or not folder_path or not original_name:
            continue
        if file_id in seen_file_ids:
            continue
        seen_file_ids.add(file_id)
        retry_step = str(details.get("retry_step") or "").strip()
        rows.append(
            {
                "alarm_id": int(row["id"]),
                "alarm_code": str(row["code"] or "").strip(),
                "file_id": file_id,
                "folder_path": folder_path,
                "original_name": original_name,
                "file_url": file_url,
                "retry_step": retry_step,
            }
        )
    return rows


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


@celery_app.task(name="app.tasks.fileapp.process_tipo1_event", bind=True, ignore_result=True, max_retries=6)
def process_fileapp_tipo1_event_task(
    self,
    *,
    workspace_uuid: str,
    flow_uuid: str,
    payload: dict[str, Any],
    mapping_template_uuid: str,
) -> dict[str, Any]:
    result = asyncio.run(
        _process_fileapp_tipo1_event_task(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            payload=payload,
            mapping_template_uuid=mapping_template_uuid,
        )
    )
    retry_step: str | None = None
    retry_delays: tuple[int, ...] | None = None
    retry_alarm_code_scheduled: str | None = None
    retry_alarm_code_exhausted: str | None = None
    retry_alarm_message_scheduled: str | None = None
    retry_alarm_message_exhausted: str | None = None

    if _is_retryable_step1_upload_failure(result):
        retry_step = "step1_upload"
        retry_delays = _STEP1_UPLOAD_RETRY_DELAYS
        retry_alarm_code_scheduled = "fileapp_tipo1_step1_upload_retry_scheduled"
        retry_alarm_code_exhausted = "fileapp_tipo1_step1_upload_retry_exhausted"
        retry_alarm_message_scheduled = "Falha transitória no upload (step1). Retry automático agendado."
        retry_alarm_message_exhausted = "Falha no upload (step1) após esgotar retries automáticos."
    elif _is_retryable_step6_import_conflict(result):
        retry_step = "step6_import"
        retry_delays = _STEP6_IMPORT_RETRY_DELAYS
        retry_alarm_code_scheduled = "fileapp_tipo1_step6_import_retry_scheduled"
        retry_alarm_code_exhausted = "fileapp_tipo1_step6_import_retry_exhausted"
        retry_alarm_message_scheduled = "Conflito transitório no import (step6). Retry automático agendado."
        retry_alarm_message_exhausted = "Conflito no import (step6) após esgotar retries automáticos."
    else:
        return result

    retries = int(self.request.retries or 0)
    attempt = retries + 1
    max_attempts = int(self.max_retries or 0) + 1
    assert retry_delays is not None
    assert retry_alarm_code_scheduled is not None
    assert retry_alarm_code_exhausted is not None
    assert retry_alarm_message_scheduled is not None
    assert retry_alarm_message_exhausted is not None
    countdown = retry_delays[min(retries, len(retry_delays) - 1)]

    if retries >= int(self.max_retries or 0):
        quarantine_result: dict[str, Any] | None = None
        quarantine_error: dict[str, Any] | None = None
        if retry_step == "step1_upload":
            file_payload = payload.get("file") if isinstance(payload.get("file"), dict) else {}
            if (
                str(file_payload.get("id") or "").strip()
                and str(file_payload.get("folder_path") or "").strip()
                and str(file_payload.get("original_name") or "").strip()
            ):
                try:
                    quarantine_result = asyncio.run(
                        quarantine_file_to_falha(
                            settings=get_settings(),
                            workspace_uuid=workspace_uuid,
                            payload=payload,
                        )
                    )
                except FileAppProcessedFileError as exc:
                    quarantine_error = {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    }
                except Exception as exc:  # pragma: no cover
                    quarantine_error = {
                        "code": "unexpected_quarantine_error",
                        "message": str(exc),
                        "details": {"exception_type": type(exc).__name__},
                    }

        asyncio.run(
            _persist_step1_retry_alarm(
                workspace_uuid=workspace_uuid,
                flow_uuid=flow_uuid,
                payload=payload,
                result=result,
                code=retry_alarm_code_exhausted,
                message=retry_alarm_message_exhausted,
                details={
                    "retry_step": retry_step,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "quarantine_result": quarantine_result,
                    "quarantine_error": quarantine_error,
                },
                level="error",
            )
        )
        return {
            **result,
            "status": "failed_final",
            "retry_exhausted": True,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "quarantine_result": quarantine_result,
            "quarantine_error": quarantine_error,
        }

    asyncio.run(
        _persist_step1_retry_alarm(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            payload=payload,
            result=result,
            code=retry_alarm_code_scheduled,
            message=retry_alarm_message_scheduled,
            details={
                "retry_step": retry_step,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "countdown_seconds": countdown,
            },
            level="warning",
        )
    )
    raise self.retry(
        exc=RuntimeError(
            f"{retry_step} transient failure (attempt {attempt}/{max_attempts})"
        ),
        countdown=countdown,
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


@celery_app.task(name="app.tasks.fileapp.reconcile_post_process", ignore_result=True)
def reconcile_fileapp_post_process_task() -> dict[str, int]:
    return asyncio.run(_reconcile_fileapp_post_process_task())


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
        downloaded_file_bytes = await download_file_bytes_for_file_event(
            settings=settings,
            payload=payload,
            default_workspace_uuid=safe_workspace_uuid,
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
            predownloaded_file_bytes=downloaded_file_bytes,
            upload_file_name_override=original_name,
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
            "upload_file_name": original_name,
        },
    )

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

    post_process_status: dict[str, Any] | None = None
    try:
        post_process_result = await move_processed_file_to_processados(
            settings=settings,
            workspace_uuid=safe_workspace_uuid,
            payload=payload,
        )
        post_process_status = {
            "status": "done",
            "result": post_process_result,
        }
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
        post_process_status = {
            "status": "warning",
            "error_code": exc.code,
            "error_message": exc.message,
            "error_details": exc.details,
        }
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
    except Exception as exc:
        post_process_status = {
            "status": "warning",
            "error_code": "unexpected_error",
            "error_message": str(exc),
            "error_details": {"exception_type": type(exc).__name__},
        }
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
        "post_process_file": post_process_status or {"status": "skipped"},
    }


async def _reconcile_fileapp_post_process_task() -> dict[str, int]:
    settings = get_settings()
    if not settings.celery_enabled or not settings.celery_fileapp_ingest_enabled:
        return {
            "workspaces_scanned": 0,
            "candidates_scanned": 0,
            "moved": 0,
            "quarantined": 0,
            "exhausted_quarantined": 0,
            "warnings": 0,
        }

    redis_client: redis.Redis | None = None
    if settings.celery_result_backend:
        try:
            redis_client = redis.Redis.from_url(settings.celery_result_backend)
        except Exception:
            logger.exception(
                "fileapp.post_process_reconcile.redis_setup_failed",
                extra={"event": "orch.fileapp.post_process_reconcile.redis_setup_failed"},
            )

    workspace_scope = str(settings.celery_fileapp_post_process_reconcile_workspace_uuid or "").strip() or None
    session_factory = get_session_factory()
    workspaces_scanned = 0
    candidates_scanned = 0
    moved = 0
    quarantined = 0
    exhausted_quarantined = 0
    warnings = 0
    async with session_factory() as db_session:
        try:
            workspaces = await list_completed_workspaces(db_session)
        except Exception as exc:
            await persist_alarm(
                db_session,
                level="error",
                code="fileapp_post_process_reconcile_task_failed",
                message="Falha inesperada no reconciliador de pós-processamento FileApp.",
                details={
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
                app_name="Celery",
            )
            raise

        for workspace in workspaces:
            workspace_uuid = str(workspace.get("workspace_uuid") or "").strip()
            if not workspace_uuid:
                continue
            if workspace_scope and workspace_uuid != workspace_scope:
                continue

            workspaces_scanned += 1
            safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)
            candidates = await _fetch_fileapp_post_process_candidates(
                db_session,
                workspace_schema=workspace_schema,
                limit=settings.celery_fileapp_post_process_reconcile_batch_size,
            )
            for item in candidates:
                source_list_id = int(item["id"])
                if not _try_acquire_fileapp_post_process_lock(
                    redis_client,
                    workspace_uuid=safe_workspace_uuid,
                    source_list_id=source_list_id,
                    cooldown_seconds=settings.celery_fileapp_post_process_reconcile_cooldown_seconds,
                ):
                    continue

                file_id = _extract_file_id_from_url(item["file_url"])
                if not file_id:
                    continue

                candidates_scanned += 1
                payload = {
                    "file": {
                        "id": file_id,
                        "folder_path": item["file_path"],
                        "original_name": item["file_name"],
                        "url": item["file_url"],
                    }
                }
                try:
                    result = await move_processed_file_to_processados(
                        settings=settings,
                        workspace_uuid=safe_workspace_uuid,
                        payload=payload,
                    )
                    if str(result.get("quarantine_folder") or "").strip().lower() == "falha":
                        quarantined += 1
                    elif str(result.get("status") or "").strip().lower() == "done":
                        moved += 1
                except FileAppProcessedFileError as exc:
                    warnings += 1
                    await persist_alarm(
                        db_session,
                        level="warning",
                        code="fileapp_post_process_reconcile_failed",
                        message="Falha ao reconciliar pós-processamento de source_list FileApp.",
                        details={
                            "workspace_uuid": safe_workspace_uuid,
                            "source_list_id": source_list_id,
                            "error_code": exc.code,
                            "error_message": exc.message,
                            "error_details": exc.details,
                        },
                        app_name="ArquivosApp",
                        entity=str(source_list_id),
                        entity_type="source_list",
                        entity_address=str(item["file_path"]),
                    )
                except Exception as exc:
                    warnings += 1
                    await persist_alarm(
                        db_session,
                        level="warning",
                        code="fileapp_post_process_reconcile_unexpected_error",
                        message="Erro inesperado no reconciliador de pós-processamento FileApp.",
                        details={
                            "workspace_uuid": safe_workspace_uuid,
                            "source_list_id": source_list_id,
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                        },
                        app_name="ArquivosApp",
                        entity=str(source_list_id),
                        entity_type="source_list",
                        entity_address=str(item["file_path"]),
                    )

            exhausted_candidates = await _fetch_exhausted_quarantine_candidates(
                db_session,
                workspace_schema=workspace_schema,
                limit=settings.celery_fileapp_post_process_reconcile_batch_size,
            )
            for item in exhausted_candidates:
                file_id = str(item.get("file_id") or "").strip()
                if not file_id:
                    continue
                if not _try_acquire_fileapp_post_process_file_lock(
                    redis_client,
                    workspace_uuid=safe_workspace_uuid,
                    file_id=file_id,
                    cooldown_seconds=settings.celery_fileapp_post_process_reconcile_cooldown_seconds,
                ):
                    continue
                payload = {
                    "file": {
                        "id": file_id,
                        "folder_path": str(item.get("folder_path") or ""),
                        "original_name": str(item.get("original_name") or ""),
                        "url": str(item.get("file_url") or ""),
                    }
                }
                try:
                    result = await quarantine_file_to_falha(
                        settings=settings,
                        workspace_uuid=safe_workspace_uuid,
                        payload=payload,
                    )
                    if str(result.get("status") or "").strip().lower() in {"done", "skipped"}:
                        exhausted_quarantined += 1
                except FileAppProcessedFileError as exc:
                    warnings += 1
                    await persist_alarm(
                        db_session,
                        level="warning",
                        code="fileapp_post_process_reconcile_exhausted_quarantine_failed",
                        message="Falha ao quarentenar arquivo com retry esgotado no pipeline tipo1.",
                        details={
                            "workspace_uuid": safe_workspace_uuid,
                            "alarm_id": int(item.get("alarm_id") or 0),
                            "alarm_code": str(item.get("alarm_code") or ""),
                            "retry_step": str(item.get("retry_step") or ""),
                            "file_id": file_id,
                            "error_code": exc.code,
                            "error_message": exc.message,
                            "error_details": exc.details,
                        },
                        app_name="ArquivosApp",
                        entity=file_id,
                        entity_type="file",
                        entity_address=str(item.get("folder_path") or ""),
                    )
                except Exception as exc:
                    warnings += 1
                    await persist_alarm(
                        db_session,
                        level="warning",
                        code="fileapp_post_process_reconcile_exhausted_quarantine_unexpected_error",
                        message="Erro inesperado ao quarentenar arquivo com retry esgotado no pipeline tipo1.",
                        details={
                            "workspace_uuid": safe_workspace_uuid,
                            "alarm_id": int(item.get("alarm_id") or 0),
                            "alarm_code": str(item.get("alarm_code") or ""),
                            "retry_step": str(item.get("retry_step") or ""),
                            "file_id": file_id,
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                        },
                        app_name="ArquivosApp",
                        entity=file_id,
                        entity_type="file",
                        entity_address=str(item.get("folder_path") or ""),
                    )

    logger.info(
        "fileapp.post_process_reconcile.finished",
        extra={
            "event": "orch.fileapp.post_process_reconcile.finished",
            "workspace_scope": workspace_scope,
            "workspaces_scanned": workspaces_scanned,
            "candidates_scanned": candidates_scanned,
            "moved": moved,
            "quarantined": quarantined,
            "exhausted_quarantined": exhausted_quarantined,
            "warnings": warnings,
        },
    )
    return {
        "workspaces_scanned": workspaces_scanned,
        "candidates_scanned": candidates_scanned,
        "moved": moved,
        "quarantined": quarantined,
        "exhausted_quarantined": exhausted_quarantined,
        "warnings": warnings,
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
