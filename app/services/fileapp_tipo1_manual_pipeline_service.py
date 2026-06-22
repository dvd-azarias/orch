from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib import request
from urllib.error import HTTPError, URLError

from app.core.config import Settings


@dataclass(frozen=True)
class FileUploadPayload:
    file_name: str
    content_type: str
    file_bytes: bytes
    description: str
    legal_basis: str


@dataclass(frozen=True)
class FileEventMailingIdentity:
    name: str
    description: str


class FileAppTipo1ManualPipelineError(Exception):
    def __init__(self, *, step: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.step = step
        self.message = message
        self.details = details or {}


_FILE_DOWNLOAD_RETRY_ATTEMPTS = 5
_FILE_DOWNLOAD_RETRY_INTERVAL_SECONDS = 15.0


def _build_target_core_headers(
    *,
    workspace_uuid: str,
    bearer_token: str | None,
    workspace_api_key: str | None,
    content_type: str | None = "application/json",
) -> dict[str, str]:
    headers = {
        "accept": "application/json",
        "x-application": "target",
        "x-workspace-uuid": workspace_uuid,
    }
    if content_type is not None:
        headers["content-type"] = content_type
    if bearer_token:
        headers["authorization"] = f"Bearer {bearer_token}"
    if workspace_api_key:
        headers["x-api-key"] = workspace_api_key
        headers["x-workspace-api-key"] = workspace_api_key
    return headers


def _decode_json(raw_body: str) -> dict[str, Any]:
    if not raw_body:
        return {}
    parsed = json.loads(raw_body)
    if isinstance(parsed, dict):
        return parsed
    return {}


def _json_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout_seconds: float,
) -> tuple[int, str]:
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url=url, data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8", errors="replace")
        return int(response.status), body


def _multipart_encode(upload: FileUploadPayload) -> tuple[bytes, str]:
    boundary = f"orch-fileapp-{uuid.uuid4().hex}"
    lines: list[bytes] = []

    def _add_text_field(name: str, value: str) -> None:
        lines.append(f"--{boundary}\r\n".encode("utf-8"))
        lines.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        lines.append(value.encode("utf-8"))
        lines.append(b"\r\n")

    lines.append(f"--{boundary}\r\n".encode("utf-8"))
    lines.append(
        (
            'Content-Disposition: form-data; name="file"; '
            f'filename="{upload.file_name}"\r\n'
            f"Content-Type: {upload.content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    lines.append(upload.file_bytes)
    lines.append(b"\r\n")
    _add_text_field("description", upload.description)
    _add_text_field("legal_basis", upload.legal_basis)
    lines.append(f"--{boundary}--\r\n".encode("utf-8"))

    return b"".join(lines), f"multipart/form-data; boundary={boundary}"


def _slugify_file_name(file_name: str) -> str:
    stem = str(file_name or "").strip()
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    normalized = unicodedata.normalize("NFKD", stem)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_only).strip("_").lower()
    return slug or "arquivo"


def _next_incremental_slug(base_slug: str, existing_names: list[str] | tuple[str, ...]) -> str:
    used_indexes: set[int] = set()
    prefix = f"{base_slug}_"
    for raw_name in existing_names:
        name = str(raw_name or "").strip().lower()
        if not name:
            continue
        if name == base_slug:
            used_indexes.add(0)
            continue
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix) :]
        if len(suffix) != 3 or not suffix.isdigit():
            continue
        used_indexes.add(int(suffix))
    if 0 not in used_indexes:
        return base_slug
    next_index = 1
    while next_index in used_indexes:
        next_index += 1
    return f"{base_slug}_{next_index:03d}"


def build_file_event_mailing_identity(
    *,
    file_name: str,
    existing_names: list[str] | tuple[str, ...] | None = None,
) -> FileEventMailingIdentity:
    base_slug = _slugify_file_name(file_name)
    final_slug = _next_incremental_slug(base_slug, existing_names or [])
    return FileEventMailingIdentity(
        name=final_slug,
        description=f"Carga via evento de cópia de arquivo no SFTP - {final_slug}",
    )


def _multipart_request(
    *,
    url: str,
    headers: dict[str, str],
    upload: FileUploadPayload,
    timeout_seconds: float,
) -> tuple[int, str]:
    data, content_type = _multipart_encode(upload)
    multipart_headers = dict(headers)
    multipart_headers["content-type"] = content_type
    req = request.Request(url=url, data=data, headers=multipart_headers, method="POST")
    with request.urlopen(req, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8", errors="replace")
        return int(response.status), body


def _download_file_bytes(*, url: str, headers: dict[str, str], timeout_seconds: float) -> bytes:
    req = request.Request(url=url, headers=headers, method="GET")
    with request.urlopen(req, timeout=timeout_seconds) as response:
        return response.read()


async def _download_file_bytes_with_retry(
    *,
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
) -> bytes:
    attempts = max(1, int(_FILE_DOWNLOAD_RETRY_ATTEMPTS))
    interval_seconds = max(0.0, float(_FILE_DOWNLOAD_RETRY_INTERVAL_SECONDS))
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(
                _download_file_bytes,
                url=url,
                headers=headers,
                timeout_seconds=timeout_seconds,
            )
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= attempts:
                break
            await asyncio.sleep(interval_seconds)

    details: dict[str, Any] = {
        "attempts": attempts,
        "retry_interval_seconds": interval_seconds,
    }
    if isinstance(last_error, HTTPError):
        details["status_code"] = int(last_error.code)
    elif isinstance(last_error, URLError):
        details["reason"] = str(last_error.reason)
    if last_error is not None:
        details["error_type"] = type(last_error).__name__

    raise FileAppTipo1ManualPipelineError(
        step="step1_upload",
        message=(
            f"Falha ao baixar arquivo após {attempts} tentativas "
            f"(intervalo {int(interval_seconds)}s)."
        ),
        details=details,
    )


async def download_file_bytes_for_file_event(
    *,
    settings: Settings,
    payload: dict[str, Any],
    default_workspace_uuid: str,
) -> bytes:
    file_data = payload.get("file")
    if not isinstance(file_data, dict):
        raise FileAppTipo1ManualPipelineError(
            step="step1_upload",
            message="Payload de FileApp inválido: campo file ausente.",
        )

    file_url = str(file_data.get("url") or "").strip()
    if not file_url:
        raise FileAppTipo1ManualPipelineError(
            step="step1_upload",
            message="file.url ausente no evento FileApp.",
        )
    if not settings.sync_ws_client_id or not settings.sync_ws_client_secret:
        raise FileAppTipo1ManualPipelineError(
            step="step1_upload",
            message="Credenciais SYNC_WS_* não configuradas para baixar arquivo.",
        )

    event_workspace_uuid = str(file_data.get("workspace_uuid") or "").strip() or default_workspace_uuid
    download_headers = {
        "x-workspace-uuid": event_workspace_uuid,
        "x-client-id": settings.sync_ws_client_id,
        "x-client-secret": settings.sync_ws_client_secret,
    }
    return await _download_file_bytes_with_retry(
        url=file_url,
        headers=download_headers,
        timeout_seconds=settings.sync_ws_timeout_seconds,
    )


def _normalize_uuid(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except (ValueError, TypeError, AttributeError):
        return None


def _extract_uuid_from_url(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        path = urlparse(raw).path or ""
    except Exception:
        return None
    if not path:
        return None
    candidates = [segment.strip() for segment in path.split("/") if segment.strip()]
    for candidate in reversed(candidates):
        normalized = _normalize_uuid(candidate)
        if normalized:
            return normalized
    return None


def _resolve_linked_by_uuid(*, payload: dict[str, Any], file_data: dict[str, Any]) -> str | None:
    candidates = [
        file_data.get("id"),
        file_data.get("uuid"),
        payload.get("file_id"),
        payload.get("file_uuid"),
    ]
    for candidate in candidates:
        normalized = _normalize_uuid(None if candidate is None else str(candidate))
        if normalized:
            return normalized
    return _extract_uuid_from_url(str(file_data.get("url") or ""))


async def run_tipo1_manual_pipeline(
    *,
    settings: Settings,
    workspace_uuid: str,
    flow_uuid: str,
    payload: dict[str, Any],
    mapping_template_uuid: str,
    workspace_api_key: str | None = None,
    mailing_name: str | None = None,
    mailing_description: str | None = None,
    defer_step7_link_flow: bool = False,
    predownloaded_file_bytes: bytes | None = None,
    upload_file_name_override: str | None = None,
) -> dict[str, Any]:
    file_data = payload.get("file")
    if not isinstance(file_data, dict):
        raise FileAppTipo1ManualPipelineError(
            step="input",
            message="Payload de FileApp inválido: campo file ausente.",
        )

    file_name = str(file_data.get("original_name") or "").strip()
    upload_file_name = str(upload_file_name_override or file_name).strip()
    linked_by = _resolve_linked_by_uuid(payload=payload, file_data=file_data)
    legal_basis = str(file_data.get("legal_basis") or payload.get("legal_basis") or "consent")
    mime_type = str(file_data.get("mime_type") or "").strip()

    if not upload_file_name:
        raise FileAppTipo1ManualPipelineError(
            step="step1_upload",
            message="file.original_name ausente no evento FileApp.",
        )
    identity = build_file_event_mailing_identity(file_name=upload_file_name)
    resolved_mailing_name = str(mailing_name or identity.name).strip() or identity.name
    resolved_description = str(mailing_description or identity.description).strip() or identity.description
    if not linked_by:
        raise FileAppTipo1ManualPipelineError(
            step="step7_link_flow",
            message="Não foi possível resolver UUID válido para linked_by a partir de file.id/file.url.",
        )

    base_url = str(settings.sync_webhook_base_url or "").strip().rstrip("/")
    if not base_url:
        raise FileAppTipo1ManualPipelineError(
            step="target_core_config",
            message="SYNC_WEBHOOK_BASE_URL não configurada.",
        )

    bearer_token = str(settings.target_core_api_bearer_token or "").strip() or None
    fallback_workspace_api_key = str(workspace_api_key or "").strip() or None
    if not bearer_token and not fallback_workspace_api_key:
        raise FileAppTipo1ManualPipelineError(
            step="target_core_config",
            message="Token Bearer e API key de workspace indisponíveis.",
        )

    file_bytes = (
        bytes(predownloaded_file_bytes)
        if predownloaded_file_bytes is not None
        else await download_file_bytes_for_file_event(
            settings=settings,
            payload=payload,
            default_workspace_uuid=workspace_uuid,
        )
    )

    upload_content_type = mime_type or mimetypes.guess_type(upload_file_name)[0] or "application/octet-stream"
    step_results: list[dict[str, Any]] = []

    # Step 1
    try:
        upload_headers = _build_target_core_headers(
            workspace_uuid=workspace_uuid,
            bearer_token=bearer_token,
            workspace_api_key=fallback_workspace_api_key,
            content_type=None,
        )
        upload_payload = FileUploadPayload(
            file_name=upload_file_name,
            content_type=upload_content_type,
            file_bytes=file_bytes,
            description=resolved_description,
            legal_basis=legal_basis,
        )
        status_code, body = await asyncio.to_thread(
            _multipart_request,
            url=f"{base_url}/v2/mailings/upload",
            headers=upload_headers,
            upload=upload_payload,
            timeout_seconds=settings.sync_ws_timeout_seconds,
        )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FileAppTipo1ManualPipelineError(
            step="step1_upload",
            message=f"Upload do mailing falhou (HTTP {int(exc.code)}).",
            details={"status_code": int(exc.code), "response_body": detail},
        ) from exc
    except URLError as exc:
        raise FileAppTipo1ManualPipelineError(
            step="step1_upload",
            message=f"Upload do mailing falhou: {exc.reason}",
        ) from exc

    upload_response = _decode_json(body)
    upload_data = upload_response.get("data") if isinstance(upload_response.get("data"), dict) else {}
    mailing_uuid = str(upload_data.get("mailing_id") or "").strip()
    if status_code >= 400 or not mailing_uuid:
        raise FileAppTipo1ManualPipelineError(
            step="step1_upload",
            message="Upload do mailing não retornou mailing_id válido.",
            details={"status_code": status_code, "response_body": body},
        )
    step_results.append({"step": "step1_upload", "status_code": status_code, "mailing_id": mailing_uuid})

    json_headers = _build_target_core_headers(
        workspace_uuid=workspace_uuid,
        bearer_token=bearer_token,
        workspace_api_key=fallback_workspace_api_key,
    )

    # Step 2
    try:
        status_code, body = await asyncio.to_thread(
            _json_request,
            method="GET",
            url=f"{base_url}/v2/mailings/mapping-templates",
            headers=json_headers,
            payload=None,
            timeout_seconds=settings.sync_ws_timeout_seconds,
        )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FileAppTipo1ManualPipelineError(
            step="step2_templates",
            message=f"Listagem de templates falhou (HTTP {int(exc.code)}).",
            details={"status_code": int(exc.code), "response_body": detail},
        ) from exc
    templates_response = _decode_json(body)
    templates = templates_response.get("data")
    template_exists = False
    if isinstance(templates, list):
        template_exists = any(str(item.get("id") or "").strip() == mapping_template_uuid for item in templates if isinstance(item, dict))
    if status_code >= 400 or not template_exists:
        raise FileAppTipo1ManualPipelineError(
            step="step2_templates",
            message="mapping_template_id do flow não encontrado nos templates do workspace.",
            details={"status_code": status_code},
        )
    step_results.append({"step": "step2_templates", "status_code": status_code, "mapping_template_uuid": mapping_template_uuid})

    # Step 4
    patch_payload = {
        "mapping_template_id": mapping_template_uuid,
        "name": resolved_mailing_name,
        "description": resolved_description,
    }
    try:
        status_code, body = await asyncio.to_thread(
            _json_request,
            method="PATCH",
            url=f"{base_url}/v2/mailings/{mailing_uuid}",
            headers=json_headers,
            payload=patch_payload,
            timeout_seconds=settings.sync_ws_timeout_seconds,
        )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FileAppTipo1ManualPipelineError(
            step="step4_patch_mailing",
            message=f"PATCH do mailing falhou (HTTP {int(exc.code)}).",
            details={"status_code": int(exc.code), "response_body": detail},
        ) from exc
    if status_code >= 400:
        raise FileAppTipo1ManualPipelineError(
            step="step4_patch_mailing",
            message="PATCH do mailing retornou status inválido.",
            details={"status_code": status_code, "response_body": body},
        )
    step_results.append({"step": "step4_patch_mailing", "status_code": status_code})

    # Step 3 (recarrega após aplicar template no PATCH)
    try:
        status_code, body = await asyncio.to_thread(
            _json_request,
            method="GET",
            url=f"{base_url}/v2/mailings/{mailing_uuid}/field-mappings",
            headers=json_headers,
            payload=None,
            timeout_seconds=settings.sync_ws_timeout_seconds,
        )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FileAppTipo1ManualPipelineError(
            step="step3_field_mappings_get",
            message=f"Consulta de field-mappings falhou (HTTP {int(exc.code)}).",
            details={"status_code": int(exc.code), "response_body": detail},
        ) from exc
    field_mappings_response = _decode_json(body)
    field_mappings_data = (
        field_mappings_response.get("data") if isinstance(field_mappings_response.get("data"), dict) else {}
    )
    put_suggestion = (
        field_mappings_data.get("put_suggestion")
        if isinstance(field_mappings_data.get("put_suggestion"), dict)
        else {}
    )
    suggested_mappings = put_suggestion.get("mappings")
    if status_code >= 400 or not isinstance(suggested_mappings, list) or not suggested_mappings:
        raise FileAppTipo1ManualPipelineError(
            step="step3_field_mappings_get",
            message="Consulta de field-mappings não retornou put_suggestion.mappings válido.",
            details={"status_code": status_code},
        )
    step_results.append(
        {
            "step": "step3_field_mappings_get",
            "status_code": status_code,
            "mappings_count": len(suggested_mappings),
        }
    )

    # Step 5
    put_payload = {"mappings": suggested_mappings}
    try:
        status_code, body = await asyncio.to_thread(
            _json_request,
            method="PUT",
            url=f"{base_url}/v2/mailings/{mailing_uuid}/field-mappings",
            headers=json_headers,
            payload=put_payload,
            timeout_seconds=settings.sync_ws_timeout_seconds,
        )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FileAppTipo1ManualPipelineError(
            step="step5_put_field_mappings",
            message=f"PUT de field-mappings falhou (HTTP {int(exc.code)}).",
            details={"status_code": int(exc.code), "response_body": detail},
        ) from exc
    put_response = _decode_json(body)
    put_data = put_response.get("data") if isinstance(put_response.get("data"), dict) else {}
    put_status = str(put_data.get("status") or "").strip().upper()
    if status_code >= 400 or put_status != "READY_TO_INGEST":
        raise FileAppTipo1ManualPipelineError(
            step="step5_put_field_mappings",
            message="Mailing não ficou em READY_TO_INGEST após PUT de field-mappings.",
            details={"status_code": status_code, "mailing_status": put_status},
        )
    step_results.append({"step": "step5_put_field_mappings", "status_code": status_code, "mailing_status": put_status})

    # Step 6
    import_payload = {
        "mailing_id": mailing_uuid,
        "mapping_template_id": mapping_template_uuid,
        "use_mapping_as_template": False,
        "mapping_template_name": None,
    }
    try:
        status_code, body = await asyncio.to_thread(
            _json_request,
            method="POST",
            url=f"{base_url}/v2/mailings/{mailing_uuid}/import",
            headers=json_headers,
            payload=import_payload,
            timeout_seconds=settings.sync_ws_timeout_seconds,
        )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FileAppTipo1ManualPipelineError(
            step="step6_import",
            message=f"Import do mailing falhou (HTTP {int(exc.code)}).",
            details={"status_code": int(exc.code), "response_body": detail},
        ) from exc
    import_response = _decode_json(body)
    import_data = import_response.get("data") if isinstance(import_response.get("data"), dict) else {}
    import_task_id = str(import_data.get("task_id") or "").strip()
    if status_code >= 400 or not import_task_id:
        raise FileAppTipo1ManualPipelineError(
            step="step6_import",
            message="Import do mailing não retornou task_id válido.",
            details={"status_code": status_code, "response_body": body},
        )
    step_results.append({"step": "step6_import", "status_code": status_code, "import_task_id": import_task_id})

    if defer_step7_link_flow:
        step_results.append(
            {
                "step": "step7_link_flow",
                "status": "deferred",
                "mode": "async_celery",
            }
        )
    else:
        # Step 7
        association_payload = {
            "mailing_ids_added": [mailing_uuid],
            "mailing_ids_removed": [],
            "linked_by": linked_by,
            "call_origin": "file_event",
        }
        try:
            status_code, body = await asyncio.to_thread(
                _json_request,
                method="POST",
                url=f"{base_url}/v2/flow/{flow_uuid}/mailings",
                headers=json_headers,
                payload=association_payload,
                timeout_seconds=settings.sync_ws_timeout_seconds,
            )
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise FileAppTipo1ManualPipelineError(
                step="step7_link_flow",
                message=f"Associação de mailing ao flow falhou (HTTP {int(exc.code)}).",
                details={"status_code": int(exc.code), "response_body": detail},
            ) from exc
        if status_code >= 400:
            raise FileAppTipo1ManualPipelineError(
                step="step7_link_flow",
                message="Associação de mailing ao flow retornou erro.",
                details={"status_code": status_code, "response_body": body},
            )
        step_results.append({"step": "step7_link_flow", "status_code": status_code})

    return {
        "status": "done",
        "mailing_uuid": mailing_uuid,
        "import_task_id": import_task_id,
        "steps": step_results,
    }
