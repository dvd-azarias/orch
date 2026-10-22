from __future__ import annotations
import asyncio, json, re
from dataclasses import dataclass
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError
from app.core.config import get_settings
_PHONE_DIGITS_RE = re.compile(r"\D+")
_QUALITY_BRANCHES = {"GREEN": "verde", "YELLOW": "amarelo", "RED": "vermelho"}
class WhatsAppHealthError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message); self.code = code; self.message = message
@dataclass(frozen=True)
class WhatsAppHealthResult:
    number: str; quality_code: str; branch: str; status_description: str | None; updated_at: str | None
def _selected_number(component: dict[str, Any]) -> str:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    raw: Any = params.get("number")
    if isinstance(raw, list): raw = raw[0] if len(raw) == 1 else None
    if isinstance(raw, dict):
        raw = next((raw[key] for key in ("number", "display_phone_number", "phone_number", "phone", "id") if raw.get(key) is not None), None)
    number = _PHONE_DIGITS_RE.sub("", str(raw or "").strip())
    if len(number) < 12 or len(number) > 15: raise WhatsAppHealthError("whatsapp_health_invalid_number", "Selecione um número WhatsApp no formato internacional, somente com dígitos.")
    return number
def _request_phone_number(*, base_url: str, number: str, headers: dict[str, str], timeout_seconds: float) -> tuple[int, Any]:
    req = request.Request(url=f"{base_url.rstrip('/')}/whatsapp/provider/{number}/phone-number", method="GET", headers=headers)
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:  # noqa: S310
            return int(response.status), json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc: return int(exc.code), None
    except (URLError, TimeoutError, json.JSONDecodeError) as exc: raise WhatsAppHealthError("whatsapp_health_api_unavailable", "Não foi possível consultar a qualidade do número WhatsApp.") from exc
async def query_whatsapp_health(*, component: dict[str, Any]) -> WhatsAppHealthResult:
    number = _selected_number(component); settings = get_settings()
    base_url, client_id, client_secret = str(settings.sync_ws_base_url or "").strip(), str(settings.sync_ws_client_id or "").strip(), str(settings.sync_ws_client_secret or "").strip()
    if not base_url: raise WhatsAppHealthError("whatsapp_health_sync_base_url_missing", "SYNC_WS_BASE_URL não configurada.")
    if not client_id or not client_secret: raise WhatsAppHealthError("whatsapp_health_sync_credentials_missing", "Credenciais Sync do WhatsApp não configuradas.")
    status_code, payload = await asyncio.to_thread(_request_phone_number, base_url=base_url, number=number, headers={"Accept":"application/json", "X-APPLICATION-CLIENT-ID":client_id, "X-APPLICATION-CLIENT-SECRET":client_secret}, timeout_seconds=max(1.0, float(settings.sync_ws_timeout_seconds)))
    if not 200 <= status_code < 300: raise WhatsAppHealthError("whatsapp_health_api_error", "A API de WhatsApp não retornou a qualidade do número.")
    data = payload.get("data") if isinstance(payload, dict) else None; rating = data.get("quality_rating") if isinstance(data, dict) else None
    quality_code = str(rating.get("code") if isinstance(rating, dict) else "").strip().upper(); branch = _QUALITY_BRANCHES.get(quality_code)
    if branch is None: raise WhatsAppHealthError("whatsapp_health_quality_unknown", "A Meta não disponibilizou uma qualidade GREEN, YELLOW ou RED para o número.")
    status = data.get("status") if isinstance(data, dict) else None
    return WhatsAppHealthResult(number, quality_code, branch, (str(status.get("description") or "").strip() or None) if isinstance(status, dict) else None, str(data.get("updated_at") or "").strip() or None)
