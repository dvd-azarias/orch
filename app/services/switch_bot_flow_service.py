from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

import redis

from app.core.config import Settings, get_settings


_RUNNER_TOKEN_CACHE_PREFIX = "orch:switch_bot_flow:runner_token"
_runner_token_memory_cache: dict[tuple[str, str], str] = {}
_runner_token_memory_lock = threading.Lock()


class SwitchBotFlowError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class RunnerDeliveryResult:
    status_code: int
    response_body: dict[str, Any]
    attempts: int

    @property
    def session_id(self) -> str | None:
        value = self.response_body.get("session_id")
        normalized = str(value or "").strip()
        return normalized or None


def resolve_target_flow_uuid(component: dict[str, Any]) -> str:
    parameters = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    flow = parameters.get("flow") if isinstance(parameters.get("flow"), dict) else {}
    target_flow_uuid = str(flow.get("id") or parameters.get("flow_uuid") or "").strip()
    if not target_flow_uuid:
        raise SwitchBotFlowError(
            "switch_bot_flow_missing_target_flow",
            "switch_bot_flow sem flow de destino configurado.",
        )
    return target_flow_uuid


def is_meta_user_message_payload(payload: Any) -> bool:
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return False
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return False
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
            messages = value.get("messages") if isinstance(value, dict) else None
            if isinstance(messages, list) and any(isinstance(item, dict) for item in messages):
                return True
    return False


def extract_meta_message_ids(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    message_ids: list[str] = []
    seen: set[str] = set()
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return []
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
            messages = value.get("messages") if isinstance(value, dict) else None
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                message_id = str(message.get("id") or "").strip()
                if message_id and message_id not in seen:
                    seen.add(message_id)
                    message_ids.append(message_id)
    return message_ids


def _redis_client(settings: Settings) -> redis.Redis | None:
    backend_url = str(settings.celery_result_backend or "").strip()
    if not backend_url.startswith(("redis://", "rediss://")):
        return None
    try:
        return redis.Redis.from_url(backend_url, socket_timeout=1.0, socket_connect_timeout=1.0)
    except Exception:
        return None


def _cache_key(*, workspace_uuid: str, target_flow_uuid: str) -> str:
    return f"{_RUNNER_TOKEN_CACHE_PREFIX}:{workspace_uuid}:{target_flow_uuid}"


def _read_cached_runner_token(
    *,
    workspace_uuid: str,
    target_flow_uuid: str,
    settings: Settings,
) -> str | None:
    key_tuple = (workspace_uuid, target_flow_uuid)
    with _runner_token_memory_lock:
        memory_value = _runner_token_memory_cache.get(key_tuple)
    if memory_value:
        return memory_value

    client = _redis_client(settings)
    if client is None:
        return None
    try:
        cached = client.get(_cache_key(workspace_uuid=workspace_uuid, target_flow_uuid=target_flow_uuid))
    except Exception:
        return None
    if isinstance(cached, bytes):
        cached = cached.decode("utf-8", errors="strict")
    value = str(cached or "").strip()
    if not value:
        return None
    with _runner_token_memory_lock:
        _runner_token_memory_cache[key_tuple] = value
    return value


def _write_cached_runner_token(
    *,
    workspace_uuid: str,
    target_flow_uuid: str,
    runner_token: str,
    settings: Settings,
) -> None:
    key_tuple = (workspace_uuid, target_flow_uuid)
    with _runner_token_memory_lock:
        _runner_token_memory_cache[key_tuple] = runner_token
    client = _redis_client(settings)
    if client is None:
        return
    try:
        client.set(
            _cache_key(workspace_uuid=workspace_uuid, target_flow_uuid=target_flow_uuid),
            runner_token,
        )
    except Exception:
        return


def clear_runner_token_cache(*, workspace_uuid: str, target_flow_uuid: str) -> None:
    settings = get_settings()
    key_tuple = (workspace_uuid, target_flow_uuid)
    with _runner_token_memory_lock:
        _runner_token_memory_cache.pop(key_tuple, None)
    client = _redis_client(settings)
    if client is None:
        return
    try:
        client.delete(_cache_key(workspace_uuid=workspace_uuid, target_flow_uuid=target_flow_uuid))
    except Exception:
        return


def resolve_runner_token(
    *,
    workspace_uuid: str,
    target_flow_uuid: str,
    settings: Settings | None = None,
) -> str:
    resolved_settings = settings or get_settings()
    cached = _read_cached_runner_token(
        workspace_uuid=workspace_uuid,
        target_flow_uuid=target_flow_uuid,
        settings=resolved_settings,
    )
    if cached:
        return cached

    base_url = str(resolved_settings.target_core_api_base_url or "").strip().rstrip("/")
    bearer = str(resolved_settings.target_core_api_bearer_token or "").strip()
    if not base_url:
        raise SwitchBotFlowError(
            "switch_bot_flow_target_core_base_url_missing",
            "TARGET_CORE_API_BASE_URL não configurada.",
        )
    if not bearer:
        raise SwitchBotFlowError(
            "switch_bot_flow_target_core_bearer_missing",
            "TARGET_CORE_API_BEARER_TOKEN não configurado.",
        )

    req = request.Request(
        url=f"{base_url}/v2/flow/{target_flow_uuid}?compact=true",
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer}",
            "X-WORKSPACE-UUID": workspace_uuid,
        },
    )
    try:
        with request.urlopen(  # noqa: S310
            req,
            timeout=max(1.0, resolved_settings.switch_bot_flow_http_timeout_seconds),
        ) as response:
            status_code = int(response.status)
            response_body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise SwitchBotFlowError(
            "switch_bot_flow_runner_token_http_error",
            "Target Core rejeitou a consulta do runner_token.",
            status_code=int(exc.code),
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise SwitchBotFlowError(
            "switch_bot_flow_runner_token_unavailable",
            "Target Core indisponível durante consulta do runner_token.",
        ) from exc

    if not 200 <= status_code < 300:
        raise SwitchBotFlowError(
            "switch_bot_flow_runner_token_http_error",
            "Target Core rejeitou a consulta do runner_token.",
            status_code=status_code,
        )
    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise SwitchBotFlowError(
            "switch_bot_flow_runner_token_invalid_response",
            "Target Core devolveu JSON inválido ao consultar runner_token.",
        ) from exc
    data = parsed.get("data") if isinstance(parsed, dict) else None
    summary = data.get("summary") if isinstance(data, dict) else None
    runner_token = str(summary.get("runner_token") or "").strip() if isinstance(summary, dict) else ""
    if not runner_token:
        raise SwitchBotFlowError(
            "switch_bot_flow_runner_token_missing",
            "Target Core não devolveu data.summary.runner_token.",
        )

    _write_cached_runner_token(
        workspace_uuid=workspace_uuid,
        target_flow_uuid=target_flow_uuid,
        runner_token=runner_token,
        settings=resolved_settings,
    )
    return runner_token


def deliver_meta_payload(
    *,
    runner_token: str,
    payload: dict[str, Any],
    settings: Settings | None = None,
) -> RunnerDeliveryResult:
    resolved_settings = settings or get_settings()
    base_url = str(resolved_settings.target_core_api_base_url or "").strip().rstrip("/")
    if not base_url:
        raise SwitchBotFlowError(
            "switch_bot_flow_target_core_base_url_missing",
            "TARGET_CORE_API_BASE_URL não configurada.",
        )
    if not is_meta_user_message_payload(payload):
        raise SwitchBotFlowError(
            "switch_bot_flow_meta_message_required",
            "switch_bot_flow requer um payload Meta contendo mensagem de usuário.",
        )

    url = f"{base_url}/v5/runner/tokens/{runner_token}/webhook/session"
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timeout_seconds = max(1.0, resolved_settings.switch_bot_flow_http_timeout_seconds)
    max_attempts = max(1, min(resolved_settings.switch_bot_flow_max_attempts, 5))
    backoff_seconds = max(0.0, resolved_settings.switch_bot_flow_retry_backoff_seconds)
    retryable_statuses = {408, 425, 429, 500, 502, 503, 504, 599}

    status_code = 599
    response_body = ""
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        req = request.Request(
            url=url,
            method="POST",
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:  # noqa: S310
                status_code = int(response.status)
                response_body = response.read().decode("utf-8", errors="replace")
                last_error = None
        except HTTPError as exc:
            status_code = int(exc.code)
            response_body = exc.read().decode("utf-8", errors="replace")
            last_error = exc
        except (URLError, TimeoutError) as exc:
            status_code = 599
            response_body = ""
            last_error = exc

        if 200 <= status_code < 300:
            try:
                parsed_body = json.loads(response_body) if response_body else {}
            except json.JSONDecodeError as exc:
                raise SwitchBotFlowError(
                    "switch_bot_flow_runner_invalid_response",
                    "Runner v5 aceitou o payload, mas devolveu JSON inválido.",
                    status_code=status_code,
                ) from exc
            if not isinstance(parsed_body, dict):
                raise SwitchBotFlowError(
                    "switch_bot_flow_runner_invalid_response",
                    "Runner v5 devolveu uma resposta incompatível.",
                    status_code=status_code,
                )
            return RunnerDeliveryResult(
                status_code=status_code,
                response_body=parsed_body,
                attempts=attempt,
            )

        if attempt >= max_attempts or status_code not in retryable_statuses:
            break
        if backoff_seconds > 0:
            time.sleep(backoff_seconds * attempt)

    raise SwitchBotFlowError(
        "switch_bot_flow_runner_delivery_failed",
        "Runner v5 não confirmou o recebimento do payload Meta.",
        status_code=status_code if status_code != 599 else None,
    ) from last_error
