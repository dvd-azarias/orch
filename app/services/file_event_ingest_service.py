from __future__ import annotations

import asyncio
import copy
import csv
import io
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from fastapi import HTTPException, status

from app.core.config import Settings


def parse_file_rows(raw_bytes: bytes) -> list[dict[str, Any]]:
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return []

    sample = text[:2048]
    delimiters = ",;|\t"
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=delimiters)
        delimiter = dialect.delimiter
    except Exception:
        delimiter = ","

    buffer = io.StringIO(text, newline="")
    reader = csv.DictReader(buffer, delimiter=delimiter)
    if not reader.fieldnames:
        return []

    rows: list[dict[str, Any]] = []
    for row in reader:
        normalized: dict[str, Any] = {}
        for key, value in row.items():
            if key is None:
                continue
            clean_key = str(key).strip()
            if not clean_key:
                continue
            normalized[clean_key] = "" if value is None else str(value).strip()
        if any(str(v).strip() for v in normalized.values()):
            rows.append(normalized)
    return rows


def _download_file_bytes(*, url: str, headers: dict[str, str], timeout_seconds: int = 30) -> bytes:
    req = request.Request(url=url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            body = response.read()
    except HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Falha ao baixar arquivo (HTTP {exc.code}).",
        ) from exc
    except URLError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Falha ao baixar arquivo: {exc.reason}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Falha inesperada ao baixar arquivo: {type(exc).__name__}",
        ) from exc
    return body


def build_row_payloads(base_payload: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        row_payload = copy.deepcopy(base_payload)
        file_data = row_payload.get("file")
        if not isinstance(file_data, dict):
            file_data = {}
            row_payload["file"] = file_data
        file_data["content"] = row
        file_data["row_index"] = idx
        file_data["row_count"] = len(rows)
        payloads.append(row_payload)
    return payloads


async def expand_arquivos_payload_into_rows(
    payload: dict[str, Any],
    *,
    settings: Settings,
) -> list[dict[str, Any]]:
    file_data = payload.get("file")
    if not isinstance(file_data, dict):
        return [payload]

    file_url = str(file_data.get("url", "")).strip()
    event_workspace = str(file_data.get("workspace_uuid", "")).strip()
    if not file_url:
        return [payload]

    if not event_workspace:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Payload de ArquivosApp inválido: file.workspace_uuid ausente.",
        )
    if not settings.sync_ws_client_id or not settings.sync_ws_client_secret:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Credenciais SYNC_WS_* não configuradas para leitura de arquivo.",
        )

    headers = {
        "x-workspace-uuid": event_workspace,
        "x-client-id": settings.sync_ws_client_id,
        "x-client-secret": settings.sync_ws_client_secret,
    }
    raw_bytes = await asyncio.to_thread(_download_file_bytes, url=file_url, headers=headers)
    rows = parse_file_rows(raw_bytes)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Arquivo recebido sem linhas válidas para ingestão.",
        )
    return build_row_payloads(payload, rows)

