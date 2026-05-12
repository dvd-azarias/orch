from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.services.app_detector import APP_ARQUIVOS, detect_app
from app.services.file_event_ingest_service import expand_arquivos_payload_into_rows
from app.services.fileapp_mailing_association_service import associate_mailing_to_flow_from_file_event
from app.services.fileapp_tipo1_service import (
    SourceListStatus,
    count_rows_without_channel,
    create_source_list_for_file_event,
    load_mapping_items,
    resolve_person_ids_for_rows,
    resolve_mapping_template_id,
    update_source_list_after_ingest,
    upsert_persons_from_rows,
)
from app.services.orch_trigger_service import process_single_payload
from app.repositories.workspaces_repository import fetch_workspace_otima_billing_api_key
from app.services.workspace_service import bind_workspace_context

logger = get_logger(__name__)

_RETRY_DELAYS = (30, 120, 300)


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
    async with session_factory() as db_session:
        safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)
        payloads = await expand_arquivos_payload_into_rows(payload, settings=settings)
        rows: list[dict[str, Any]] = []
        for item in payloads:
            file_data = item.get("file")
            if isinstance(file_data, dict):
                content = file_data.get("content")
                if isinstance(content, dict):
                    rows.append(content)

        template_id = await resolve_mapping_template_id(
            db_session,
            workspace_schema=workspace_schema,
            mapping_template_uuid=mapping_template_uuid,
        )
        if template_id is None:
            logger.warning(
                "fileapp.tipo1.mapping_template_not_found",
                extra={
                    "event": "orch.fileapp.tipo1.mapping_template_not_found",
                    "workspace_uuid": safe_workspace_uuid,
                    "flow_uuid": flow_uuid,
                    "mapping_template_uuid": mapping_template_uuid,
                },
            )
            return {
                "status": "ignored",
                "reason": "mapping_template_not_found",
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": flow_uuid,
                "mapping_template_uuid": mapping_template_uuid,
            }

        mapping_items = await load_mapping_items(
            db_session,
            workspace_schema=workspace_schema,
            template_id=template_id,
        )
        if not mapping_items:
            logger.warning(
                "fileapp.tipo1.mapping_items_not_found",
                extra={
                    "event": "orch.fileapp.tipo1.mapping_items_not_found",
                    "workspace_uuid": safe_workspace_uuid,
                    "flow_uuid": flow_uuid,
                    "mapping_template_uuid": mapping_template_uuid,
                },
            )
            return {
                "status": "failed",
                "reason": "mapping_items_not_found",
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": flow_uuid,
                "mapping_template_uuid": mapping_template_uuid,
            }
        file_data = payload.get("file") if isinstance(payload.get("file"), dict) else {}
        linked_by = str(file_data.get("id", "")).strip() if isinstance(file_data, dict) else ""
        source_list_id: int | None = None
        source_list_uuid: str | None = None
        if db_session.in_transaction():
            await db_session.commit()
        async with db_session.begin():
            source_list_id, source_list_uuid = await create_source_list_for_file_event(
                db_session,
                workspace_schema=workspace_schema,
                file_data=file_data if isinstance(file_data, dict) else {},
                template_id=template_id,
                rows_total=len(rows),
            )
        session_rows = 0
        session_failed_rows = 0
        source_list_status = SourceListStatus.ERROR
        async with db_session.begin():
            processed_rows, skipped_rows = await upsert_persons_from_rows(
                db_session,
                workspace_schema=workspace_schema,
                rows=rows,
                mapping_items=mapping_items,
                source_list_id=source_list_id,
            )
            person_ids = await resolve_person_ids_for_rows(
                db_session,
                workspace_schema=workspace_schema,
                rows=rows,
                mapping_items=mapping_items,
            )
            for idx, item_payload in enumerate(payloads):
                try:
                    item_payload_with_type = dict(item_payload)
                    item_payload_with_type["mapping_template_id"] = mapping_template_uuid
                    file_data = item_payload_with_type.get("file")
                    person_id = person_ids[idx] if idx < len(person_ids) else None
                    if isinstance(file_data, dict) and person_id:
                        file_data["person_id"] = person_id
                    await process_single_payload(
                        safe_workspace_uuid=safe_workspace_uuid,
                        workspace_schema=workspace_schema,
                        flow_uuid=flow_uuid,
                        payload=item_payload_with_type,
                        db_session=db_session,
                        app_name=app_name,
                    )
                    session_rows += 1
                except Exception:
                    session_failed_rows += 1
                    logger.exception(
                        "fileapp.tipo1.process_event.session_row_failed",
                        extra={
                            "event": "orch.fileapp.tipo1.process_event.session_row_failed",
                            "workspace_uuid": safe_workspace_uuid,
                            "flow_uuid": flow_uuid,
                            "mapping_template_uuid": mapping_template_uuid,
                        },
                    )
            if source_list_id:
                rows_without_channel = count_rows_without_channel(rows=rows, mapping_items=mapping_items)
                if session_rows > 0 and session_failed_rows == 0:
                    source_list_status = SourceListStatus.READY_TO_INGEST
                else:
                    source_list_status = SourceListStatus.ERROR
                await update_source_list_after_ingest(
                    db_session,
                    workspace_schema=workspace_schema,
                    source_list_id=int(source_list_id),
                    status=source_list_status,
                    rows_processed=session_rows,
                    rows_discarded=skipped_rows,
                    rows_error=session_failed_rows,
                    rows_without_channel=rows_without_channel,
                )

        mailing_association: dict[str, Any]
        if source_list_uuid and source_list_status == SourceListStatus.READY_TO_INGEST:
            assoc_task = associate_fileapp_mailing_task.apply_async(
                kwargs={
                    "workspace_uuid": safe_workspace_uuid,
                    "flow_uuid": flow_uuid,
                    "mailing_uuid": source_list_uuid,
                    "linked_by": linked_by or None,
                },
                queue=settings.celery_fileapp_mailing_assoc_queue,
                routing_key=settings.celery_fileapp_mailing_assoc_queue,
            )
            mailing_association = {
                "status": "queued",
                "task_id": assoc_task.id,
                "queue": settings.celery_fileapp_mailing_assoc_queue,
                "mailing_uuid": source_list_uuid,
            }
        else:
            mailing_association = {
                "status": "ignored",
                "reason": "source_list_not_ready",
                "source_list_status": source_list_status,
            }

    logger.info(
        "fileapp.tipo1.process_event.finished",
        extra={
            "event": "orch.fileapp.tipo1.process_event.finished",
            "workspace_uuid": workspace_uuid,
            "flow_uuid": flow_uuid,
            "mapping_template_uuid": mapping_template_uuid,
            "processed_rows": processed_rows,
            "skipped_rows": skipped_rows,
            "session_rows": session_rows,
            "session_failed_rows": session_failed_rows,
            "mailing_association": mailing_association,
        },
    )
    return {
        "status": "done",
        "workspace_uuid": workspace_uuid,
        "flow_uuid": flow_uuid,
        "mapping_template_uuid": mapping_template_uuid,
        "processed_rows": processed_rows,
        "skipped_rows": skipped_rows,
        "session_rows": session_rows,
        "session_failed_rows": session_failed_rows,
        "mailing_association": mailing_association,
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
    async with session_factory() as db_session:
        workspace_api_key = await fetch_workspace_otima_billing_api_key(
            db_session,
            workspace_uuid=workspace_uuid,
        )
    result = await associate_mailing_to_flow_from_file_event(
        settings=settings,
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
        mailing_uuid=mailing_uuid,
        linked_by=linked_by,
        workspace_api_key=workspace_api_key,
    )
    if result.get("status") == "error":
        retries = int(task.request.retries or 0)
        countdown = _RETRY_DELAYS[min(retries, len(_RETRY_DELAYS) - 1)]
        raise task.retry(
            exc=RuntimeError(f"mailing_association_failed:{result.get('reason')}"),
            countdown=countdown,
        )
    return {
        "status": "done",
        "workspace_uuid": workspace_uuid,
        "flow_uuid": flow_uuid,
        "mailing_uuid": mailing_uuid,
        "result": result,
    }
