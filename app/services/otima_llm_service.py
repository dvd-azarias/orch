from __future__ import annotations

import json
import re
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from app.core.config import get_settings

_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def _extract_content_from_response(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            if chunks:
                return "\n".join(chunks)

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = payload.get("output")
    if isinstance(output, list) and output:
        first = output[0] if isinstance(output[0], dict) else {}
        content = first.get("content")
        if isinstance(content, list):
            chunks: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            if chunks:
                return "\n".join(chunks)
    return ""


def _extract_first_json_object(raw_text: str) -> dict[str, Any] | None:
    text = (raw_text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    match = _JSON_BLOCK_RE.search(text)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _build_headers(*, global_api_key: str, workspace_uuid: str | None, workspace_api_key: str | None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {global_api_key}",
        "Content-Type": "application/json",
    }
    if workspace_uuid:
        headers["x-workspace-uuid"] = workspace_uuid
    if workspace_api_key:
        headers["x-api-key"] = workspace_api_key
        headers["x-workspace-api-key"] = workspace_api_key
    return headers


def _http_json_request(*, url: str, payload: dict[str, Any], headers: dict[str, str], timeout_seconds: float) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url=url, method="POST", data=data, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", errors="replace")
            status_code = int(resp.status)
    except HTTPError as err:
        body = err.read().decode("utf-8", errors="replace") if hasattr(err, "read") else str(err)
        status_code = int(getattr(err, "code", 500) or 500)
    except URLError as err:
        raise RuntimeError(f"Falha de conectividade com Otima LLM: {err.reason}") from err

    try:
        parsed = json.loads(body) if body else {}
    except Exception:
        parsed = {"raw": body}
    return status_code, parsed if isinstance(parsed, dict) else {"raw": parsed}


def execute_otima_llm_prompt(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    workspace_uuid: str | None,
    workspace_api_key: str | None,
) -> dict[str, Any]:
    settings = get_settings()
    base_url = (settings.otima_llm_api_gateway or settings.otima_llm_api_base_url or "").strip().rstrip("/")
    global_api_key = (settings.otima_llm_api_key or "").strip()
    timeout_seconds = float(settings.otima_llm_api_timeout_seconds or 10.0)
    if not base_url:
        raise RuntimeError("OTIMA_LLM_API_BASE_URL/OTIMA_LLM_API_GATEWAY não configurado.")
    if not global_api_key:
        raise RuntimeError("OTIMA_LLM_API_KEY não configurado.")

    headers = _build_headers(
        global_api_key=global_api_key,
        workspace_uuid=workspace_uuid,
        workspace_api_key=workspace_api_key,
    )
    urls = [
        f"{base_url}/v1/chat/completions",
        f"{base_url}/chat/completions",
        f"{base_url}/v1/responses",
        f"{base_url}/responses",
    ]

    last_error: str | None = None
    for url in urls:
        is_responses = url.endswith("/responses")
        payload = (
            {
                "model": model,
                "input": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            if is_responses
            else {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
            }
        )
        status_code, response_json = _http_json_request(
            url=url,
            payload=payload,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
        if 200 <= status_code < 300:
            raw_text = _extract_content_from_response(response_json)
            return {
                "status_code": status_code,
                "endpoint": url,
                "raw_text": raw_text,
                "parsed_json": _extract_first_json_object(raw_text),
                "response_json": response_json,
            }
        if status_code in {404, 405}:
            last_error = f"{status_code} em {url}"
            continue
        last_error = f"{status_code} em {url}"
        break

    raise RuntimeError(f"Falha na Otima LLM: {last_error or 'sem resposta válida'}")

