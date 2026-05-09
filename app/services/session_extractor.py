from __future__ import annotations

from typing import Any

from app.handlers.arquivos_app import extract_arquivos_session_fields
from app.handlers.dialer_app import extract_dialer_session_fields
from app.handlers.generic_app import extract_generic_session_fields
from app.handlers.whatsapp import extract_whatsapp_session_fields
from app.schemas.orch import SessionExtraction
from app.services.app_detector import APP_ARQUIVOS, APP_DIALER, APP_GENERIC, APP_WHATSAPP


def extract_session_fields(app_name: str, payload: dict[str, Any]) -> SessionExtraction:
    if app_name == APP_ARQUIVOS:
        return extract_arquivos_session_fields(payload)
    if app_name == APP_WHATSAPP:
        return extract_whatsapp_session_fields(payload)
    if app_name == APP_DIALER:
        return extract_dialer_session_fields(payload)
    if app_name == APP_GENERIC:
        return extract_generic_session_fields(payload)

    raise ValueError(f"App não suportada para extração: {app_name}")
