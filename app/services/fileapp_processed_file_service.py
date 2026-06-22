from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from app.core.config import Settings

_PROCESSADOS_FOLDER_NAME = "processados"
_POST_PROCESS_RETRY_ATTEMPTS = 5
_POST_PROCESS_RETRY_INTERVAL_SECONDS = 15.0


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

    target_folder = f"{folder_path}/{_PROCESSADOS_FOLDER_NAME}"
    headers = _build_headers(settings=settings, workspace_uuid=workspace_uuid)

    try:
        await _request_json_with_retry(
            method="POST",
            url=f"{base_url}/files/folders",
            headers=headers,
            payload={"name": _PROCESSADOS_FOLDER_NAME, "parent_path": folder_path},
            timeout_seconds=settings.sync_ws_timeout_seconds,
        )
    except HTTPError as exc:
        if int(exc.code) not in {409, 422}:
            detail = exc.read().decode("utf-8", errors="replace")
            raise FileAppProcessedFileError(
                code="create_processados_folder_failed",
                message=f"Falha ao criar pasta processados (HTTP {int(exc.code)}).",
                details={"status_code": int(exc.code), "response_body": detail},
            ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise FileAppProcessedFileError(
            code="create_processados_folder_failed",
            message="Falha ao criar pasta processados após retries.",
            details={"error_type": type(exc).__name__, "error_message": str(exc)},
        ) from exc

    metadata_url = f"{base_url}/files/metadata/{file_id}"
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    renamed_to = ""
    for index in range(0, 1000):
        candidate_name = _build_rename_candidate(original_name, timestamp=suffix, index=index)
        try:
            await _request_json_with_retry(
                method="PATCH",
                url=metadata_url,
                headers=headers,
                payload={"original_name": candidate_name},
                timeout_seconds=settings.sync_ws_timeout_seconds,
            )
        except HTTPError as exc:
            if _is_name_conflict(int(exc.code)) and index < 999:
                continue
            detail = exc.read().decode("utf-8", errors="replace")
            raise FileAppProcessedFileError(
                code="rename_processed_file_failed",
                message=f"Falha ao renomear arquivo processado (HTTP {int(exc.code)}).",
                details={
                    "status_code": int(exc.code),
                    "response_body": detail,
                    "last_candidate": candidate_name,
                },
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise FileAppProcessedFileError(
                code="rename_processed_file_failed",
                message="Falha ao renomear arquivo processado após retries.",
                details={
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "last_candidate": candidate_name,
                },
            ) from exc

        try:
            await _request_json_with_retry(
                method="PATCH",
                url=metadata_url,
                headers=headers,
                payload={"folder_path": target_folder},
                timeout_seconds=settings.sync_ws_timeout_seconds,
            )
            renamed_to = candidate_name
            break
        except HTTPError as exc:
            if _is_name_conflict(int(exc.code)) and index < 999:
                continue
            detail = exc.read().decode("utf-8", errors="replace")
            raise FileAppProcessedFileError(
                code="move_file_to_processados_failed",
                message=f"Falha ao mover arquivo para processados (HTTP {int(exc.code)}).",
                details={
                    "status_code": int(exc.code),
                    "response_body": detail,
                    "last_candidate": candidate_name,
                },
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise FileAppProcessedFileError(
                code="move_file_to_processados_failed",
                message="Falha ao mover arquivo para processados após retries.",
                details={
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "last_candidate": candidate_name,
                },
            ) from exc

    if not renamed_to:
        raise FileAppProcessedFileError(
            code="rename_processed_file_failed",
            message="Não foi possível gerar nome único para arquivo processado.",
            details={"base_name": original_name, "suffix": suffix},
        )

    return {
        "status": "done",
        "file_id": file_id,
        "source_folder": folder_path,
        "target_folder": target_folder,
        "source_name": original_name,
        "target_name": renamed_to,
    }
