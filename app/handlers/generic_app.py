from __future__ import annotations

from uuid import uuid4
from typing import Any

from fastapi import HTTPException, status

from app.schemas.orch import SessionExtraction


def is_callback(payload: dict[str, Any]) -> bool:
    event_name = str(payload.get("event_name", "")).strip().lower()
    if event_name != "callback":
        return False
    entity = str(payload.get("entity", "")).strip()
    return bool(entity)


def is_generic(payload: dict[str, Any]) -> bool:
    if is_callback(payload):
        return True
    if "external_id" in payload:
        return True
    return bool(payload)


def extract_callback_session_fields(payload: dict[str, Any]) -> SessionExtraction:
    entity = str(payload.get("entity", "")).strip()
    if not entity:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Payload de callback inválido: campo 'entity' obrigatório.",
        )

    entity_type = str(payload.get("entity_type", "")).strip() or "person"
    entity_address = str(payload.get("entity_address", "")).strip() or entity
    entity_session_id = str(payload.get("entity_session_id", "")).strip() or entity

    return SessionExtraction(
        entity=entity,
        entity_type=entity_type,
        entity_address=entity_address,
        entity_session_id=entity_session_id,
    )


def extract_generic_session_fields(payload: dict[str, Any]) -> SessionExtraction:
    if is_callback(payload):
        return extract_callback_session_fields(payload)

    external_id = str(payload.get("external_id", "")).strip()
    if not external_id:
        external_id = f"generated-{uuid4()}"

    return SessionExtraction(
        entity=external_id,
        entity_type="api_request",
        entity_address=external_id,
        entity_session_id=external_id,
    )
