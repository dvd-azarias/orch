from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from app.core.config import Settings


@dataclass(frozen=True)
class IdentidadePersonFlowLinkResult:
    success: bool
    status_code: int | None
    attempts: int
    reason: str
    message: str | None = None


def _post_json(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> tuple[int, str]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url=url, data=raw, headers=headers, method="POST")
    with request.urlopen(req, timeout=timeout_seconds) as response:
        return int(response.status), response.read().decode("utf-8", errors="replace")


def _response_result(body: str, *, mailing_uuid: str) -> tuple[bool, str | None]:
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return False, "Resposta inválida do Target Core."
    data = payload.get("data") if isinstance(payload, dict) else None
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    results = first.get("results") if isinstance(first.get("results"), dict) else {}
    linked = results.get("linked") if isinstance(results.get("linked"), list) else []
    if mailing_uuid in {str(value) for value in linked}:
        return True, None
    errors = results.get("errors") if isinstance(results.get("errors"), dict) else {}
    messages = errors.get(mailing_uuid)
    if isinstance(messages, list):
        safe_message = "; ".join(str(value) for value in messages if value)[:500]
        return False, safe_message or "Target Core recusou o vínculo da lista."
    return False, "Target Core não confirmou o vínculo da lista."


async def link_identidade_mailing_to_current_flow(
    *,
    settings: Settings,
    workspace_uuid: str,
    flow_uuid: str,
    mailing_uuid: str,
    max_attempts: int = 3,
) -> IdentidadePersonFlowLinkResult:
    base_url = str(settings.target_core_api_base_url or settings.sync_webhook_base_url or "").strip().rstrip("/")
    bearer_token = str(settings.target_core_api_bearer_token or "").strip()
    if not base_url:
        return IdentidadePersonFlowLinkResult(False, None, 0, "target_core_base_url_not_configured")
    if not bearer_token:
        return IdentidadePersonFlowLinkResult(False, None, 0, "target_core_bearer_token_not_configured")

    target_url = f"{base_url}/v2/flow/{flow_uuid}/mailings"
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {bearer_token}",
        "content-type": "application/json",
        "x-application": "target",
        "X-WORKSPACE-UUID": workspace_uuid,
    }
    payload = {
        "mailing_ids_added": [mailing_uuid],
        "mailing_ids_removed": [],
        "linked_by": None,
        "call_origin": "identidade_person",
    }
    attempts = max(1, int(max_attempts))
    for attempt in range(1, attempts + 1):
        status_code: int | None = None
        response_body = ""
        try:
            status_code, response_body = await asyncio.to_thread(
                _post_json,
                url=target_url,
                headers=headers,
                payload=payload,
                timeout_seconds=settings.sync_ws_timeout_seconds,
            )
        except HTTPError as exc:
            status_code = int(exc.code)
            response_body = exc.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError) as exc:
            if attempt < attempts:
                await asyncio.sleep(0.5 * attempt)
                continue
            reason = exc.reason if isinstance(exc, URLError) else str(exc)
            return IdentidadePersonFlowLinkResult(
                False,
                None,
                attempt,
                "target_core_unreachable",
                str(reason)[:500],
            )

        if status_code is not None and (status_code == 429 or status_code >= 500) and attempt < attempts:
            await asyncio.sleep(0.5 * attempt)
            continue
        if status_code is None or status_code >= 400:
            return IdentidadePersonFlowLinkResult(
                False,
                status_code,
                attempt,
                "target_core_http_error",
                f"Target Core respondeu HTTP {status_code}.",
            )
        success, message = _response_result(response_body, mailing_uuid=mailing_uuid)
        return IdentidadePersonFlowLinkResult(
            success,
            status_code,
            attempt,
            "linked" if success else "target_core_link_not_confirmed",
            message,
        )

    return IdentidadePersonFlowLinkResult(False, None, attempts, "target_core_unreachable")
