from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from app.schemas.orch import SessionExtraction
from app.services.phone_normalizer import normalize_br_mobile_missing_ninth_digit


def _pick_first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def is_whatsapp(payload: dict[str, Any]) -> bool:
    if payload.get("object") != "whatsapp_business_account":
        return False

    entries = payload.get("entry")
    if not isinstance(entries, list) or not entries:
        return False

    for entry in entries:
        changes = entry.get("changes") if isinstance(entry, dict) else None
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            if value.get("messaging_product") != "whatsapp":
                continue
            statuses = value.get("statuses")
            if isinstance(statuses, list) and statuses:
                return True

    return False


def extract_whatsapp_session_fields(payload: dict[str, Any]) -> SessionExtraction:
    entries = payload.get("entry")
    contacts_wa_id: str | None = None
    recipient_id: str | None = None

    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            changes = entry.get("changes")
            if not isinstance(changes, list):
                continue
            for change in changes:
                if not isinstance(change, dict):
                    continue
                value = change.get("value")
                if not isinstance(value, dict):
                    continue
                contacts = value.get("contacts")
                statuses = value.get("statuses")
                if isinstance(contacts, list) and contacts and isinstance(contacts[0], dict):
                    contacts_wa_id = _pick_first_non_empty(contacts[0].get("wa_id"))
                if isinstance(statuses, list) and statuses and isinstance(statuses[0], dict):
                    recipient_id = _pick_first_non_empty(statuses[0].get("recipient_id"))
                if contacts_wa_id or recipient_id:
                    break
            if contacts_wa_id or recipient_id:
                break

    identity = normalize_br_mobile_missing_ninth_digit(_pick_first_non_empty(contacts_wa_id, recipient_id))
    session_id = normalize_br_mobile_missing_ninth_digit(_pick_first_non_empty(contacts_wa_id, recipient_id))
    if not (identity and session_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Payload de WhatsApp inválido: não foi possível extrair "
                "'contacts[].wa_id' ou 'statuses[].recipient_id'."
            ),
        )

    return SessionExtraction(
        entity=identity,
        entity_type="person",
        entity_address=identity,
        entity_session_id=session_id,
    )
