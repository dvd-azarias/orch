# Referência: app/api/routers/webhook.py
#
# Trecho central responsável por receber eventos S3 e disparar o pipeline
# assíncrono de auto-mailing.

from __future__ import annotations

import logging
from json import JSONDecodeError
from typing import Any, Dict

import httpx
from fastapi import Body, Request, status

from app.config import get_settings
from app.tasks.webhook import ingest_s3_files_event_task

LOGGER = logging.getLogger("target_core.api.webhook")


def _sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    sanitized: Dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if any(token in lowered for token in ("authorization", "secret", "token")):
            sanitized[key] = "***"
        else:
            sanitized[key] = value
    return sanitized


async def receive_s3_files_webhook(request: Request, payload: Any = Body(default=None)) -> Dict[str, str]:
    settings = get_settings()
    body = payload
    if body is None:
        try:
            body = await request.json()
        except (JSONDecodeError, ValueError):
            raw_body = await request.body()
            body = raw_body.decode("utf-8", errors="replace") if raw_body else None

    headers = _sanitize_headers(dict(request.headers))
    LOGGER.info("webhook.s3_files.received", extra={"headers": headers, "payload": body})

    if settings.s3_arquivos_app_forward_event:
        forward_url = (settings.s3_arquivos_app_forward_url or "").strip()
        if not forward_url:
            LOGGER.warning("webhook.s3_files.forward_url_missing")
        else:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    if isinstance(body, dict):
                        await client.post(forward_url, json=body)
                    elif isinstance(body, (bytes, bytearray)):
                        await client.post(forward_url, content=body)
                    elif body is None:
                        await client.post(forward_url, content=b"")
                    else:
                        await client.post(forward_url, content=str(body).encode("utf-8"))
            except Exception:
                LOGGER.exception("webhook.s3_files.forward_failed", extra={"url": forward_url})

    if settings.s3_arquivos_app_auto_mailing_enabled and isinstance(body, dict):
        try:
            task = ingest_s3_files_event_task.apply_async(
                kwargs={"payload": body},
                queue=settings.celery_s3_files_ingest_queue,
                routing_key=settings.celery_s3_files_ingest_queue,
            )
            LOGGER.info(
                "webhook.s3_files.auto_mailing_enqueued",
                extra={
                    "task_id": task.id,
                    "queue": settings.celery_s3_files_ingest_queue,
                },
            )
        except Exception:
            LOGGER.exception("webhook.s3_files.auto_mailing_enqueue_failed")

    return {"status": "ok", "code": status.HTTP_200_OK}
