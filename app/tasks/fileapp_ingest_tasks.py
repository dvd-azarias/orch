from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

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
from app.services.fileapp_tipo1_service import (
    extract_monitored_folders_from_orchestration_trigger,
    resolve_detach_all_files,
    resolve_mapping_template_uuid,
)
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


def _normalize_folder_path(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized.strip("/")


def _fileapp_post_process_lock_key(*, workspace_uuid: str, source_list_id: int) -> str:
    return f"orch:fileapp:post-process:{workspace_uuid}:{source_list_id}"


def _fileapp_post_process_file_lock_key(*, workspace_uuid: str, file_id: str) -> str:
    return f"orch:fileapp:post-process:file:{workspace_uuid}:{file_id}"


def _fileapp_entrada_rescue_file_lock_key(*, workspace_uuid: str, file_id: str) -> str:
    return f"orch:fileapp:entrada-rescue:lock:{workspace_uuid}:{file_id}"


def _fileapp_entrada_rescue_state_key(*, workspace_uuid: str, file_id: str) -> str:
    return f"orch:fileapp:entrada-rescue:state:{workspace_uuid}:{file_id}"


def _fileapp_entrada_rescue_flow_state_key(*, workspace_uuid: str, flow_uuid: str, file_id: str) -> str:
    return f"orch:fileapp:entrada-rescue:flow-state:{workspace_uuid}:{flow_uuid}:{file_id}"


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


def _try_acquire_fileapp_entrada_rescue_file_lock(
    redis_client: redis.Redis | None,
    *,
    workspace_uuid: str,
    file_id: str,
    lock_seconds: int,
) -> bool:
    if redis_client is None:
        return True
    try:
        return bool(
            redis_client.set(
                _fileapp_entrada_rescue_file_lock_key(workspace_uuid=workspace_uuid, file_id=file_id),
                "1",
                ex=max(1, int(lock_seconds)),
                nx=True,
            )
        )
    except Exception:
        logger.exception(
            "fileapp.entrada_rescue.lock_failed",
            extra={
                "event": "orch.fileapp.entrada_rescue.lock_failed",
                "workspace_uuid": workspace_uuid,
                "file_id": file_id,
            },
        )
        return True


def _get_fileapp_entrada_rescue_attempts(
    redis_client: redis.Redis | None,
    *,
    workspace_uuid: str,
    file_id: str,
) -> int:
    if redis_client is None:
        return 0
    try:
        raw = redis_client.hget(_fileapp_entrada_rescue_state_key(workspace_uuid=workspace_uuid, file_id=file_id), "attempts")
        if raw is None:
            return 0
        text_value = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
        return int(text_value.strip() or "0")
    except Exception:
        return 0


def _set_fileapp_entrada_rescue_attempts(
    redis_client: redis.Redis | None,
    *,
    workspace_uuid: str,
    file_id: str,
    attempts: int,
    ttl_seconds: int,
) -> None:
    if redis_client is None:
        return
    try:
        key = _fileapp_entrada_rescue_state_key(workspace_uuid=workspace_uuid, file_id=file_id)
        redis_client.hset(key, mapping={"attempts": str(max(0, int(attempts)))})
        redis_client.expire(key, max(60, int(ttl_seconds)))
    except Exception:
        logger.exception(
            "fileapp.entrada_rescue.state_set_failed",
            extra={
                "event": "orch.fileapp.entrada_rescue.state_set_failed",
                "workspace_uuid": workspace_uuid,
                "file_id": file_id,
            },
        )


def _clear_fileapp_entrada_rescue_attempts(
    redis_client: redis.Redis | None,
    *,
    workspace_uuid: str,
    file_id: str,
) -> None:
    if redis_client is None:
        return
    try:
        redis_client.delete(_fileapp_entrada_rescue_state_key(workspace_uuid=workspace_uuid, file_id=file_id))
    except Exception:
        logger.exception(
            "fileapp.entrada_rescue.state_clear_failed",
            extra={
                "event": "orch.fileapp.entrada_rescue.state_clear_failed",
                "workspace_uuid": workspace_uuid,
                "file_id": file_id,
            },
        )


def _get_fileapp_entrada_rescue_flow_state(
    redis_client: redis.Redis | None,
    *,
    workspace_uuid: str,
    flow_uuid: str,
    file_id: str,
) -> str | None:
    if redis_client is None:
        return None
    try:
        raw = redis_client.get(
            _fileapp_entrada_rescue_flow_state_key(
                workspace_uuid=workspace_uuid,
                flow_uuid=flow_uuid,
                file_id=file_id,
            )
        )
        if raw is None:
            return None
        text_value = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
        normalized = text_value.strip().lower()
        return normalized or None
    except Exception:
        return None


def _set_fileapp_entrada_rescue_flow_state(
    redis_client: redis.Redis | None,
    *,
    workspace_uuid: str,
    flow_uuid: str,
    file_id: str,
    state: str,
    ttl_seconds: int,
) -> None:
    if redis_client is None:
        return
    normalized_state = str(state or "").strip().lower()
    if not normalized_state:
        return
    try:
        redis_client.set(
            _fileapp_entrada_rescue_flow_state_key(
                workspace_uuid=workspace_uuid,
                flow_uuid=flow_uuid,
                file_id=file_id,
            ),
            normalized_state,
            ex=max(60, int(ttl_seconds)),
        )
    except Exception:
        logger.exception(
            "fileapp.entrada_rescue.flow_state_set_failed",
            extra={
                "event": "orch.fileapp.entrada_rescue.flow_state_set_failed",
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "file_id": file_id,
                "state": normalized_state,
            },
        )


def _try_mark_fileapp_entrada_rescue_flow_in_flight(
    redis_client: redis.Redis | None,
    *,
    workspace_uuid: str,
    flow_uuid: str,
    file_id: str,
    ttl_seconds: int,
) -> bool:
    if redis_client is None:
        return True
    key = _fileapp_entrada_rescue_flow_state_key(
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
        file_id=file_id,
    )
    try:
        return bool(redis_client.set(key, "in_flight", ex=max(60, int(ttl_seconds)), nx=True))
    except Exception:
        logger.exception(
            "fileapp.entrada_rescue.flow_state_in_flight_failed",
            extra={
                "event": "orch.fileapp.entrada_rescue.flow_state_in_flight_failed",
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "file_id": file_id,
            },
        )
        return True


def _extract_payload_file_id(payload: dict[str, Any]) -> str:
    file_payload = payload.get("file") if isinstance(payload.get("file"), dict) else {}
    return str(file_payload.get("id") or "").strip()


def _persist_process_tipo1_rescue_flow_state(
    *,
    workspace_uuid: str,
    flow_uuid: str,
    payload: dict[str, Any],
    state: str,
    ttl_seconds: int = 86400,
) -> None:
    file_id = _extract_payload_file_id(payload)
    if not file_id:
        return
    settings = get_settings()
    if not settings.celery_result_backend:
        return
    try:
        redis_client = redis.Redis.from_url(settings.celery_result_backend)
    except Exception:
        logger.exception(
            "fileapp.entrada_rescue.flow_state_redis_setup_failed",
            extra={
                "event": "orch.fileapp.entrada_rescue.flow_state_redis_setup_failed",
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "file_id": file_id,
            },
        )
        return
    _set_fileapp_entrada_rescue_flow_state(
        redis_client,
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
        file_id=file_id,
        state=state,
        ttl_seconds=ttl_seconds,
    )


def _build_files_api_headers(
    *,
    settings,
    workspace_uuid: str,
    workspace_api_key: str | None,
) -> dict[str, str]:
    headers: dict[str, str] = {"accept": "application/json", "x-application": "files"}
    client_id = str(settings.arquivos_client_id or "").strip()
    client_secret = str(settings.arquivos_client_secret or "").strip()
    has_client_credentials = bool(client_id and client_secret)
    if has_client_credentials:
        headers["x-client-id"] = client_id
        headers["x-client-secret"] = client_secret
    else:
        bearer = str(settings.target_core_api_bearer_token or "").strip()
        if bearer:
            headers["authorization"] = f"Bearer {bearer}"
    if str(workspace_uuid or "").strip():
        headers["x-workspace-uuid"] = str(workspace_uuid).strip()
    return headers


def _parse_file_datetime(raw_value: Any) -> datetime | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, datetime):
        if raw_value.tzinfo is None:
            return raw_value.replace(tzinfo=timezone.utc)
        return raw_value.astimezone(timezone.utc)
    text_value = str(raw_value).strip()
    if not text_value:
        return None
    normalized = text_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _list_files_in_folder(
    *,
    settings,
    workspace_uuid: str,
    folder_path: str,
    workspace_api_key: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    base_url = str(settings.arquivos_base_url or "").strip().rstrip("/")
    if not base_url:
        return []

    headers = _build_files_api_headers(
        settings=settings,
        workspace_uuid=workspace_uuid,
        workspace_api_key=workspace_api_key,
    )

    def _fetch_page(offset: int) -> list[dict[str, Any]]:
        from urllib.parse import urlencode

        bounded_limit = min(100, max(1, int(limit)))
        params = urlencode({"prefix": f"{folder_path}/", "limit": bounded_limit, "offset": max(0, int(offset))})
        request = Request(f"{base_url}/files/list?{params}", headers=headers, method="GET")
        with urlopen(request, timeout=max(2, int(settings.sync_ws_timeout_seconds))) as response:
            body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
        if isinstance(payload, dict):
            items = payload.get("items")
            if not isinstance(items, list):
                items = payload.get("data")
            if not isinstance(items, list):
                items = payload.get("files")
            return items if isinstance(items, list) else []
        if isinstance(payload, list):
            return payload
        return []

    try:
        first_page = await asyncio.to_thread(_fetch_page, 0)
    except HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = ""
        logger.exception(
            "fileapp.entrada_rescue.list_files_failed status=%s folder=%s body=%s",
            int(exc.code),
            folder_path,
            error_body[:500],
            extra={
                "event": "orch.fileapp.entrada_rescue.list_files_failed",
                "workspace_uuid": workspace_uuid,
                "folder_path": folder_path,
                "status_code": int(exc.code),
                "response_body": error_body[:500],
            },
        )
        return []
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        logger.exception(
            "fileapp.entrada_rescue.list_files_failed folder=%s",
            folder_path,
            extra={
                "event": "orch.fileapp.entrada_rescue.list_files_failed",
                "workspace_uuid": workspace_uuid,
                "folder_path": folder_path,
            },
        )
        return []
    return first_page


async def _fetch_file_metadata_by_id(
    *,
    settings,
    workspace_uuid: str,
    workspace_api_key: str | None,
    file_id: str,
) -> dict[str, Any] | None:
    base_url = str(settings.arquivos_base_url or "").strip().rstrip("/")
    if not base_url:
        return None
    headers = _build_files_api_headers(
        settings=settings,
        workspace_uuid=workspace_uuid,
        workspace_api_key=workspace_api_key,
    )
    request = Request(f"{base_url}/files/metadata/{file_id}", headers=headers, method="GET")
    try:
        with urlopen(request, timeout=max(2, int(settings.sync_ws_timeout_seconds))) as response:
            body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
    except Exception:
        return None
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    return None


async def _fetch_fileapp_rescue_flow_targets(
    db_session,
    *,
    workspace_schema: str,
) -> list[dict[str, Any]]:
    safe_schema = workspace_schema.replace('"', '""')
    result = await db_session.execute(
        text(
            f"""
            SELECT
                f.id::text AS flow_uuid,
                COALESCE(
                    cr.definition -> 'canvas_properties' -> 'orchestration_trigger',
                    dr.definition -> 'canvas_properties' -> 'orchestration_trigger',
                    '{{}}'::jsonb
                ) AS orchestration_trigger
            FROM "{safe_schema}".flow_v2 f
            LEFT JOIN "{safe_schema}".flow_v2_revision cr ON cr.id = f.current_revision_id
            LEFT JOIN "{safe_schema}".flow_v2_revision dr ON dr.id = f.draft_revision_id
            WHERE f.deleted_at IS NULL
              AND COALESCE(f.is_active, FALSE) = TRUE
              AND COALESCE(f.status, '') = 'active'
            ORDER BY f.updated_at DESC
            """
        )
    )
    rows: list[dict[str, Any]] = []
    for row in result.mappings().all():
        flow_uuid = str(row.get("flow_uuid") or "").strip()
        if not flow_uuid:
            continue
        trigger = row.get("orchestration_trigger")
        if isinstance(trigger, str):
            try:
                trigger = json.loads(trigger)
            except json.JSONDecodeError:
                trigger = {}
        folders = sorted(extract_monitored_folders_from_orchestration_trigger(trigger if isinstance(trigger, dict) else {}))
        if not folders:
            continue
        rows.append({"flow_uuid": flow_uuid, "monitored_folders": folders})
    return rows


async def _has_fileapp_ingest_evidence(
    db_session,
    *,
    workspace_schema: str,
    file_id: str,
    original_name: str,
    monitored_folder: str,
) -> bool:
    safe_schema = workspace_schema.replace('"', '""')
    normalized_folder = _normalize_folder_path(monitored_folder).lower()
    if not normalized_folder:
        return False
    event_check = await db_session.execute(
        text(
            f"""
            SELECT 1
            FROM "{safe_schema}".arquivos_s3_events
            WHERE (
                    file_id = CAST(:file_id AS uuid)
                    OR file_name = :original_name
                  )
              AND lower(trim(BOTH '/' FROM COALESCE(folder_path, ''))) = :folder_path
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"file_id": file_id, "original_name": original_name, "folder_path": normalized_folder},
    )
    if event_check.first() is not None:
        return True

    source_list_check = await db_session.execute(
        text(
            f"""
            SELECT 1
            FROM "{safe_schema}".source_lists
            WHERE file_url ILIKE :file_id_suffix
               OR (
                   file_name = :original_name
                   AND lower(trim(BOTH '/' FROM COALESCE(file_path, ''))) = :folder_path
               )
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"original_name": original_name, "file_id_suffix": f"%{file_id}", "folder_path": normalized_folder},
    )
    return source_list_check.first() is not None


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
                public_id::text AS mailing_uuid,
                file_name,
                file_path,
                file_url
            FROM "{safe_schema}".source_lists
            WHERE UPPER(COALESCE(status, '')) = 'PROCESSED'
              AND COALESCE(file_name, '') <> ''
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
                "mailing_uuid": str(row["mailing_uuid"] or "").strip(),
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
    _persist_process_tipo1_rescue_flow_state(
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
        payload=payload,
        state="in_flight",
        ttl_seconds=86400,
    )
    try:
        result = asyncio.run(
            _process_fileapp_tipo1_event_task(
                workspace_uuid=workspace_uuid,
                flow_uuid=flow_uuid,
                payload=payload,
                mapping_template_uuid=mapping_template_uuid,
            )
        )
    except Exception:
        _persist_process_tipo1_rescue_flow_state(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            payload=payload,
            state="failed",
            ttl_seconds=86400,
        )
        raise
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
        terminal_state = "done" if str(result.get("status") or "").strip().lower() == "done" else "failed"
        _persist_process_tipo1_rescue_flow_state(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            payload=payload,
            state=terminal_state,
            ttl_seconds=86400,
        )
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
        failed_result = {
            **result,
            "status": "failed_final",
            "retry_exhausted": True,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "quarantine_result": quarantine_result,
            "quarantine_error": quarantine_error,
        }
        _persist_process_tipo1_rescue_flow_state(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            payload=payload,
            state="failed",
            ttl_seconds=86400,
        )
        return failed_result

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


@celery_app.task(name="app.tasks.fileapp.reconcile_entrada_rescue", ignore_result=True)
def reconcile_fileapp_entrada_rescue_task() -> dict[str, int]:
    return asyncio.run(_reconcile_fileapp_entrada_rescue_task())


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
    mailing_uuid = str(pipeline_result.get("mailing_uuid") or "").strip()
    association_status: dict[str, Any]
    try:
        association_task = associate_fileapp_mailing_task.apply_async(
            kwargs={
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": flow_uuid,
                "mailing_uuid": mailing_uuid,
                "linked_by": str(payload_file.get("id") or "").strip() or None,
            },
            queue=settings.celery_fileapp_mailing_assoc_queue,
            routing_key=settings.celery_fileapp_mailing_assoc_queue,
            countdown=max(0, int(settings.celery_fileapp_mailing_assoc_delay_seconds)),
        )
        association_status = {
            "status": "queued",
            "task_id": association_task.id,
            "queue": settings.celery_fileapp_mailing_assoc_queue,
            "countdown_seconds": int(settings.celery_fileapp_mailing_assoc_delay_seconds),
        }
        logger.info(
            "fileapp.tipo1.mailing_association.enqueued",
            extra={
                "event": "orch.fileapp.tipo1.mailing_association.enqueued",
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": flow_uuid,
                "mailing_uuid": mailing_uuid,
                "queue": settings.celery_fileapp_mailing_assoc_queue,
                "countdown_seconds": int(settings.celery_fileapp_mailing_assoc_delay_seconds),
                "task_id": association_task.id,
            },
        )
    except Exception as exc:
        association_status = {
            "status": "warning",
            "error_code": "enqueue_failed",
            "error_message": str(exc),
            "error_details": {"exception_type": type(exc).__name__},
        }
        logger.exception(
            "fileapp.tipo1.mailing_association.enqueue_failed",
            extra={
                "event": "orch.fileapp.tipo1.mailing_association.enqueue_failed",
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": flow_uuid,
                "mailing_uuid": mailing_uuid,
                "queue": settings.celery_fileapp_mailing_assoc_queue,
            },
        )
        async with session_factory() as alarm_session:
            bind_workspace_context(safe_workspace_uuid)
            await persist_alarm(
                alarm_session,
                level="warning",
                code="fileapp_tipo1_mailing_association_enqueue_failed",
                message="Falha ao enfileirar associacao de mailing no fluxo FileApp tipo1.",
                details={
                    "flow_uuid": flow_uuid,
                    "workspace_uuid": safe_workspace_uuid,
                    "mailing_uuid": mailing_uuid,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
                flow_uuid=flow_uuid,
                app_name="ArquivosApp",
                entity=str(payload_file.get("id") or ""),
                entity_type="file",
                entity_address=str(payload_file.get("folder_path") or ""),
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
        "mailing_association": association_status,
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
    associations_done = 0
    associations_blocked = 0
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
            workspace_api_key = await fetch_workspace_otima_billing_api_key(
                db_session,
                workspace_uuid=safe_workspace_uuid,
            )
            folder_to_flows: dict[str, list[str]] = {}
            try:
                flow_targets = await _fetch_fileapp_rescue_flow_targets(
                    db_session,
                    workspace_schema=workspace_schema,
                )
                for flow in flow_targets:
                    flow_uuid = str(flow.get("flow_uuid") or "").strip()
                    if not flow_uuid:
                        continue
                    for folder in flow.get("monitored_folders") or []:
                        monitored_folder = str(folder or "").strip().strip("/")
                        if not monitored_folder:
                            continue
                        if monitored_folder.lower().endswith("/processados") or monitored_folder.lower().endswith("/falha"):
                            continue
                        folder_to_flows.setdefault(monitored_folder, [])
                        if flow_uuid not in folder_to_flows[monitored_folder]:
                            folder_to_flows[monitored_folder].append(flow_uuid)
            except Exception:
                warnings += 1
                logger.exception(
                    "fileapp.post_process_reconcile.fetch_flow_targets_failed",
                    extra={
                        "event": "orch.fileapp.post_process_reconcile.fetch_flow_targets_failed",
                        "workspace_uuid": safe_workspace_uuid,
                    },
                )

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

                file_path = _normalize_folder_path(str(item.get("file_path") or ""))
                if not file_path:
                    metadata = await _fetch_file_metadata_by_id(
                        settings=settings,
                        workspace_uuid=safe_workspace_uuid,
                        workspace_api_key=workspace_api_key,
                        file_id=file_id,
                    )
                    if isinstance(metadata, dict):
                        file_path = _normalize_folder_path(str(metadata.get("folder_path") or ""))
                if not file_path:
                    continue
                flow_uuids = folder_to_flows.get(file_path, [])
                if not flow_uuids:
                    continue
                mailing_uuid = str(item.get("mailing_uuid") or "").strip()
                if flow_uuids and mailing_uuid:
                    association_blocked = False
                    for flow_uuid in flow_uuids:
                        try:
                            detach_all_files = await resolve_detach_all_files(
                                db_session,
                                workspace_schema=workspace_schema,
                                flow_uuid=flow_uuid,
                            )
                            association_result = await associate_mailing_to_flow_from_file_event(
                                settings=settings,
                                workspace_uuid=safe_workspace_uuid,
                                flow_uuid=flow_uuid,
                                mailing_uuid=mailing_uuid,
                                linked_by=file_id,
                                workspace_api_key=workspace_api_key,
                                detach_all_files=detach_all_files,
                            )
                            association_status = str(association_result.get("status") or "").strip().lower()
                            if association_status in {"error", "pending"}:
                                association_blocked = True
                                warnings += 1
                                await persist_alarm(
                                    db_session,
                                    level="warning",
                                    code="fileapp_post_process_reconcile_association_blocked",
                                    message="Import concluído, mas vínculo de mailing ao flow ainda não finalizado.",
                                    details={
                                        "workspace_uuid": safe_workspace_uuid,
                                        "flow_uuid": flow_uuid,
                                        "source_list_id": source_list_id,
                                        "file_id": file_id,
                                        "mailing_uuid": mailing_uuid,
                                        "association_result": association_result,
                                    },
                                    flow_uuid=flow_uuid,
                                    app_name="ArquivosApp",
                                    entity=file_id,
                                    entity_type="file",
                                    entity_address=file_path,
                                )
                                break
                            associations_done += 1
                        except Exception as exc:
                            association_blocked = True
                            warnings += 1
                            await persist_alarm(
                                db_session,
                                level="warning",
                                code="fileapp_post_process_reconcile_association_unexpected_error",
                                message="Erro inesperado ao vincular mailing ao flow no reconciliador de pós-processamento.",
                                details={
                                    "workspace_uuid": safe_workspace_uuid,
                                    "flow_uuid": flow_uuid,
                                    "source_list_id": source_list_id,
                                    "file_id": file_id,
                                    "mailing_uuid": mailing_uuid,
                                    "exception_type": type(exc).__name__,
                                    "exception_message": str(exc),
                                },
                                flow_uuid=flow_uuid,
                                app_name="ArquivosApp",
                                entity=file_id,
                                entity_type="file",
                                entity_address=file_path,
                            )
                            break
                    if association_blocked:
                        associations_blocked += 1
                        continue

                candidates_scanned += 1
                payload = {
                    "file": {
                        "id": file_id,
                        "folder_path": file_path,
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
                        entity_address=file_path,
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
                        entity_address=file_path,
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
            "associations_done": associations_done,
            "associations_blocked": associations_blocked,
            "quarantined": quarantined,
            "exhausted_quarantined": exhausted_quarantined,
            "warnings": warnings,
        },
    )
    return {
        "workspaces_scanned": workspaces_scanned,
        "candidates_scanned": candidates_scanned,
        "moved": moved,
        "associations_done": associations_done,
        "associations_blocked": associations_blocked,
        "quarantined": quarantined,
        "exhausted_quarantined": exhausted_quarantined,
        "warnings": warnings,
    }


async def _reconcile_fileapp_entrada_rescue_task() -> dict[str, int]:
    settings = get_settings()
    if not settings.celery_enabled or not settings.celery_fileapp_ingest_enabled:
        return {
            "workspaces_scanned": 0,
            "folders_scanned": 0,
            "files_scanned": 0,
            "reingested": 0,
            "quarantined": 0,
            "skipped_recent": 0,
            "skipped_evidence": 0,
            "warnings": 0,
        }

    redis_client: redis.Redis | None = None
    if settings.celery_result_backend:
        try:
            redis_client = redis.Redis.from_url(settings.celery_result_backend)
        except Exception:
            logger.exception(
                "fileapp.entrada_rescue.redis_setup_failed",
                extra={"event": "orch.fileapp.entrada_rescue.redis_setup_failed"},
            )

    workspace_scope = str(settings.celery_fileapp_entrada_rescue_workspace_uuid or "").strip() or None
    now_utc = datetime.now(timezone.utc)
    grace_seconds = max(0, int(settings.celery_fileapp_entrada_rescue_grace_seconds))
    fail_after_seconds = max(grace_seconds, int(settings.celery_fileapp_entrada_rescue_fail_after_seconds))
    max_retries = max(1, int(settings.celery_fileapp_entrada_rescue_max_retries))
    lock_seconds = max(5, int(settings.celery_fileapp_entrada_rescue_lock_seconds))
    state_ttl_seconds = max(3600, fail_after_seconds * 2)

    session_factory = get_session_factory()
    workspaces_scanned = 0
    folders_scanned = 0
    files_scanned = 0
    reingested = 0
    quarantined = 0
    skipped_recent = 0
    skipped_evidence = 0
    warnings = 0

    async with session_factory() as db_session:
        workspaces = await list_completed_workspaces(db_session)
        for workspace in workspaces:
            workspace_uuid = str(workspace.get("workspace_uuid") or "").strip()
            if not workspace_uuid:
                continue
            if workspace_scope and workspace_uuid != workspace_scope:
                continue

            workspaces_scanned += 1
            safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)
            workspace_api_key = await fetch_workspace_otima_billing_api_key(
                db_session,
                workspace_uuid=safe_workspace_uuid,
            )
            try:
                flow_targets = await _fetch_fileapp_rescue_flow_targets(
                    db_session,
                    workspace_schema=workspace_schema,
                )
            except Exception:
                warnings += 1
                logger.exception(
                    "fileapp.entrada_rescue.fetch_flow_targets_failed",
                    extra={
                        "event": "orch.fileapp.entrada_rescue.fetch_flow_targets_failed",
                        "workspace_uuid": safe_workspace_uuid,
                    },
                )
                continue

            folder_to_flows: dict[str, list[str]] = {}
            for flow in flow_targets:
                flow_uuid = str(flow.get("flow_uuid") or "").strip()
                for folder in flow.get("monitored_folders") or []:
                    monitored_folder = str(folder or "").strip().strip("/")
                    if not monitored_folder:
                        continue
                    if monitored_folder.lower().endswith("/processados") or monitored_folder.lower().endswith("/falha"):
                        continue
                    folder_to_flows.setdefault(monitored_folder, [])
                    if flow_uuid not in folder_to_flows[monitored_folder]:
                        folder_to_flows[monitored_folder].append(flow_uuid)

            for folder_path, flow_uuids in folder_to_flows.items():
                folders_scanned += 1
                listed_files = await _list_files_in_folder(
                    settings=settings,
                    workspace_uuid=safe_workspace_uuid,
                    folder_path=folder_path,
                    workspace_api_key=workspace_api_key,
                    limit=settings.celery_fileapp_entrada_rescue_batch_size,
                )
                for file_item in listed_files:
                    file_id = str(file_item.get("id") or file_item.get("uuid") or "").strip()
                    original_name = str(
                        file_item.get("original_name") or file_item.get("name") or file_item.get("file_name") or ""
                    ).strip()
                    listed_folder = str(file_item.get("folder_path") or file_item.get("path") or folder_path).strip().strip("/")
                    file_url = str(file_item.get("url") or "").strip()
                    if not file_id or not original_name:
                        continue
                    if listed_folder != folder_path:
                        continue

                    files_scanned += 1
                    created_at = _parse_file_datetime(file_item.get("created_at")) or _parse_file_datetime(file_item.get("updated_at"))
                    if created_at is None:
                        continue
                    age_seconds = (now_utc - created_at).total_seconds()
                    if age_seconds < grace_seconds:
                        skipped_recent += 1
                        continue

                    if await _has_fileapp_ingest_evidence(
                        db_session,
                        workspace_schema=workspace_schema,
                        file_id=file_id,
                        original_name=original_name,
                        monitored_folder=folder_path,
                    ):
                        skipped_evidence += 1
                        for flow_uuid in flow_uuids:
                            _set_fileapp_entrada_rescue_flow_state(
                                redis_client,
                                workspace_uuid=safe_workspace_uuid,
                                flow_uuid=flow_uuid,
                                file_id=file_id,
                                state="done",
                                ttl_seconds=state_ttl_seconds,
                            )
                        _clear_fileapp_entrada_rescue_attempts(
                            redis_client,
                            workspace_uuid=safe_workspace_uuid,
                            file_id=file_id,
                        )
                        continue

                    if not _try_acquire_fileapp_entrada_rescue_file_lock(
                        redis_client,
                        workspace_uuid=safe_workspace_uuid,
                        file_id=file_id,
                        lock_seconds=lock_seconds,
                    ):
                        continue

                    attempts = _get_fileapp_entrada_rescue_attempts(
                        redis_client,
                        workspace_uuid=safe_workspace_uuid,
                        file_id=file_id,
                    )
                    payload = {
                        "file": {
                            "id": file_id,
                            "folder_path": folder_path,
                            "original_name": original_name,
                            "url": file_url,
                        }
                    }

                    if age_seconds >= fail_after_seconds and attempts >= max_retries:
                        try:
                            quarantine_result = await quarantine_file_to_falha(
                                settings=settings,
                                workspace_uuid=safe_workspace_uuid,
                                payload=payload,
                            )
                            if str(quarantine_result.get("status") or "").strip().lower() in {"done", "skipped"}:
                                quarantined += 1
                                _clear_fileapp_entrada_rescue_attempts(
                                    redis_client,
                                    workspace_uuid=safe_workspace_uuid,
                                    file_id=file_id,
                                )
                            continue
                        except Exception as exc:
                            warnings += 1
                            await persist_alarm(
                                db_session,
                                level="warning",
                                code="fileapp_entrada_rescue_quarantine_failed",
                                message="Falha ao mover arquivo órfão da entrada para pasta falha.",
                                details={
                                    "workspace_uuid": safe_workspace_uuid,
                                    "file_id": file_id,
                                    "file_name": original_name,
                                    "folder_path": folder_path,
                                    "attempts": attempts,
                                    "exception_type": type(exc).__name__,
                                    "exception_message": str(exc),
                                },
                                app_name="ArquivosApp",
                                entity=file_id,
                                entity_type="file",
                                entity_address=folder_path,
                            )
                            continue

                    enqueued = False
                    should_increment_attempt = False
                    for flow_uuid in flow_uuids:
                        flow_state = _get_fileapp_entrada_rescue_flow_state(
                            redis_client,
                            workspace_uuid=safe_workspace_uuid,
                            flow_uuid=flow_uuid,
                            file_id=file_id,
                        )
                        if flow_state in {"in_flight", "done"}:
                            continue

                        mapping_template_uuid = await resolve_mapping_template_uuid(
                            db_session,
                            workspace_schema=workspace_schema,
                            flow_uuid=flow_uuid,
                            payload=payload,
                        )
                        if not mapping_template_uuid:
                            continue
                        if not _try_mark_fileapp_entrada_rescue_flow_in_flight(
                            redis_client,
                            workspace_uuid=safe_workspace_uuid,
                            flow_uuid=flow_uuid,
                            file_id=file_id,
                            ttl_seconds=state_ttl_seconds,
                        ):
                            continue
                        should_increment_attempt = True
                        try:
                            ingest_fileapp_tipo1_event_task.apply_async(
                                kwargs={
                                    "workspace_uuid": safe_workspace_uuid,
                                    "flow_uuid": flow_uuid,
                                    "payload": payload,
                                    "mapping_template_uuid": mapping_template_uuid,
                                },
                                queue=settings.celery_s3_files_ingest_queue,
                                routing_key=settings.celery_s3_files_ingest_queue,
                            )
                            enqueued = True
                            reingested += 1
                            _set_fileapp_entrada_rescue_attempts(
                                redis_client,
                                workspace_uuid=safe_workspace_uuid,
                                file_id=file_id,
                                attempts=attempts + 1,
                                ttl_seconds=state_ttl_seconds,
                            )
                            break
                        except Exception as exc:
                            _set_fileapp_entrada_rescue_flow_state(
                                redis_client,
                                workspace_uuid=safe_workspace_uuid,
                                flow_uuid=flow_uuid,
                                file_id=file_id,
                                state="failed",
                                ttl_seconds=state_ttl_seconds,
                            )
                            warnings += 1
                            await persist_alarm(
                                db_session,
                                level="warning",
                                code="fileapp_entrada_rescue_enqueue_failed",
                                message="Falha ao reingestar arquivo órfão da entrada.",
                                details={
                                    "workspace_uuid": safe_workspace_uuid,
                                    "flow_uuid": flow_uuid,
                                    "file_id": file_id,
                                    "file_name": original_name,
                                    "folder_path": folder_path,
                                    "attempts": attempts,
                                    "exception_type": type(exc).__name__,
                                    "exception_message": str(exc),
                                },
                                flow_uuid=flow_uuid,
                                app_name="ArquivosApp",
                                entity=file_id,
                                entity_type="file",
                                entity_address=folder_path,
                            )
                    if not enqueued and should_increment_attempt:
                        _set_fileapp_entrada_rescue_attempts(
                            redis_client,
                            workspace_uuid=safe_workspace_uuid,
                            file_id=file_id,
                            attempts=attempts + 1,
                            ttl_seconds=state_ttl_seconds,
                        )

    logger.info(
        "fileapp.entrada_rescue.finished",
        extra={
            "event": "orch.fileapp.entrada_rescue.finished",
            "workspace_scope": workspace_scope,
            "workspaces_scanned": workspaces_scanned,
            "folders_scanned": folders_scanned,
            "files_scanned": files_scanned,
            "reingested": reingested,
            "quarantined": quarantined,
            "skipped_recent": skipped_recent,
            "skipped_evidence": skipped_evidence,
            "warnings": warnings,
        },
    )
    return {
        "workspaces_scanned": workspaces_scanned,
        "folders_scanned": folders_scanned,
        "files_scanned": files_scanned,
        "reingested": reingested,
        "quarantined": quarantined,
        "skipped_recent": skipped_recent,
        "skipped_evidence": skipped_evidence,
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
