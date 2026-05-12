from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _post_json(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> tuple[int, str]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url=url,
        data=raw,
        headers=headers,
        method="POST",
    )
    with request.urlopen(req, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8", errors="replace")
        return int(response.status), body


async def associate_mailing_to_flow_from_file_event(
    *,
    settings: Settings,
    workspace_uuid: str,
    flow_uuid: str,
    mailing_uuid: str | None,
    linked_by: str | None,
    workspace_api_key: str | None = None,
) -> dict[str, Any]:
    if not mailing_uuid:
        return {"status": "ignored", "reason": "mailing_not_resolved"}

    base_url = str(settings.sync_webhook_base_url or "").strip().rstrip("/")
    if not base_url:
        return {"status": "ignored", "reason": "sync_webhook_base_url_not_configured"}

    bearer_token = str(settings.target_core_api_bearer_token or "").strip()
    fallback_workspace_api_key = str(workspace_api_key or "").strip()
    if not bearer_token and not fallback_workspace_api_key:
        return {"status": "ignored", "reason": "target_core_api_bearer_token_not_configured"}

    target_url = f"{base_url}/v2/flow/{flow_uuid}/mailings"
    body = {
        "mailing_ids_added": [mailing_uuid],
        "mailing_ids_removed": [],
        "linked_by": linked_by,
        "call_origin": "file_event",
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "x-application": "target",
        "X-WORKSPACE-UUID": workspace_uuid,
    }
    if bearer_token:
        headers["authorization"] = f"Bearer {bearer_token}"
    if fallback_workspace_api_key:
        headers["x-api-key"] = fallback_workspace_api_key
        headers["x-workspace-api-key"] = fallback_workspace_api_key

    try:
        status_code, response_body = await asyncio.to_thread(
            _post_json,
            url=target_url,
            headers=headers,
            payload=body,
            timeout_seconds=settings.sync_ws_timeout_seconds,
        )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.warning(
            "fileapp.tipo1.mailing_association.http_error",
            extra={
                "event": "orch.fileapp.tipo1.mailing_association.http_error",
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "mailing_uuid": mailing_uuid,
                "status_code": int(exc.code),
                "response_body": detail,
            },
        )
        return {
            "status": "error",
            "reason": "http_error",
            "status_code": int(exc.code),
            "response_body": detail,
        }
    except URLError as exc:
        logger.warning(
            "fileapp.tipo1.mailing_association.url_error",
            extra={
                "event": "orch.fileapp.tipo1.mailing_association.url_error",
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "mailing_uuid": mailing_uuid,
                "reason": str(exc.reason),
            },
        )
        return {
            "status": "error",
            "reason": "url_error",
            "error_detail": str(exc.reason),
        }
    except Exception as exc:
        logger.exception(
            "fileapp.tipo1.mailing_association.unexpected_error",
            extra={
                "event": "orch.fileapp.tipo1.mailing_association.unexpected_error",
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "mailing_uuid": mailing_uuid,
            },
        )
        return {
            "status": "error",
            "reason": type(exc).__name__,
            "error_detail": str(exc),
        }

    logger.info(
        "fileapp.tipo1.mailing_association.done",
        extra={
            "event": "orch.fileapp.tipo1.mailing_association.done",
            "workspace_uuid": workspace_uuid,
            "flow_uuid": flow_uuid,
            "mailing_uuid": mailing_uuid,
            "status_code": status_code,
        },
    )
    return {
        "status": "done",
        "status_code": status_code,
        "response_body": response_body,
    }
