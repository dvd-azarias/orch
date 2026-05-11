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

    content = file_data.get("content")
    row_index = file_data.get("row_index")

    row_identifier: str | None = None
    if isinstance(content, dict):
        for key in ("cpf", "code", "id", "external_id"):
            value = content.get(key)
            text_value = "" if value is None else str(value).strip()
            if text_value:
                row_identifier = text_value
                break
    if not row_identifier and row_index is not None:
        text_row_index = str(row_index).strip()
        if text_row_index:
            row_identifier = f"row_{text_row_index}"

    if row_identifier:
        entity = f"{file_id}:{row_identifier}"
        entity_address = f"{folder_path}/{original_name}#{row_identifier}"
        entity_session_id = entity
    else:
        entity = file_id
        entity_address = f"{folder_path}/{original_name}"
        entity_session_id = file_id

    mapping_template_id = str(file_data.get("mapping_template_id", "")).strip()
    if not mapping_template_id:
        mapping_template_id = str(payload.get("mapping_template_id", "")).strip()
    entity_type = "person" if mapping_template_id else "file"

    if entity_type == "person":
        person_id = str(file_data.get("person_id", "")).strip()
        if not person_id and isinstance(content, dict):
            person_id = str(content.get("person_id", "")).strip()
        if person_id:
            entity = person_id
            entity_session_id = person_id

    return SessionExtraction(
        entity=entity,
        entity_type=entity_type,
        entity_address=entity_address,
        entity_session_id=entity_session_id,
    )
