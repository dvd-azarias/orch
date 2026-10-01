from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError

from app.core.config import Settings, get_settings


class FlowBuilderTargetError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class TargetResponse:
    status_code: int
    payload: dict[str, Any]


def _request_json(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    payload: dict[str, Any] | None = None,
) -> TargetResponse:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = request.Request(url=url, method=method, data=body, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:  # noqa: S310
            raw = response.read().decode("utf-8", errors="replace")
            status_code = int(response.status)
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status_code = int(exc.code)
    except (URLError, TimeoutError) as exc:
        raise FlowBuilderTargetError(
            "target_core_unreachable",
            "Target Core indisponível para o Flow Builder.",
        ) from exc
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise FlowBuilderTargetError(
            "target_core_invalid_json",
            "Target Core devolveu uma resposta inválida.",
            status_code=status_code,
        ) from exc
    if not isinstance(parsed, dict):
        parsed = {"data": parsed}
    if status_code >= 400:
        message = str(parsed.get("message") or parsed.get("detail") or "Target Core recusou a operação.")
        raise FlowBuilderTargetError(
            "target_core_http_error",
            message,
            status_code=status_code,
        )
    return TargetResponse(status_code=status_code, payload=parsed)


def _target_context(
    *,
    workspace_uuid: str,
    actor: str,
    settings: Settings,
) -> tuple[str, dict[str, str], float]:
    base_url = str(settings.target_core_api_base_url or "").strip().rstrip("/")
    bearer = str(settings.target_core_api_bearer_token or "").strip()
    if not base_url or not bearer:
        raise FlowBuilderTargetError(
            "target_core_not_configured",
            "Integração do Flow Builder com o Target Core não configurada.",
        )
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
        "X-WORKSPACE-UUID": workspace_uuid,
        "X-User-UUID": actor,
        "X-Application": "target",
    }
    return base_url, headers, float(settings.orch_flow_builder_target_timeout_seconds)


async def fetch_orchestration_catalog(
    *,
    workspace_uuid: str,
    actor: str,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    resolved = settings or get_settings()
    base_url, headers, timeout = _target_context(
        workspace_uuid=workspace_uuid,
        actor=actor,
        settings=resolved,
    )
    query = parse.urlencode({"mode": "orchestration"})
    response = await asyncio.to_thread(
        _request_json,
        method="GET",
        url=f"{base_url}/v1/flow-catalog-tasks?{query}",
        headers=headers,
        timeout_seconds=timeout,
    )
    data = response.payload.get("data")
    if not isinstance(data, list):
        raise FlowBuilderTargetError(
            "target_core_catalog_invalid",
            "Target Core não devolveu um catálogo de orquestração válido.",
            status_code=response.status_code,
        )
    return [item for item in data if isinstance(item, dict)]
