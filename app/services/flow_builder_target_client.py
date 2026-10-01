from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError
from uuid import UUID

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


@dataclass(frozen=True)
class TargetDraftResult:
    flow_uuid: UUID
    draft_checksum: str
    slug: str


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


def _extract_draft_result(
    payload: dict[str, Any],
    *,
    expected_session_id: str,
    expected_slug: str,
) -> TargetDraftResult:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FlowBuilderTargetError(
            "target_core_draft_invalid",
            "Target Core não devolveu o rascunho criado.",
        )
    summary = data.get("summary")
    draft = data.get("draft_revision")
    if not isinstance(summary, dict) or not isinstance(draft, dict):
        raise FlowBuilderTargetError(
            "target_core_draft_invalid",
            "Target Core devolveu um rascunho incompleto.",
        )
    definition = draft.get("definition")
    metadata = definition.get("builder_metadata") if isinstance(definition, dict) else None
    if (
        not isinstance(definition, dict)
        or definition.get("mode") != "orchestration"
        or not isinstance(metadata, dict)
        or str(metadata.get("builder_session_id") or "") != expected_session_id
        or str(summary.get("slug") or "") != expected_slug
    ):
        raise FlowBuilderTargetError(
            "target_core_draft_identity_mismatch",
            "O rascunho devolvido pelo Target Core não pertence a esta sessão do Builder.",
        )
    try:
        flow_uuid = UUID(str(summary["id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise FlowBuilderTargetError(
            "target_core_draft_invalid",
            "Target Core devolveu um identificador de fluxo inválido.",
        ) from exc
    checksum = str(draft.get("checksum") or "").strip()
    if not checksum:
        raise FlowBuilderTargetError(
            "target_core_draft_invalid",
            "Target Core não devolveu o checksum do rascunho.",
        )
    return TargetDraftResult(flow_uuid=flow_uuid, draft_checksum=checksum, slug=expected_slug)


async def _recover_orchestration_draft(
    *,
    base_url: str,
    headers: dict[str, str],
    timeout: float,
    slug: str,
    session_id: str,
) -> TargetDraftResult | None:
    query = parse.urlencode(
        [("search", slug), ("page", "1"), ("per_page", "100"), ("mode[]", "orchestration")]
    )
    response = await asyncio.to_thread(
        _request_json,
        method="GET",
        url=f"{base_url}/v2/flow?{query}",
        headers=headers,
        timeout_seconds=timeout,
    )
    data = response.payload.get("data")
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        summary = item.get("summary")
        if not isinstance(summary, dict) or str(summary.get("slug") or "") != slug:
            continue
        flow_id = str(summary.get("id") or "").strip()
        if not flow_id:
            continue
        detail = await asyncio.to_thread(
            _request_json,
            method="GET",
            url=f"{base_url}/v2/flow/{flow_id}?compact=false",
            headers=headers,
            timeout_seconds=timeout,
        )
        return _extract_draft_result(
            detail.payload,
            expected_session_id=session_id,
            expected_slug=slug,
        )
    return None


async def create_orchestration_draft(
    *,
    workspace_uuid: str,
    actor: str,
    builder_session_id: str,
    definition: dict[str, Any],
    settings: Settings | None = None,
) -> TargetDraftResult:
    resolved = settings or get_settings()
    base_url, headers, timeout = _target_context(
        workspace_uuid=workspace_uuid,
        actor=actor,
        settings=resolved,
    )
    safe_session_id = str(UUID(builder_session_id))
    slug = f"orch-ai-{safe_session_id}"
    desired_definition = deepcopy(definition)
    metadata = desired_definition.setdefault("builder_metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
        desired_definition["builder_metadata"] = metadata
    metadata["builder_session_id"] = safe_session_id
    metadata["owner"] = "orch"
    metadata["kind"] = "ai_flow_builder"
    info = desired_definition.get("info")
    display_name = str(info.get("name") or "Fluxo criado com IA") if isinstance(info, dict) else "Fluxo criado com IA"
    payload = {
        "slug": slug,
        "display_name": display_name,
        "definition": desired_definition,
        "change_note": "Rascunho criado pelo AI Flow Builder do ORCH.",
    }
    try:
        response = await asyncio.to_thread(
            _request_json,
            method="POST",
            url=f"{base_url}/v2/flow",
            headers=headers,
            timeout_seconds=timeout,
            payload=payload,
        )
    except FlowBuilderTargetError as error:
        if error.status_code != 409:
            raise
        recovered = await _recover_orchestration_draft(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            slug=slug,
            session_id=safe_session_id,
        )
        if recovered is None:
            raise
        return recovered
    return _extract_draft_result(
        response.payload,
        expected_session_id=safe_session_id,
        expected_slug=slug,
    )
