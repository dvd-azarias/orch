from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from app.schemas.orch import SessionExtraction


def is_arquivos_app(payload: dict[str, Any]) -> bool:
    file_data = payload.get("file")
    if not isinstance(file_data, dict):
        return False

    has_file_fields = all(key in file_data for key in ("id", "original_name", "folder_path"))
    has_s3_signals = "EventName" in payload or "Records" in payload
    return has_file_fields or has_s3_signals


def extract_arquivos_session_fields(payload: dict[str, Any]) -> SessionExtraction:
    file_data = payload.get("file")
    if not isinstance(file_data, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Payload de ArquivosApp inválido: campo 'file' ausente ou inválido.",
        )

    file_id = str(file_data.get("id", "")).strip()
    folder_path = str(file_data.get("folder_path", "")).strip()
    original_name = str(file_data.get("original_name", "")).strip()

    if not (file_id and folder_path and original_name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Payload de ArquivosApp inválido: campos mínimos esperados "
                "'file.id', 'file.folder_path' e 'file.original_name'."
            ),
        )

    return SessionExtraction(
        entity=file_id,
        entity_type="file",
        entity_address=f"{folder_path}/{original_name}",
        entity_session_id=file_id,
    )
