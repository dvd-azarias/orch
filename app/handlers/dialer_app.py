from __future__ import annotations

import ast
import re
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


def is_dialer(payload: dict[str, Any]) -> bool:
    has_core_fields = any(key in payload for key in ("hangup", "makecall", "uniqueid"))
    if not has_core_fields:
        return False

    hangup = payload.get("hangup")
    makecall = payload.get("makecall")

    hangup_ok = isinstance(hangup, dict) and hangup.get("Event") == "Hangup"
    makecall_ok = isinstance(makecall, dict) and makecall.get("Event") == "DialBegin"

    return hangup_ok or makecall_ok


def _extract_phone_from_dialer(payload: dict[str, Any]) -> str | None:
    hangup = payload.get("hangup")
    if isinstance(hangup, dict):
        cdr_mailing_data = hangup.get("CdrMailingData")
        if isinstance(cdr_mailing_data, str) and cdr_mailing_data.strip():
            try:
                parsed = ast.literal_eval(cdr_mailing_data)
                if isinstance(parsed, dict):
                    phone = parsed.get("phone")
                    if phone:
                        return str(phone).strip()
            except (SyntaxError, ValueError):
                pass

    makecall = payload.get("makecall")
    if isinstance(makecall, dict):
        dial_string = makecall.get("DialString")
        if isinstance(dial_string, str) and dial_string.strip():
            tail = dial_string.split("/")[-1].strip()
            digits = "".join(re.findall(r"\d+", tail))
            return digits or tail

    return None


def extract_dialer_session_fields(payload: dict[str, Any]) -> SessionExtraction:
    hangup = payload.get("hangup") if isinstance(payload.get("hangup"), dict) else {}
    makecall = payload.get("makecall") if isinstance(payload.get("makecall"), dict) else {}

    phone = normalize_br_mobile_missing_ninth_digit(_extract_phone_from_dialer(payload))
    entity = _pick_first_non_empty(
        hangup.get("DialerActionID"),
        hangup.get("PoolActionID"),
        hangup.get("DialerCampaignUUID"),
        payload.get("uniqueid"),
        phone,
    )
    entity_session_id = _pick_first_non_empty(
        payload.get("uniqueid"),
        hangup.get("Uniqueid"),
        hangup.get("Linkedid"),
        makecall.get("DestUniqueid"),
    )
    entity_address = _pick_first_non_empty(phone, entity)

    if not (entity and entity_session_id and entity_address):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Payload de DialerApp inválido: não foi possível extrair "
                "entity/entity_address/entity_session_id mínimos."
            ),
        )

    return SessionExtraction(
        entity=entity,
        entity_type="person",
        entity_address=entity_address,
        entity_session_id=entity_session_id,
    )
