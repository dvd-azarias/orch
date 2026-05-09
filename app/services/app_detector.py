from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from app.handlers.arquivos_app import is_arquivos_app
from app.handlers.dialer_app import is_dialer
from app.handlers.generic_app import is_generic
from app.handlers.whatsapp import is_whatsapp


APP_ARQUIVOS = "ArquivosApp"
APP_WHATSAPP = "WhatsApp"
APP_DIALER = "DialerApp"
APP_GENERIC = "GenericApp"


def detect_app(payload: dict[str, Any]) -> str:
    if is_arquivos_app(payload):
        return APP_ARQUIVOS
    if is_whatsapp(payload):
        return APP_WHATSAPP
    if is_dialer(payload):
        return APP_DIALER
    if is_generic(payload):
        return APP_GENERIC

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Payload não reconhecido. Para GenericApp, informe ao menos o campo 'external_id'.",
    )
