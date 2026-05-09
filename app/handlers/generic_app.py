from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from app.schemas.orch import SessionExtraction


def is_generic(payload: dict[str, Any]) -> bool:
    return "external_id" in payload


def extract_generic_session_fields(payload: dict[str, Any]) -> SessionExtraction:
    external_id = str(payload.get("external_id", "")).strip()
    if not external_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Payload de GenericApp inválido: campo 'external_id' obrigatório.",
        )

    return SessionExtraction(
        entity=external_id,
        entity_type="api_request",
        entity_address=external_id,
        entity_session_id=external_id,
    )
