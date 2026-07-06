from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from app.core.config import Settings

_PROCESSADOS_FOLDER_NAME = "processados"
_FALHA_FOLDER_NAME = "falha"
_POST_PROCESS_RETRY_ATTEMPTS = 5
_POST_PROCESS_RETRY_INTERVAL_SECONDS = 15.0
_REUPLOAD_ENDPOINT_CANDIDATES = ("/files/upload", "/files")


class FileAppProcessedFileError(Exception):
    def __init__(self, *, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _normalize_folder_path(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized.strip("/")


def _build_headers(*, settings: Settings, workspace_uuid: str) -> dict[str, str]:
    client_id = str(settings.arquivos_client_id or "").strip()
    client_secret = str(settings.arquivos_client_secret or "").strip()
    if not client_id or not client_secret:
        raise FileAppProcessedFileError(
            code="missing_arquivos_credentials",
            message="Credenciais ARQUIVOS_CLIENT_* ausentes para pós-processamento do arquivo.",
        )
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "x-application": "files",
        "x-client-id": client_id,
        "x-client-secret": client_secret,
        "x-workspace-uuid": workspace_uuid,
    }


def _request_json(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url=url, data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=timeout_seconds) as response:
        return int(response.status), response.read().decode("utf-8", errors="replace")


def _request_bytes(
    *,
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
) -> bytes:
    req = request.Request(url=url, headers=headers, method="GET")
    with request.urlopen(req, timeout=timeout_seconds) as response:
        return response.read()


def _multipart_encode(
    *,
    file_name: str,
    content_type: str,
    file_bytes: bytes,
    folder_path: str,
) -> tuple[bytes, str]:
    boundary = f"orch-fileapp-{uuid.uuid4().hex}"
    lines: list[bytes] = []

    lines.append(f"--{boundary}\r\n".encode("utf-8"))
    lines.append(
        (
            'Content-Disposition: form-data; name="file"; '
            f'filename="{file_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    lines.append(file_bytes)
    lines.append(b"\r\n")

    for field_name, field_value in (
        ("folder_path", folder_path),
        ("parent_path", folder_path),
        ("name", file_name),
        ("original_name", file_name),
    ):
        lines.append(f"--{boundary}\r\n".encode("utf-8"))
        lines.append(f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'.encode("utf-8"))
        lines.append(str(field_value).encode("utf-8"))
        lines.append(b"\r\n")

    lines.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(lines), f"multipart/form-data; boundary={boundary}"


def _request_multipart(
    *,
    url: str,
    headers: dict[str, str],
    file_name: str,
    file_bytes: bytes,
    folder_path: str,
    timeout_seconds: float,
) -> tuple[int, str]:
    content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    data, multipart_content_type = _multipart_encode(
        file_name=file_name,
        content_type=content_type,
        file_bytes=file_bytes,
        folder_path=folder_path,
    )
    multipart_headers = dict(headers)
    multipart_headers["content-type"] = multipart_content_type
    req = request.Request(url=url, data=data, headers=multipart_headers, method="POST")
    with request.urlopen(req, timeout=timeout_seconds) as response:
        return int(response.status), response.read().decode("utf-8", errors="replace")


def _build_timestamped_name(original_name: str, *, suffix: str) -> str:
    name = str(original_name or "").strip()
    if "." not in name:
        return f"{name}_{suffix}"
    stem, ext = name.rsplit(".", 1)
    return f"{stem}_{suffix}.{ext}"


def _build_rename_candidate(original_name: str, *, timestamp: str, index: int) -> str:
    name = str(original_name or "").strip()
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        dot_ext = f".{ext}"
    else:
        stem, dot_ext = name, ""

    timestamped_pattern = re.compile(r"^(?P<base>.+?)_(?P<ts>\d{8}T\d{6}Z)(?:_(?P<seq>\d{3}))?$")
    matched = timestamped_pattern.fullmatch(stem)
    if matched:
        base_name = f"{matched.group('base')}_{matched.group('ts')}"
    else:
        base_name = _build_timestamped_name(original_name, suffix=timestamp).removesuffix(dot_ext)

    if index == 0:
        return f"{base_name}{dot_ext}"
    return f"{base_name}_{index:03d}{dot_ext}"


def _is_name_conflict(status_code: int) -> bool:
    return int(status_code) in {409, 422}


async def _request_json_with_retry(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    retry_not_found: bool = False,
) -> tuple[int, str]:
    attempts = max(1, int(_POST_PROCESS_RETRY_ATTEMPTS))
    interval_seconds = max(0.0, float(_POST_PROCESS_RETRY_INTERVAL_SECONDS))
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(
                _request_json,
                method=method,
                url=url,
                headers=headers,
                payload=payload,
                timeout_seconds=timeout_seconds,
            )
        except HTTPError as exc:
            code = int(exc.code)
            if retry_not_found and code == 404 and attempt < attempts:
                await asyncio.sleep(interval_seconds)
                continue
            if 500 <= code <= 599 and attempt < attempts:
                await asyncio.sleep(interval_seconds)
                continue
            raise
        except (URLError, TimeoutError, OSError):
            if attempt < attempts:
                await asyncio.sleep(interval_seconds)
                continue
            raise
    raise RuntimeError("retry_unreachable")


def _build_download_headers(*, settings: Settings, workspace_uuid: str) -> dict[str, str]:
    client_id = str(settings.sync_ws_client_id or "").strip()
    client_secret = str(settings.sync_ws_client_secret or "").strip()
    if not client_id or not client_secret:
        raise FileAppProcessedFileError(
            code="missing_sync_ws_credentials",
            message="Credenciais SYNC_WS_* ausentes para fallback de reupload de arquivo.",
        )
    return {
        "x-workspace-uuid": workspace_uuid,
        "x-client-id": client_id,
        "x-client-secret": client_secret,
    }


async def _download_source_file_with_retry(
    *,
    settings: Settings,
    workspace_uuid: str,
    file_url: str,
) -> bytes:
    attempts = max(1, int(_POST_PROCESS_RETRY_ATTEMPTS))
    interval_seconds = max(0.0, float(_POST_PROCESS_RETRY_INTERVAL_SECONDS))
    headers = _build_download_headers(settings=settings, workspace_uuid=workspace_uuid)
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(
                _request_bytes,
                url=file_url,
                headers=headers,
                timeout_seconds=settings.sync_ws_timeout_seconds,
            )
        except HTTPError as exc:
            if 500 <= int(exc.code) <= 599 and attempt < attempts:
                await asyncio.sleep(interval_seconds)
                continue
            detail = exc.read().decode("utf-8", errors="replace")
            raise FileAppProcessedFileError(
                code="download_file_for_reupload_failed",
                message=f"Falha ao baixar arquivo para reupload (HTTP {int(exc.code)}).",
                details={"status_code": int(exc.code), "response_body": detail},
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt < attempts:
                await asyncio.sleep(interval_seconds)
                continue
            raise FileAppProcessedFileError(
                code="download_file_for_reupload_failed",
                message="Falha ao baixar arquivo para reupload após retries.",
                details={"error_type": type(exc).__name__, "error_message": str(exc)},
            ) from exc

    raise FileAppProcessedFileError(
        code="download_file_for_reupload_failed",
        message="Falha ao baixar arquivo para reupload.",
    )


async def _upload_file_to_folder_with_retry(
    *,
    settings: Settings,
    base_url: str,
    headers: dict[str, str],
    file_name: str,
    file_bytes: bytes,
    target_folder: str,
    folder_label: str,
) -> dict[str, Any]:
    attempts = max(1, int(_POST_PROCESS_RETRY_ATTEMPTS))
    interval_seconds = max(0.0, float(_POST_PROCESS_RETRY_INTERVAL_SECONDS))
    endpoints = [f"{base_url}{suffix}" for suffix in _REUPLOAD_ENDPOINT_CANDIDATES]
    last_error: dict[str, Any] | None = None

    for endpoint in endpoints:
        for attempt in range(1, attempts + 1):
            try:
                status_code, body = await asyncio.to_thread(
                    _request_multipart,
                    url=endpoint,
                    headers=headers,
                    file_name=file_name,
                    file_bytes=file_bytes,
                    folder_path=target_folder,
                    timeout_seconds=settings.sync_ws_timeout_seconds,
                )
                return {
                    "status": "done",
                    "status_code": status_code,
                    "endpoint": endpoint,
                    "response_body": body,
                }
            except HTTPError as exc:
                code = int(exc.code)
                detail = exc.read().decode("utf-8", errors="replace")
                if code in {404, 405}:
                    last_error = {"status_code": code, "response_body": detail, "endpoint": endpoint}
                    break
                if 500 <= code <= 599 and attempt < attempts:
                    await asyncio.sleep(interval_seconds)
                    continue
                raise FileAppProcessedFileError(
                    code=f"reupload_file_to_{folder_label}_failed",
                    message=f"Falha no reupload para {folder_label} (HTTP {code}).",
                    details={
                        "status_code": code,
                        "response_body": detail,
                        "endpoint": endpoint,
                    },
                ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < attempts:
                    await asyncio.sleep(interval_seconds)
                    continue
                raise FileAppProcessedFileError(
                    code=f"reupload_file_to_{folder_label}_failed",
                    message=f"Falha no reupload para {folder_label} após retries.",
                    details={
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "endpoint": endpoint,
                    },
                ) from exc

    raise FileAppProcessedFileError(
        code=f"reupload_file_to_{folder_label}_failed",
        message=f"Nenhum endpoint de reupload aceitou a requisição para {folder_label}.",
        details=last_error or {},
    )


async def _move_or_reupload_to_folder(
    *,
    settings: Settings,
    workspace_uuid: str,
    base_url: str,
    headers: dict[str, str],
    folder_path: str,
    folder_name: str,
    original_name: str,
    file_id: str,
    file_url: str,
    suffix: str,
) -> dict[str, Any]:
    target_folder = f"{folder_path}/{folder_name}"
    create_folder_code = f"create_{folder_name}_folder_failed"
    move_code = f"move_file_to_{folder_name}_failed"

    try:
        await _request_json_with_retry(
            method="POST",
            url=f"{base_url}/files/folders",
            headers=headers,
            payload={"name": folder_name, "parent_path": folder_path},
            timeout_seconds=settings.sync_ws_timeout_seconds,
        )
    except HTTPError as exc:
        if int(exc.code) not in {409, 422}:
            detail = exc.read().decode("utf-8", errors="replace")
            raise FileAppProcessedFileError(
                code=create_folder_code,
                message=f"Falha ao criar pasta {folder_name} (HTTP {int(exc.code)}).",
                details={"status_code": int(exc.code), "response_body": detail},
            ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise FileAppProcessedFileError(
            code=create_folder_code,
            message=f"Falha ao criar pasta {folder_name} após retries.",
            details={"error_type": type(exc).__name__, "error_message": str(exc)},
        ) from exc

    metadata_url = f"{base_url}/files/metadata/{file_id}"
    for index in range(0, 1000):
        candidate_name = _build_rename_candidate(original_name, timestamp=suffix, index=index)
        try:
            await _request_json_with_retry(
                method="PATCH",
                url=metadata_url,
                headers=headers,
                payload={
                    "original_name": candidate_name,
                    "folder_path": target_folder,
                },
                timeout_seconds=settings.sync_ws_timeout_seconds,
                retry_not_found=True,
            )
            return {
                "status": "done",
                "file_id": file_id,
                "source_folder": folder_path,
                "target_folder": target_folder,
                "source_name": original_name,
                "target_name": candidate_name,
            }
        except HTTPError as exc:
            status_code = int(exc.code)
            detail = exc.read().decode("utf-8", errors="replace")
            if status_code == 404 and file_url:
                file_bytes = await _download_source_file_with_retry(
                    settings=settings,
                    workspace_uuid=workspace_uuid,
                    file_url=file_url,
                )
                reupload_result = await _upload_file_to_folder_with_retry(
                    settings=settings,
                    base_url=base_url,
                    headers=headers,
                    file_name=candidate_name,
                    file_bytes=file_bytes,
                    target_folder=target_folder,
                    folder_label=folder_name,
                )
                return {
                    "status": "done",
                    "file_id": file_id,
                    "source_folder": folder_path,
                    "target_folder": target_folder,
                    "source_name": original_name,
                    "target_name": candidate_name,
                    "fallback_reupload": reupload_result,
                }
            if _is_name_conflict(status_code) and index < 999:
                continue
            raise FileAppProcessedFileError(
                code=move_code,
                message=f"Falha ao mover/renomear arquivo para {folder_name} (HTTP {status_code}).",
                details={
                    "status_code": status_code,
                    "response_body": detail,
                    "last_candidate": candidate_name,
                },
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise FileAppProcessedFileError(
                code=move_code,
                message=f"Falha ao mover arquivo para {folder_name} após retries.",
                details={
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "last_candidate": candidate_name,
                },
            ) from exc

    raise FileAppProcessedFileError(
        code=f"rename_file_to_{folder_name}_failed",
        message=f"Não foi possível gerar nome único para arquivo em {folder_name}.",
        details={"base_name": original_name, "suffix": suffix},
    )


async def move_processed_file_to_processados(
    *,
    settings: Settings,
    workspace_uuid: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    base_url = str(settings.arquivos_base_url or "").strip().rstrip("/")
    if not base_url:
        raise FileAppProcessedFileError(
            code="missing_arquivos_base_url",
            message="ARQUIVOS_BASE_URL ausente para pós-processamento do arquivo.",
        )

    file_data = payload.get("file")
    if not isinstance(file_data, dict):
        raise FileAppProcessedFileError(
            code="invalid_payload_file",
            message="Payload sem objeto file para mover arquivo processado.",
        )

    file_id = str(file_data.get("id") or "").strip()
    folder_path = _normalize_folder_path(str(file_data.get("folder_path") or ""))
    original_name = str(file_data.get("original_name") or "").strip()
    file_url = str(file_data.get("url") or "").strip()
    if not file_id or not folder_path or not original_name:
        raise FileAppProcessedFileError(
            code="missing_file_fields",
            message="Campos obrigatórios ausentes em file (id/folder_path/original_name).",
        )

    lower_folder = folder_path.lower()
    if lower_folder.endswith(f"/{_PROCESSADOS_FOLDER_NAME}") or lower_folder == _PROCESSADOS_FOLDER_NAME:
        return {
            "status": "skipped",
            "reason": "already_in_processados",
            "file_id": file_id,
            "folder_path": folder_path,
            "original_name": original_name,
        }
    if lower_folder.endswith(f"/{_FALHA_FOLDER_NAME}") or lower_folder == _FALHA_FOLDER_NAME:
        return {
            "status": "skipped",
            "reason": "already_in_falha",
            "file_id": file_id,
            "folder_path": folder_path,
            "original_name": original_name,
        }

    headers = _build_headers(settings=settings, workspace_uuid=workspace_uuid)
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        return await _move_or_reupload_to_folder(
            settings=settings,
            workspace_uuid=workspace_uuid,
            base_url=base_url,
            headers=headers,
            folder_path=folder_path,
            folder_name=_PROCESSADOS_FOLDER_NAME,
            original_name=original_name,
            file_id=file_id,
            file_url=file_url,
            suffix=suffix,
        )
    except FileAppProcessedFileError as processados_error:
        falha_result = await _move_or_reupload_to_folder(
            settings=settings,
            workspace_uuid=workspace_uuid,
            base_url=base_url,
            headers=headers,
            folder_path=folder_path,
            folder_name=_FALHA_FOLDER_NAME,
            original_name=original_name,
            file_id=file_id,
            file_url=file_url,
            suffix=suffix,
        )
        return {
            **falha_result,
            "quarantine_folder": _FALHA_FOLDER_NAME,
            "processados_error": {
                "code": processados_error.code,
                "message": processados_error.message,
                "details": processados_error.details,
            },
        }
