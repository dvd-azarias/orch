# Referência: app/tasks/webhook.py
#
# Tasks Celery utilizadas pelo fluxo de auto-mailing via evento S3.

from __future__ import annotations

import logging
from typing import Any, Dict

from celery import Celery

from app.config import get_settings
from app.services.s3_files_auto_mailing import process_s3_files_event
from app.tasks.source_list_ingestion import ingest_source_list_task

LOGGER = logging.getLogger("target_core.tasks.webhook")
settings = get_settings()

celery_webhook = Celery(main="celery.webhook", broker=settings.celery_broker_url)


@celery_webhook.task(bind=True, name="webhook.ingest_s3_files_event", autoretry_for=(), retry_backoff=False)
def ingest_s3_files_event_task(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {"status": "ignored", "reason": "invalid_payload_type"}

    try:
        task = process_s3_files_event_task.apply_async(
            kwargs={"payload": payload},
            queue=settings.celery_s3_files_event_queue,
            routing_key=settings.celery_s3_files_event_queue,
        )
    except Exception as exc:
        retries = self.request.retries or 0
        delay = (30, 120, 300)[min(retries, 2)]
        raise self.retry(exc=exc, countdown=delay)

    LOGGER.info(
        "webhook.s3_processing_enqueued",
        extra={
            "queue": settings.celery_s3_files_event_queue,
            "task_id": task.id,
        },
    )
    return {"status": "queued", "reason": "processing_enqueued", "process_task_id": task.id}


@celery_webhook.task(name="webhook.process_s3_files_event")
def process_s3_files_event_task(*, payload: Dict[str, Any]) -> Dict[str, Any]:
    result = process_s3_files_event(payload)
    if result.get("status") != "ready":
        return result

    workspace_uuid = str(result["workspace_uuid"])
    flow_id = str(result["flow_id"])
    internal_mailing_id = int(result["internal_mailing_id"])

    task = ingest_source_list_task.apply_async(
        kwargs={
            "workspace_uuid": workspace_uuid,
            "source_list_id": internal_mailing_id,
            "flow_id": flow_id,
        },
        queue=settings.celery_source_list_ingest_queue,
        routing_key=settings.celery_source_list_ingest_queue,
    )
    LOGGER.info(
        "webhook.s3_source_list_ingest_enqueued",
        extra={
            "workspace_uuid": workspace_uuid,
            "flow_id": flow_id,
            "source_list_id": internal_mailing_id,
            "queue": settings.celery_source_list_ingest_queue,
            "task_id": task.id,
        },
    )
    result["ingest_task_id"] = task.id
    return result
