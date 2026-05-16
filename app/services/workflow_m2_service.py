from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import posixpath
import re
import subprocess
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.workspace import get_current_workspace_schema, get_current_workspace_uuid
from app.repositories.flow_v2_repository import fetch_flow_row, fetch_selected_revision
from app.repositories.orch_channel_events_repository import claim_next_pending_channel_event, has_pending_channel_events
from app.repositories.orch_sessions_repository import (
    assign_whatsapp_routing_for_session,
    fetch_session_workflow_state,
    replace_session_workflow_state,
)
from app.repositories.workspaces_repository import fetch_workspace_otima_billing_api_key
from app.services.generate_file_dispatch_service import upsert_job_and_buffer_row
from app.services.otima_llm_service import execute_otima_llm_prompt
from app.services.session_metrics_service import persist_session_metrics
from app.services.workflow_engine import (
    component_kind,
    index_components,
    outgoing_branch_labels,
    resolve_next_card_uuid,
    resolve_next_card_uuid_by_branch,
)

_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
logger = get_logger(__name__)
WHATSAPP_BLOCKING_STOP_REASONS_BY_KIND = {
    "send_with_whatsapp": "blocked_send_with_whatsapp",
    "proccess_whatsapp_response": "blocked_process_whatsapp_response",
    "process_whatsapp_response": "blocked_process_whatsapp_response",
    "send_with_dialer": "blocked_send_with_dialer",
    "proccess_dialer_response": "blocked_process_dialer_response",
    "process_dialer_response": "blocked_process_dialer_response",
}

WHATSAPP_RESPONSE_BRANCH_BY_STATUS = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "limit_reached": "limit_reached",
}

WHATSAPP_BLOCKING_STOP_REASONS = {
    "blocked_send_with_whatsapp",
    "blocked_process_whatsapp_response",
}
WHATSAPP_STATUS_ORDER_PREREQUISITES = {
    "delivered": "whatsapp_sent_at",
    "read": "whatsapp_delivered_at",
}
WHATSAPP_STATUS_ORDER_TTL_SECONDS = {
    "delivered": 20,
    "read": 45,
}
WHATSAPP_STATUS_ORDER_RETRY_SECONDS = 3


@dataclass(frozen=True)
class WorkflowExecutionResult:
    enabled: bool
    executed_steps: int
    stopped_reason: str
    last_card_uuid: str | None
    next_card_uuid: str | None


class WorkflowExecutionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _blocking_stop_reason_for_component(kind: str) -> str | None:
    return WHATSAPP_BLOCKING_STOP_REASONS_BY_KIND.get(kind)


def _mark_blocking_execution(runtime_variables: dict[str, Any], *, stopped_reason: str) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    workflow_meta["blocking_execution"] = True
    workflow_meta["blocking_stop_reason"] = stopped_reason


def _clear_blocking_execution(runtime_variables: dict[str, Any]) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    workflow_meta["blocking_execution"] = False
    workflow_meta.pop("blocking_stop_reason", None)


def _read_enabled(settings: Any) -> bool:
    raw = getattr(settings, "workflow_v2_execute_m2", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _read_max_steps(settings: Any) -> int:
    raw = getattr(settings, "workflow_v2_max_steps", 25)
    try:
        value = int(raw)
    except Exception:
        value = 25
    return max(1, min(200, value))


def _to_uuid_or_none(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    try:
        return str(UUID(str(raw_value)))
    except Exception:
        return None


def _ensure_workflow_meta(runtime_variables: dict[str, Any]) -> dict[str, Any]:
    workflow_meta = runtime_variables.get("workflow_v2")
    if isinstance(workflow_meta, dict):
        return workflow_meta
    workflow_meta = {}
    runtime_variables["workflow_v2"] = workflow_meta
    return workflow_meta


def _read_next_cursor(runtime_variables: dict[str, Any]) -> str | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    raw = workflow_meta.get("next_card_cursor")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _read_blocking_stop_reason(runtime_variables: dict[str, Any]) -> str | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    if not workflow_meta.get("blocking_execution"):
        return None
    raw = workflow_meta.get("blocking_stop_reason")
    if raw is None:
        return "blocked_send_with_whatsapp"
    text = str(raw).strip()
    return text or "blocked_send_with_whatsapp"


def _set_cursors(runtime_variables: dict[str, Any], *, last_cursor: str | None, next_cursor: str | None) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    workflow_meta["last_card_cursor"] = last_cursor
    workflow_meta["next_card_cursor"] = next_cursor


def _read_whatsapp_resume_cursor(runtime_variables: dict[str, Any]) -> str | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    channel_resume = workflow_meta.get("channel_resume")
    if not isinstance(channel_resume, dict):
        return None
    whatsapp_resume = channel_resume.get("whatsapp")
    if not isinstance(whatsapp_resume, dict):
        return None
    raw = whatsapp_resume.get("process_card_cursor")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _set_whatsapp_resume_cursor(runtime_variables: dict[str, Any], *, process_card_cursor: str | None) -> None:
    if not process_card_cursor:
        return
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    channel_resume = workflow_meta.get("channel_resume")
    if not isinstance(channel_resume, dict):
        channel_resume = {}
        workflow_meta["channel_resume"] = channel_resume
    whatsapp_resume = channel_resume.get("whatsapp")
    if not isinstance(whatsapp_resume, dict):
        whatsapp_resume = {}
        channel_resume["whatsapp"] = whatsapp_resume
    whatsapp_resume["process_card_cursor"] = process_card_cursor


def _extract_whatsapp_status_signature_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("object") != "whatsapp_business_account":
        return None
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return None
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
            statuses = value.get("statuses")
            if not isinstance(statuses, list):
                continue
            for item in statuses:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", "")).strip().lower()
                if not status:
                    continue
                message_id = str(item.get("id", "")).strip()
                timestamp = str(item.get("timestamp", "")).strip()
                recipient_id = str(item.get("recipient_id", "")).strip()
                return f"{status}|{message_id}|{timestamp}|{recipient_id}"
    return None


def _extract_whatsapp_status_signature_from_runtime(runtime_variables: dict[str, Any]) -> str | None:
    if not isinstance(runtime_variables, dict):
        return None
    signature = _extract_whatsapp_status_signature_from_payload(runtime_variables.get("last_payload"))
    if signature is not None:
        return signature
    signature = _extract_whatsapp_status_signature_from_payload(runtime_variables.get("input_payload"))
    if signature is not None:
        return signature
    variables = runtime_variables.get("variables")
    if not isinstance(variables, dict):
        return None
    return _extract_whatsapp_status_signature_from_payload(variables.get("payload"))


def _read_whatsapp_last_preempt_signature(runtime_variables: dict[str, Any]) -> str | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    channel_resume = workflow_meta.get("channel_resume")
    if not isinstance(channel_resume, dict):
        return None
    whatsapp_resume = channel_resume.get("whatsapp")
    if not isinstance(whatsapp_resume, dict):
        return None
    raw = whatsapp_resume.get("last_preempt_signature")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _set_whatsapp_last_preempt_signature(runtime_variables: dict[str, Any], signature: str) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    channel_resume = workflow_meta.get("channel_resume")
    if not isinstance(channel_resume, dict):
        channel_resume = {}
        workflow_meta["channel_resume"] = channel_resume
    whatsapp_resume = channel_resume.get("whatsapp")
    if not isinstance(whatsapp_resume, dict):
        whatsapp_resume = {}
        channel_resume["whatsapp"] = whatsapp_resume
    whatsapp_resume["last_preempt_signature"] = signature


def _should_preempt_to_whatsapp_resume_cursor(
    runtime_variables: dict[str, Any],
    *,
    has_pending_whatsapp_events: bool = False,
    current_next_card_uuid: str | None = None,
    blocking_stop_reason: str | None = None,
) -> bool:
    resume_cursor = _read_whatsapp_resume_cursor(runtime_variables)
    if has_pending_whatsapp_events and resume_cursor is not None:
        if current_next_card_uuid is None:
            return True
        if str(current_next_card_uuid) == str(resume_cursor):
            return True
        if blocking_stop_reason in WHATSAPP_BLOCKING_STOP_REASONS:
            return True
        return False

    status = _extract_whatsapp_status_from_runtime(runtime_variables)
    if status not in WHATSAPP_RESPONSE_BRANCH_BY_STATUS:
        return False
    if resume_cursor is None:
        return False
    signature = _extract_whatsapp_status_signature_from_runtime(runtime_variables)
    if signature is None:
        return False
    return signature != _read_whatsapp_last_preempt_signature(runtime_variables)


def _extract_whatsapp_status_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("object") != "whatsapp_business_account":
        return None
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return None

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
            statuses = value.get("statuses")
            if not isinstance(statuses, list):
                continue
            for item in statuses:
                if not isinstance(item, dict):
                    continue
                raw = item.get("status")
                if raw is None:
                    continue
                text = str(raw).strip().lower()
                if text:
                    return text
    return None


def _extract_whatsapp_status_from_runtime(runtime_variables: dict[str, Any]) -> str | None:
    if not isinstance(runtime_variables, dict):
        return None

    status = _extract_whatsapp_status_from_payload(runtime_variables.get("last_payload"))
    if status is not None:
        return status

    status = _extract_whatsapp_status_from_payload(runtime_variables.get("input_payload"))
    if status is not None:
        return status

    variables = runtime_variables.get("variables")
    if not isinstance(variables, dict):
        return None
    return _extract_whatsapp_status_from_payload(variables.get("payload"))


def _run_process_whatsapp_response(
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str | None:
    status = _extract_whatsapp_status_from_runtime(runtime_variables)
    if status is None:
        return None
    branch = WHATSAPP_RESPONSE_BRANCH_BY_STATUS.get(status)

    runtime_variables["whatsapp_last_response"] = {
        "component_ref_id": component.get("ref_id"),
        "status": status,
        "branch": branch,
    }
    return branch


def _extract_send_with_whatsapp_numbers(component: dict[str, Any]) -> list[str]:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    config = params.get("whatsapp_numbers_config") if isinstance(params.get("whatsapp_numbers_config"), dict) else {}
    rows = config.get("numbers") if isinstance(config.get("numbers"), list) else []

    numbers: list[str] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        value = str(item.get("number") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        numbers.append(value)
    return numbers


async def _prepare_send_with_whatsapp_contact_member(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    session_id: int,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> None:
    numbers = _extract_send_with_whatsapp_numbers(component)
    assignment = await assign_whatsapp_routing_for_session(
        db_session,
        flow_uuid=flow_uuid,
        session_id=session_id,
        numbers=numbers,
    )
    runtime_variables["send_with_whatsapp_routing"] = {
        "numbers": numbers,
        "assignment": assignment,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _read_status_order_wait_deadline(
    runtime_variables: dict[str, Any],
    *,
    status: str,
) -> datetime | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    wait_meta = workflow_meta.get("whatsapp_status_order_wait")
    if not isinstance(wait_meta, dict):
        return None
    raw = wait_meta.get(status)
    return _parse_iso_datetime(raw)


def _write_status_order_wait_deadline(
    runtime_variables: dict[str, Any],
    *,
    status: str,
    deadline: datetime,
) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    wait_meta = workflow_meta.get("whatsapp_status_order_wait")
    if not isinstance(wait_meta, dict):
        wait_meta = {}
        workflow_meta["whatsapp_status_order_wait"] = wait_meta
    wait_meta[status] = deadline.isoformat()


def _clear_status_order_wait_deadline(
    runtime_variables: dict[str, Any],
    *,
    status: str,
) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    wait_meta = workflow_meta.get("whatsapp_status_order_wait")
    if not isinstance(wait_meta, dict):
        return
    wait_meta.pop(status, None)
    if not wait_meta:
        workflow_meta.pop("whatsapp_status_order_wait", None)


def _compute_whatsapp_status_order_delay(
    *,
    runtime_variables: dict[str, Any],
    session_state: dict[str, Any],
) -> datetime | None:
    status = _extract_whatsapp_status_from_runtime(runtime_variables)
    if status is None:
        return None
    prerequisite_field = WHATSAPP_STATUS_ORDER_PREREQUISITES.get(status)
    if prerequisite_field is None:
        return None

    if session_state.get(prerequisite_field) is not None:
        _clear_status_order_wait_deadline(runtime_variables, status=status)
        return None

    now_utc = datetime.now(timezone.utc)
    deadline = _read_status_order_wait_deadline(runtime_variables, status=status)
    if deadline is None:
        ttl = WHATSAPP_STATUS_ORDER_TTL_SECONDS.get(status, 20)
        deadline = now_utc + timedelta(seconds=max(1, ttl))
        _write_status_order_wait_deadline(runtime_variables, status=status, deadline=deadline)

    if now_utc >= deadline:
        _clear_status_order_wait_deadline(runtime_variables, status=status)
        return None

    return now_utc + timedelta(seconds=WHATSAPP_STATUS_ORDER_RETRY_SECONDS)


def _should_resume_whatsapp_blocking_execution(runtime_variables: dict[str, Any]) -> bool:
    blocking_stop_reason = _read_blocking_stop_reason(runtime_variables)
    if blocking_stop_reason not in WHATSAPP_BLOCKING_STOP_REASONS:
        return False
    return _extract_whatsapp_status_from_runtime(runtime_variables) is not None


def _get_by_dot_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for segment in path.split("."):
        key = segment.strip()
        if not key:
            return None
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _render_value(template: Any, variables: dict[str, Any]) -> Any:
    if isinstance(template, str):
        matches = list(_TEMPLATE_PATTERN.finditer(template))
        if not matches:
            return template

        if len(matches) == 1 and matches[0].span() == (0, len(template)):
            token = matches[0].group(1).strip()
            resolved = _get_by_dot_path(variables, token)
            if resolved is None:
                customs = variables.get("customs") if isinstance(variables.get("customs"), dict) else {}
                payload = variables.get("payload") if isinstance(variables.get("payload"), dict) else {}
                resolved = _get_by_dot_path(customs, token)
                if resolved is None:
                    resolved = _get_by_dot_path(payload, token)
            return resolved

        rendered = template
        for match in matches:
            token = match.group(1).strip()
            value = _get_by_dot_path(variables, token)
            if value is None:
                customs = variables.get("customs") if isinstance(variables.get("customs"), dict) else {}
                payload = variables.get("payload") if isinstance(variables.get("payload"), dict) else {}
                value = _get_by_dot_path(customs, token)
                if value is None:
                    value = _get_by_dot_path(payload, token)
            rendered = rendered.replace(match.group(0), "" if value is None else str(value))
        return rendered

    if isinstance(template, dict):
        return {key: _render_value(value, variables) for key, value in template.items()}
    if isinstance(template, list):
        return [_render_value(item, variables) for item in template]
    return template


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ensure_variables(runtime_variables: dict[str, Any]) -> dict[str, Any]:
    variables = runtime_variables.get("variables")
    if isinstance(variables, dict):
        payload = runtime_variables.get("input_payload")
        if isinstance(payload, dict) and not isinstance(variables.get("payload"), dict):
            variables["payload"] = dict(payload)
        customs = variables.get("customs")
        if not isinstance(customs, dict):
            seed = variables.get("payload")
            variables["customs"] = dict(seed) if isinstance(seed, dict) else {}
        return variables

    input_payload = runtime_variables.get("input_payload")
    if isinstance(input_payload, dict):
        runtime_variables["variables"] = {
            "payload": dict(input_payload),
            "customs": dict(input_payload),
            **dict(input_payload),
        }
    else:
        runtime_variables["variables"] = {"payload": {}, "customs": {}}
    return runtime_variables["variables"]


def _set_by_path(root: dict[str, Any], path: str, value: Any) -> None:
    parts = [part for part in path.split(".") if part]
    if not parts:
        return

    current = root
    for part in parts[:-1]:
        existing = current.get(part)
        if not isinstance(existing, dict):
            existing = {}
            current[part] = existing
        current = existing
    current[parts[-1]] = value


def _run_set_variables(component: dict[str, Any], runtime_variables: dict[str, Any]) -> None:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    instructions = params.get("instructions") if isinstance(params.get("instructions"), list) else []
    variables = _ensure_variables(runtime_variables)

    for item in instructions:
        if not isinstance(item, dict):
            continue
        variable_path = str(item.get("variable") or item.get("key") or "").strip()
        if not variable_path:
            continue

        source_type = str(item.get("source_type") or "").strip().lower()
        if source_type == "variable" and isinstance(item.get("value"), str):
            raw_path = str(item.get("value")).strip()
            value = _get_by_dot_path(variables, raw_path)
            if value is None:
                value = _get_by_dot_path(runtime_variables.get("input_payload") if isinstance(runtime_variables.get("input_payload"), dict) else {}, raw_path)
        else:
            value = _render_value(item.get("value"), variables)

        if variable_path.startswith("variables."):
            _set_by_path(variables, variable_path[len("variables.") :], value)
        elif variable_path.startswith("customs."):
            customs = variables.get("customs")
            if not isinstance(customs, dict):
                customs = {}
                variables["customs"] = customs
            _set_by_path(customs, variable_path[len("customs.") :], value)
        else:
            customs = variables.get("customs")
            if not isinstance(customs, dict):
                customs = {}
                variables["customs"] = customs
            _set_by_path(customs, variable_path, value)


def _compare_values(left: Any, op: str, right: Any) -> bool:
    if op in {"eq", "==", "equals", "is", "equal"}:
        if left == right:
            return True
        try:
            return float(left) == float(right)
        except Exception:
            return False
    if op in {"ne", "!=", "not_equals", "is_not"}:
        if left != right:
            try:
                return float(left) != float(right)
            except Exception:
                return True
        return False

    try:
        left_num = float(left)
        right_num = float(right)
    except Exception:
        left_num = None
        right_num = None

    if op in {"gt", ">", "greater", "greater_than"}:
        return left_num is not None and right_num is not None and left_num > right_num
    if op in {"gte", ">=", "greater_or_equal"}:
        return left_num is not None and right_num is not None and left_num >= right_num
    if op in {"lt", "<", "less", "less_than"}:
        return left_num is not None and right_num is not None and left_num < right_num
    if op in {"lte", "<=", "less_or_equal"}:
        return left_num is not None and right_num is not None and left_num <= right_num

    if op in {"contains"}:
        return str(right) in str(left)

    return left == right


def _run_condition(component: dict[str, Any], runtime_variables: dict[str, Any]) -> str:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    conditions = params.get("conditions") if isinstance(params.get("conditions"), list) else []
    variables = _ensure_variables(runtime_variables)

    for condition in conditions:
        if not isinstance(condition, dict):
            continue

        rules = condition.get("rules") if isinstance(condition.get("rules"), list) else []
        match_mode = str(condition.get("match") or "all").strip().lower()

        results: list[bool] = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            field = str(rule.get("field") or "").strip()
            op = str(rule.get("operator") or rule.get("op") or "eq").strip().lower()
            expected = _render_value(rule.get("value"), variables)
            actual = None
            if field:
                if "{{" in field and "}}" in field:
                    actual = _render_value(field, variables)
                else:
                    actual = _get_by_dot_path(variables, field)
                    if actual is None:
                        customs = variables.get("customs") if isinstance(variables.get("customs"), dict) else {}
                        payload = variables.get("payload") if isinstance(variables.get("payload"), dict) else {}
                        actual = _get_by_dot_path(customs, field)
                        if actual is None:
                            actual = _get_by_dot_path(payload, field)
            results.append(_compare_values(actual, op, expected))

        if not results:
            continue

        matched = all(results) if match_mode == "all" else any(results)
        if matched:
            branch = (
                condition.get("id")
                or condition.get("branch")
                or condition.get("name")
                or condition.get("label")
                or "true"
            )
            return str(branch).strip().lower() or "true"

    return "false"


def _read_int(values: list[Any]) -> int | None:
    for raw in values:
        if raw is None:
            continue
        try:
            return int(str(raw).strip())
        except Exception:
            continue
    return None


def _compute_frozen_until(component: dict[str, Any], runtime_variables: dict[str, Any]) -> datetime:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    variables = _ensure_variables(runtime_variables)

    direct_until = _render_value(
        params.get("resume_at")
        or params.get("until")
        or params.get("frozen_until")
        or params.get("datetime"),
        variables,
    )
    parsed_direct = _parse_iso_datetime(direct_until)
    if parsed_direct is not None:
        return parsed_direct

    wait_ms = _read_int(
        [
            _render_value(params.get("tempo_ms"), variables),
            _render_value(params.get("wait_ms"), variables),
            _render_value(params.get("milliseconds"), variables),
            _render_value(params.get("ms"), variables),
            _render_value(params.get("tempo"), variables),
        ]
    )
    wait_seconds = _read_int(
        [
            _render_value(params.get("delay_in_seconds"), variables),
            _render_value(params.get("seconds"), variables),
            _render_value(params.get("tempo_seconds"), variables),
        ]
    )
    wait_minutes = _read_int(
        [
            _render_value(params.get("minutes"), variables),
            _render_value(params.get("tempo_minutes"), variables),
        ]
    )

    total_ms = 0
    if wait_ms is not None:
        total_ms += max(wait_ms, 0)
    if wait_seconds is not None:
        total_ms += max(wait_seconds, 0) * 1000
    if wait_minutes is not None:
        total_ms += max(wait_minutes, 0) * 60 * 1000

    return datetime.now(timezone.utc) + timedelta(milliseconds=max(total_ms, 0))


def _coerce_timeout_ms(raw_value: Any, default: int = 400) -> int:
    try:
        parsed = int(str(raw_value).strip())
    except Exception:
        parsed = default
    return max(100, min(10_000, parsed))


def _unwrap_option(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("id", "value", "name", "label"):
            if key in value and value.get(key) is not None:
                return value.get(key)
    if isinstance(value, list) and value:
        return _unwrap_option(value[0])
    return value


def _normalize_delimiter(value: Any) -> str:
    raw = str(_unwrap_option(value) or "").strip().lower()
    if raw in {"", "pipe", "|"}:
        return "|"
    if raw in {"virgula", "vírgula", ",", "comma"}:
        return ","
    if raw in {"ponto e virgula", "ponto-e-virgula", ";", "semicolon"}:
        return ";"
    if raw in {"tab", "\\t", "t"}:
        return "\t"
    return str(_unwrap_option(value) or "|")


def _normalize_line_break(value: Any) -> str:
    raw = str(_unwrap_option(value) or "").strip().upper()
    if raw in {"CRLF", "WINDOWS"}:
        return "\r\n"
    return "\n"


def _ensure_file_extension(file_name: str, format_type: str) -> str:
    if "." in Path(file_name).name:
        return file_name
    extension_map = {
        "csv": ".csv",
        "json": ".json",
        "jsonl": ".jsonl",
        "txt": ".txt",
    }
    suffix = extension_map.get(format_type, "")
    return f"{file_name}{suffix}" if suffix else file_name


def _append_session_suffix(file_name: str, session_id: int) -> str:
    token = str(session_id).strip()
    if "." not in file_name:
        return f"{file_name}-{token}"
    stem, suffix = file_name.rsplit(".", 1)
    return f"{stem}-{token}.{suffix}"


def _safe_relpath(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    raw = raw.lstrip("/")
    normalized = posixpath.normpath(raw)
    if normalized in {"", "."}:
        return ""
    if normalized.startswith("..") or "/../" in f"/{normalized}/":
        raise WorkflowExecutionError("generate_file_invalid_destination_path", "destination_path inválido.")
    return normalized


def _safe_filename(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise WorkflowExecutionError("generate_file_missing_file_name", "Nome do arquivo não informado.")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise WorkflowExecutionError("generate_file_invalid_file_name", "Nome do arquivo inválido.")
    return name


def _resolve_secret_reference(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    token = raw
    if raw.startswith("{{") and raw.endswith("}}"):
        token = raw[2:-2].strip()
    if token.lower().startswith("env."):
        env_name = token[4:].strip()
        return os.getenv(env_name, "").strip() or raw
    if token.lower().startswith("env:"):
        env_name = token[4:].strip()
        return os.getenv(env_name, "").strip() or raw
    return os.getenv(token, "").strip() or raw


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    token = str(_unwrap_option(value)).strip().lower()
    if token in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if token in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _normalize_generate_file_mapping(raw_mapping: Any) -> list[dict[str, str]]:
    rows: list[dict[str, Any]] = []
    if isinstance(raw_mapping, dict):
        for key in ("items", "rows", "value", "mapping"):
            candidate = raw_mapping.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
    elif isinstance(raw_mapping, list):
        rows = raw_mapping

    normalized: list[dict[str, str]] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        column = str(
            entry.get("column")
            or entry.get("header_name")
            or entry.get("name")
            or entry.get("key")
            or ""
        ).strip()
        source = str(
            entry.get("source")
            or entry.get("variable")
            or entry.get("path")
            or entry.get("value")
            or entry.get("source_value")
            or ""
        ).strip()
        data_type = str(
            entry.get("data_type")
            or entry.get("type")
            or entry.get("field_type")
            or "text"
        ).strip().lower()
        if column and source:
            normalized.append({"column": column, "source": source, "data_type": data_type})
    return normalized


def _coerce_generate_file_value(value: Any, data_type: str) -> Any:
    normalized = (data_type or "text").strip().lower()
    if normalized in {"text", "string", ""}:
        return "" if value is None else str(value)
    if normalized in {"number", "float", "double", "decimal"}:
        try:
            return float(str(value).replace(".", "").replace(",", "."))
        except Exception:
            return "" if value is None else str(value)
    if normalized in {"integer", "int"}:
        try:
            return int(float(str(value).replace(".", "").replace(",", ".")))
        except Exception:
            return "" if value is None else str(value)
    if normalized in {"bool", "boolean"}:
        return _coerce_bool(value)
    if normalized in {"json", "object"}:
        if isinstance(value, (dict, list)):
            return value
        text = "" if value is None else str(value).strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            return {"value": text}
    return "" if value is None else str(value)


def _resolve_generate_file_source(source: str, variables: dict[str, Any]) -> Any:
    candidate = source.strip()
    if not candidate:
        return ""
    if "{{" in candidate and "}}" in candidate:
        return _render_value(candidate, variables)
    resolved = _get_by_dot_path(variables, candidate)
    if resolved is not None:
        return resolved
    customs = variables.get("customs") if isinstance(variables.get("customs"), dict) else {}
    payload = variables.get("payload") if isinstance(variables.get("payload"), dict) else {}
    resolved = _get_by_dot_path(customs, candidate)
    if resolved is not None:
        return resolved
    resolved = _get_by_dot_path(payload, candidate)
    if resolved is not None:
        return resolved
    return candidate


def _build_runtime_resolution_scope(
    *,
    runtime_variables: dict[str, Any],
    variables: dict[str, Any],
) -> dict[str, Any]:
    payload = variables.get("payload") if isinstance(variables.get("payload"), dict) else {}
    customs = variables.get("customs") if isinstance(variables.get("customs"), dict) else {}

    scope: dict[str, Any] = dict(variables)
    if isinstance(payload, dict):
        for key, value in payload.items():
            scope.setdefault(key, value)
        scope["payload"] = payload
    if isinstance(customs, dict):
        for key, value in customs.items():
            scope[key] = value
        scope["customs"] = customs

    api_last_result = runtime_variables.get("api_call_last_result")
    if isinstance(api_last_result, dict):
        api_body = api_last_result.get("body")
        if isinstance(api_body, dict):
            scope.setdefault("api_body", api_body)
            if isinstance(customs, dict):
                customs.setdefault("api_body", api_body)

    return scope


def _build_generate_file_resolution_scope(
    *,
    runtime_variables: dict[str, Any],
    variables: dict[str, Any],
) -> dict[str, Any]:
    return _build_runtime_resolution_scope(
        runtime_variables=runtime_variables,
        variables=variables,
    )


def _serialize_generate_file_rows(
    *,
    rows: list[dict[str, Any]],
    format_type: str,
    delimiter: str,
    include_header: bool,
    line_break: str,
) -> tuple[str, int]:
    if format_type == "csv":
        field_names = list(rows[0].keys()) if rows else []
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=field_names, delimiter=delimiter, lineterminator=line_break)
        if include_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if v is None else str(v) for k, v in row.items()})
        text_value = buffer.getvalue()
        return text_value, len(text_value.splitlines()) if text_value else 0

    if format_type == "json":
        text_value = json.dumps(rows, ensure_ascii=False)
        return text_value, len(text_value.splitlines()) if text_value else 0

    if format_type == "jsonl":
        lines = [json.dumps(row, ensure_ascii=False) for row in rows]
        text_value = line_break.join(lines)
        if text_value:
            text_value += line_break
        return text_value, len(text_value.splitlines()) if text_value else 0

    if format_type == "txt":
        lines: list[str] = []
        for row in rows:
            values = ["" if value is None else str(value) for value in row.values()]
            lines.append(delimiter.join(values))
        text_value = line_break.join(lines)
        if text_value:
            text_value += line_break
        return text_value, len(text_value.splitlines()) if text_value else 0

    raise WorkflowExecutionError("generate_file_invalid_format", "Formato de arquivo não suportado.")


def _gzip_if_needed(*, payload: bytes, compression: str, file_name: str) -> tuple[bytes, str]:
    if compression == "none":
        return payload, file_name
    if compression == "gzip":
        output_name = file_name if file_name.endswith(".gz") else f"{file_name}.gz"
        return gzip.compress(payload), output_name
    raise WorkflowExecutionError("generate_file_invalid_compression", "Compressão não suportada.")


def _resolve_renamed_filename(*, existing_names: set[str], original_name: str) -> str:
    if original_name not in existing_names:
        return original_name
    stem = original_name
    suffix = ""
    if "." in original_name:
        stem, suffix = original_name.rsplit(".", 1)
        suffix = f".{suffix}"
    for idx in range(2, 10_000):
        candidate = f"{stem}_{idx}{suffix}"
        if candidate not in existing_names:
            return candidate
    raise WorkflowExecutionError("generate_file_rename_failed", "Não foi possível gerar nome alternativo.")


def _sftp_upload_bytes(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    destination_path: str,
    file_name: str,
    write_mode: str,
    if_exists_policy: str,
    payload: bytes,
    encoding: str,
    line_break: str,
) -> dict[str, Any]:
    try:
        import paramiko
    except Exception as exc:
        raise WorkflowExecutionError(
            "generate_file_missing_dependency",
            "Dependência paramiko ausente para upload SFTP.",
        ) from exc

    transport = paramiko.Transport((host, int(port)))
    try:
        transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            remote_dir = _safe_relpath(destination_path)
            if remote_dir:
                current = ""
                for chunk in [part for part in remote_dir.split("/") if part]:
                    current = f"{current}/{chunk}" if current else f"/{chunk}"
                    try:
                        sftp.stat(current)
                    except Exception:
                        sftp.mkdir(current)
            base = f"/{remote_dir}" if remote_dir else ""
            remote_file = f"{base}/{file_name}" if base else f"/{file_name}"

            existing_names: set[str] = set()
            if base:
                try:
                    existing_names = set(sftp.listdir(base))
                except Exception:
                    existing_names = set()

            exists_before = file_name in existing_names

            target_name = file_name
            if exists_before and if_exists_policy == "fail" and write_mode != "append":
                raise WorkflowExecutionError("generate_file_file_exists", "Arquivo já existe no destino.")
            if exists_before and if_exists_policy == "rename" and write_mode != "append":
                target_name = _resolve_renamed_filename(existing_names=existing_names, original_name=file_name)
                remote_file = f"{base}/{target_name}" if base else f"/{target_name}"

            if write_mode in {"create", "create_per_session"} and exists_before and target_name == file_name:
                raise WorkflowExecutionError("generate_file_create_exists", "Arquivo já existe para write_mode=create.")

            if write_mode == "append":
                if exists_before and target_name == file_name:
                    with sftp.open(remote_file, "rb") as remote_reader:
                        previous = remote_reader.read()
                    previous_text = previous.decode(encoding, errors="ignore")
                    append_text = payload.decode(encoding, errors="ignore")
                    if previous_text and append_text and not previous_text.endswith(line_break):
                        append_text = f"{line_break}{append_text}"
                    payload = append_text.encode(encoding)
                    with sftp.open(remote_file, "ab") as remote_writer:
                        remote_writer.write(payload)
                else:
                    with sftp.open(remote_file, "wb") as remote_writer:
                        remote_writer.write(payload)
            else:
                with sftp.open(remote_file, "wb") as remote_writer:
                    remote_writer.write(payload)

            return {
                "file_name": target_name,
                "remote_path": remote_file,
            }
        finally:
            sftp.close()
    finally:
        transport.close()


async def _run_generate_file(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
    session_id: int,
) -> str:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    variables = _ensure_variables(runtime_variables)
    resolution_scope = _build_generate_file_resolution_scope(
        runtime_variables=runtime_variables,
        variables=variables,
    )

    raw_mapping = params.get("fields_mapping") or params.get("mapping") or []
    mapping_fields = _normalize_generate_file_mapping(raw_mapping)
    if not mapping_fields:
        raise WorkflowExecutionError("generate_file_missing_mapping", "Nenhum campo de mapeamento configurado.")

    destination_type = str(_unwrap_option(params.get("destination_type") or "sftp")).strip().lower()
    if destination_type not in {"local", "sftp"}:
        raise WorkflowExecutionError("generate_file_invalid_destination", "destination_type deve ser local ou sftp.")

    format_type = str(_unwrap_option(params.get("format_type") or params.get("file_type") or "csv")).strip().lower()
    if format_type not in {"csv", "json", "jsonl", "txt"}:
        raise WorkflowExecutionError("generate_file_invalid_format", "Formato de arquivo não suportado.")

    encoding = str(_unwrap_option(params.get("encoding") or "utf-8")).strip().lower()
    if encoding in {"utf8", "utf-8"}:
        encoding = "utf-8"
    elif encoding in {"latin1", "iso-8859-1"}:
        encoding = "iso-8859-1"
    elif encoding in {"windows-1252", "cp1252"}:
        encoding = "cp1252"
    elif encoding in {"utf-8 bom", "utf8 bom", "utf-8-sig"}:
        encoding = "utf-8-sig"
    else:
        raise WorkflowExecutionError("generate_file_invalid_encoding", "Codificação não suportada.")

    write_mode = str(_unwrap_option(params.get("write_mode") or "create")).strip().lower()
    if write_mode not in {"create", "overwrite", "append", "create_per_session"}:
        raise WorkflowExecutionError("generate_file_invalid_write_mode", "write_mode inválido.")

    scheduling_mode = str(_unwrap_option(params.get("scheduling_run_mode") or "imediato")).strip().lower()
    if scheduling_mode == "imediato":
        write_mode = "create_per_session"

    include_header = _coerce_bool(params.get("include_header"), default=True)
    delimiter = _normalize_delimiter(params.get("delimiter") or params.get("separator"))
    line_break = _normalize_line_break(params.get("line_break"))
    compression = str(_unwrap_option(params.get("compression") or "none")).strip().lower()
    if compression in {"", "none", "nenhuma"}:
        compression = "none"
    elif compression in {"gzip", "gz"}:
        compression = "gzip"
    else:
        raise WorkflowExecutionError("generate_file_invalid_compression", "Compressão não suportada.")

    file_name_raw = _render_value(params.get("file_name_template") or params.get("file_name") or "", resolution_scope)
    file_name = _safe_filename("" if file_name_raw is None else str(file_name_raw))
    file_name = _ensure_file_extension(file_name, format_type)
    if write_mode == "create_per_session":
        file_name = _append_session_suffix(file_name, session_id)

    destination_path = _safe_relpath(
        ""
        if _render_value(params.get("destination_path") or "", resolution_scope) is None
        else str(_render_value(params.get("destination_path") or "", resolution_scope))
    )
    if_exists_policy = str(_unwrap_option(params.get("if_exists_policy") or "replace")).strip().lower()
    if if_exists_policy not in {"replace", "fail", "rename"}:
        if_exists_policy = "replace"

    row: dict[str, Any] = {}
    for field in mapping_fields:
        raw_value = _resolve_generate_file_source(field["source"], resolution_scope)
        row[field["column"]] = _coerce_generate_file_value(raw_value, field["data_type"])

    file_text, lines_written = _serialize_generate_file_rows(
        rows=[row],
        format_type=format_type,
        delimiter=delimiter,
        include_header=include_header,
        line_break=line_break,
    )
    host = str(_render_value(params.get("host"), resolution_scope) or "").strip() or None
    port = _coerce_int(_render_value(params.get("port"), resolution_scope), 22)
    username = str(_render_value(params.get("user") or params.get("username"), resolution_scope) or "").strip() or None
    password = _resolve_secret_reference(_render_value(params.get("password"), resolution_scope))
    if destination_type == "sftp" and (not host or not username or not password):
        raise WorkflowExecutionError("generate_file_missing_sftp_credentials", "Credenciais SFTP incompletas.")

    output_name = file_name if compression == "none" else (file_name if file_name.endswith(".gz") else f"{file_name}.gz")
    workspace_uuid = get_current_workspace_uuid()
    component_ref_id = str(component.get("ref_id") or component.get("uuid") or output_name)

    config = {
        "destination_type": destination_type,
        "destination_path": destination_path,
        "file_name": output_name,
        "if_exists_policy": if_exists_policy,
        "sftp_host": host,
        "sftp_port": port,
        "sftp_user": username,
        "sftp_password": password,
        "format_type": format_type,
        "encoding": encoding,
        "write_mode": write_mode,
        "include_header": include_header,
        "delimiter": delimiter,
        "line_break": line_break,
        "compression": compression,
        "scheduling_run_mode": scheduling_mode,
        "scheduling_date": _unwrap_option(params.get("scheduling_date")),
        "scheduling_time_agendado": _unwrap_option(params.get("scheduling_time_agendado")),
        "scheduling_fuso_agandado": _unwrap_option(params.get("scheduling_fuso_agandado")) or "sp_utc_3",
        "recurrence": _unwrap_option(params.get("recurrence")),
        "scheduling_fuso_recorrente": _unwrap_option(params.get("scheduling_fuso_recorrente")) or "sp_utc_3",
        "scheduling_time": _unwrap_option(params.get("scheduling_time")),
    }
    enqueue_result = await upsert_job_and_buffer_row(
        db_session,
        workspace_uuid=workspace_uuid,
        flow_id=flow_uuid,
        component_ref_id=component_ref_id,
        session_id=session_id,
        config=config,
        row_payload=row,
    )

    if scheduling_mode == "imediato":
        try:
            from app.tasks.generate_file_tasks import generate_file_run_task

            generate_file_run_task.delay(workspace_uuid=workspace_uuid, job_id=str(enqueue_result["job_id"]))
        except Exception as exc:
            raise WorkflowExecutionError(
                "generate_file_dispatch_enqueue_failed",
                f"Falha ao enfileirar generate_file.run: {exc}",
            ) from exc

    result_payload: dict[str, Any] = {
        "status": "queued",
        "destination_type": destination_type,
        "format_type": format_type,
        "write_mode": write_mode,
        "lines_written": lines_written,
        "queued_row": bool(enqueue_result.get("queued_row")),
        "job_id": enqueue_result.get("job_id"),
        "next_run_at": enqueue_result.get("next_run_at"),
        "mode": enqueue_result.get("mode"),
        "remote_path": None,
        "file_name": output_name,
        "size_bytes": len(file_text.encode(encoding)),
        "md5": None,
    }
    runtime_variables["generate_file_last_result"] = result_payload

    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs
    output_var_prefix = str(params.get("output_var_prefix") or "arquivo").strip() or "arquivo"
    customs[output_var_prefix] = result_payload

    response_cfg = params.get("response") if isinstance(params.get("response"), dict) else {}
    status_var = response_cfg.get("status")
    path_var = response_cfg.get("path")
    file_var = response_cfg.get("file_name")
    md5_var = response_cfg.get("md5")
    error_var = response_cfg.get("error")
    if isinstance(status_var, str) and status_var.strip():
        customs[status_var.strip()] = "queued"
    if isinstance(path_var, str) and path_var.strip():
        customs[path_var.strip()] = result_payload.get("remote_path")
    if isinstance(file_var, str) and file_var.strip():
        customs[file_var.strip()] = result_payload.get("file_name")
    if isinstance(md5_var, str) and md5_var.strip():
        customs[md5_var.strip()] = None
    if isinstance(error_var, str) and error_var.strip():
        customs[error_var.strip()] = None

    return "success"


def _run_code_editor(
    *,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
    branch_labels: list[str],
) -> str | None:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    code = params.get("code")
    if not isinstance(code, str) or not code.strip():
        raise WorkflowExecutionError("code_editor_missing_code", "Componente code_editor sem código válido.")

    variables = _ensure_variables(runtime_variables)
    timeout_ms = _coerce_timeout_ms(params.get("timeout_ms"), default=400)

    node_payload = {
        "code": code,
        "timeoutMs": timeout_ms,
        "variables": variables,
        "branches": {label: label for label in branch_labels},
    }

    runner_js = textwrap.dedent(
        """
        const fs = require('node:fs');
        const vm = require('node:vm');

        function normalizeCode(src) {
          return String(src || '')
            .replace(/export\\s+default\\s+async\\s+function\\s+main/g, 'async function main')
            .replace(/export\\s+default\\s+function\\s+main/g, 'function main');
        }

        async function run() {
          const inputRaw = fs.readFileSync(0, 'utf8');
          const input = JSON.parse(inputRaw);
          const code = normalizeCode(input.code);
          const sandbox = {
            console: { log: () => {}, error: () => {}, warn: () => {} },
            ctx: {
              variables: input.variables || {},
              branches: input.branches || {}
            }
          };

          vm.createContext(sandbox);
          const wrapped = `${code}\\n;globalThis.__orch_main=(typeof main==='function'?main:null);`;
          vm.runInContext(wrapped, sandbox, { timeout: input.timeoutMs || 400 });
          if (typeof sandbox.__orch_main !== 'function') {
            throw new Error('main_not_found');
          }

          const result = await Promise.resolve(sandbox.__orch_main(sandbox.ctx));
          process.stdout.write(JSON.stringify({ result, ctx: sandbox.ctx }));
        }

        run().catch((err) => {
          process.stderr.write(String(err && err.stack ? err.stack : err));
          process.exit(1);
        });
        """
    )

    completed = subprocess.run(
        ["node", "-e", runner_js],
        input=json.dumps(node_payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=(timeout_ms / 1000.0) + 0.5,
        check=False,
    )

    if completed.returncode != 0:
        raise WorkflowExecutionError("code_editor_runtime_error", completed.stderr.strip() or "Falha na execução JS.")

    try:
        parsed = json.loads(completed.stdout or "{}")
    except Exception as exc:
        raise WorkflowExecutionError("code_editor_invalid_output", "Saída inválida do code_editor.") from exc

    ctx = parsed.get("ctx") if isinstance(parsed.get("ctx"), dict) else {}
    result = parsed.get("result")

    ctx_variables = ctx.get("variables")
    if isinstance(ctx_variables, dict):
        runtime_variables["variables"] = ctx_variables

    runtime_variables["code_editor_last_result"] = result

    if isinstance(result, dict) and result.get("payload") is not None:
        runtime_variables["code_editor_last_payload"] = result.get("payload")

    branch = None
    if isinstance(result, dict):
        branch = result.get("branch") or result.get("next_branch")
    elif isinstance(result, str):
        branch = result
    elif isinstance(result, bool):
        branch = "true" if result else "false"

    if branch is None:
        return None
    text_branch = str(branch).strip().lower()
    return text_branch or None


def _parse_headers(raw_headers: Any, variables: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    if isinstance(raw_headers, dict):
        for key, value in raw_headers.items():
            if key is None:
                continue
            rendered = _render_value(value, variables)
            headers[str(key)] = "" if rendered is None else str(rendered)
        return headers
    if isinstance(raw_headers, list):
        for item in raw_headers:
            if not isinstance(item, dict):
                continue
            if item.get("enabled") is False:
                continue
            key = item.get("key") or item.get("name")
            if key is None or not str(key).strip():
                continue
            value = _render_value(item.get("value"), variables)
            headers[str(key).strip()] = "" if value is None else str(value)
    return headers


def _parse_query(raw_query: Any, variables: dict[str, Any]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    if not isinstance(raw_query, list):
        return items
    for entry in raw_query:
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            continue
        key = entry.get("key") or entry.get("name")
        if key is None or not str(key).strip():
            continue
        value = _render_value(entry.get("value"), variables)
        items.append((str(key).strip(), "" if value is None else str(value)))
    return items


def _resolve_body(request_config: dict[str, Any], variables: dict[str, Any]) -> tuple[bytes | None, str | None]:
    body = request_config.get("body") if isinstance(request_config.get("body"), dict) else {}
    mode = str(body.get("mode") or "json").strip().lower()
    if mode == "json":
        raw_json = body.get("json")
        rendered = _render_value(raw_json, variables)
        if rendered is None:
            return b"null", "application/json"
        if isinstance(rendered, (dict, list, int, float, bool)):
            return json.dumps(rendered, ensure_ascii=False).encode("utf-8"), "application/json"
        text_value = str(rendered).strip()
        if not text_value:
            return b"{}", "application/json"
        try:
            parsed = json.loads(text_value)
            return json.dumps(parsed, ensure_ascii=False).encode("utf-8"), "application/json"
        except Exception:
            return json.dumps({"value": text_value}, ensure_ascii=False).encode("utf-8"), "application/json"

    if mode == "text":
        rendered = _render_value(body.get("text"), variables)
        return ("" if rendered is None else str(rendered)).encode("utf-8"), "text/plain"

    if mode == "form":
        form_entries = body.get("form") if isinstance(body.get("form"), list) else []
        data: list[tuple[str, str]] = []
        for entry in form_entries:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key") or entry.get("name")
            if key is None or not str(key).strip():
                continue
            value = _render_value(entry.get("value"), variables)
            data.append((str(key).strip(), "" if value is None else str(value)))
        return parse.urlencode(data).encode("utf-8"), "application/x-www-form-urlencoded"

    return None, None


def _http_execute(req: request.Request, timeout_seconds: float) -> tuple[int, dict[str, str], str, str | None]:
    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), dict(resp.headers.items()), body, None
    except HTTPError as err:
        body = err.read().decode("utf-8", errors="replace") if hasattr(err, "read") else str(err)
        return int(getattr(err, "code", 500) or 500), dict(getattr(err, "headers", {}).items()), body, str(err)
    except URLError as err:
        return 599, {}, "", str(err)


def _run_api_call(
    *,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    request_cfg = params.get("request") if isinstance(params.get("request"), dict) else {}
    variables = _ensure_variables(runtime_variables)

    method = str(request_cfg.get("method") or "GET").strip().upper()
    raw_url = _render_value(request_cfg.get("url"), variables)
    url = "" if raw_url is None else str(raw_url).strip()
    if not url:
        raise WorkflowExecutionError("api_call_missing_url", "api_call sem URL válida.")

    query_items = _parse_query(request_cfg.get("query"), variables)
    if query_items:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{parse.urlencode(query_items)}"

    body_bytes, content_type = _resolve_body(request_cfg, variables)
    headers = _parse_headers(request_cfg.get("headers"), variables)
    if content_type and "Content-Type" not in {k.title(): v for k, v in headers.items()}:
        headers.setdefault("Content-Type", content_type)

    timeout_ms = _coerce_timeout_ms(request_cfg.get("timeout"), default=30_000)
    retry_cfg = request_cfg.get("retry") if isinstance(request_cfg.get("retry"), dict) else {}
    max_attempts = _read_int([retry_cfg.get("max_attempts"), retry_cfg.get("attempts"), retry_cfg.get("max")]) or 1
    max_attempts = max(1, min(max_attempts, 5))
    backoff_ms = _read_int([retry_cfg.get("backoff_ms"), retry_cfg.get("delay_ms"), retry_cfg.get("wait_ms")]) or 0
    backoff_ms = max(0, min(backoff_ms, 5_000))

    retry_on_statuses = retry_cfg.get("on_statuses")
    retry_status_set: set[int] = set()
    if isinstance(retry_on_statuses, list):
        for status_code in retry_on_statuses:
            try:
                retry_status_set.add(int(status_code))
            except Exception:
                continue
    if not retry_status_set:
        retry_status_set = {408, 425, 429, 500, 502, 503, 504, 599}

    attempt = 0
    status_code = 599
    resp_headers: dict[str, str] = {}
    resp_body = ""
    error_msg: str | None = None

    while attempt < max_attempts:
        attempt += 1
        req = request.Request(url=url, method=method, data=body_bytes, headers=headers)
        status_code, resp_headers, resp_body, error_msg = _http_execute(req, timeout_seconds=timeout_ms / 1000.0)
        if attempt >= max_attempts:
            break
        should_retry = (status_code in retry_status_set) or (status_code == 599) or (error_msg is not None)
        if not should_retry:
            break
        if backoff_ms > 0:
            time.sleep(backoff_ms / 1000.0)

    response_map = request_cfg.get("response") if isinstance(request_cfg.get("response"), dict) else {}
    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs

    status_var = response_map.get("status")
    body_var = response_map.get("body")
    headers_var = response_map.get("headers")
    error_var = response_map.get("error")
    parsed_body: Any = None
    if resp_body:
        try:
            parsed_body = json.loads(resp_body)
        except Exception:
            parsed_body = resp_body

    if isinstance(status_var, str) and status_var.strip():
        target = status_var.strip()
        customs[target] = status_code
        _set_by_path(variables, target, status_code)
    if isinstance(body_var, str) and body_var.strip():
        target = body_var.strip()
        customs[target] = parsed_body
        _set_by_path(variables, target, parsed_body)
    if isinstance(headers_var, str) and headers_var.strip():
        target = headers_var.strip()
        customs[target] = resp_headers
        _set_by_path(variables, target, resp_headers)
    if isinstance(error_var, str) and error_var.strip():
        target = error_var.strip()
        customs[target] = error_msg
        _set_by_path(variables, target, error_msg)

    runtime_variables["api_call_last_result"] = {
        "status_code": status_code,
        "url": url,
        "error": error_msg,
        "attempts": attempt,
        "max_attempts": max_attempts,
        "body": parsed_body,
        "headers": resp_headers,
    }

    return "success" if 200 <= status_code < 300 else "error"


def _unwrap_option(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("id", "value", "name", "label"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None
    if isinstance(value, list) and value:
        return _unwrap_option(value[0])
    if isinstance(value, str):
        token = value.strip()
        return token or None
    return None


def _parse_intelligent_agent_exit_function(raw: Any) -> tuple[str | None, dict[str, Any] | None]:
    if isinstance(raw, dict):
        output_var_name = raw.get("output_var_name")
        output_var = str(output_var_name).strip() if output_var_name is not None else None
        schema = raw.get("json")
        if isinstance(schema, dict):
            return output_var or None, dict(schema)
        return output_var or None, None
    if isinstance(raw, str):
        candidate = raw.strip()
        if not candidate:
            return None, None
        try:
            parsed = json.loads(candidate)
        except Exception:
            return None, None
        if isinstance(parsed, dict):
            return None, parsed
    return None, None


def _map_ai_output_to_schema(*, parsed: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict) or not schema:
        return dict(parsed)

    mapped: dict[str, Any] = {}
    for key in schema.keys():
        if key in parsed:
            mapped[key] = parsed.get(key)

    if mapped:
        return mapped

    data = parsed.get("dados_extraidos")
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            key = str(item.get("chave") or "").strip()
            if key in schema:
                mapped[key] = item.get("valor")
    if mapped:
        return mapped

    if len(schema.keys()) == 1:
        only_key = next(iter(schema.keys()))
        text_value = (
            parsed.get("value")
            or parsed.get("resultado")
            or parsed.get("answer")
            or parsed.get("output")
            or parsed.get("text")
        )
        if text_value is not None:
            return {only_key: text_value}
    return {}


async def _run_intelligent_agent(
    *,
    db_session: AsyncSession,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str | None:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    variables = _ensure_variables(runtime_variables)
    resolution_scope = _build_runtime_resolution_scope(
        runtime_variables=runtime_variables,
        variables=variables,
    )

    prompt_rendered = _render_value(params.get("user_prompt"), resolution_scope)
    user_prompt = "" if prompt_rendered is None else str(prompt_rendered).strip()
    if not user_prompt:
        raise WorkflowExecutionError("intelligent_agent_missing_prompt", "Componente intelligent_agent sem user_prompt.")

    output_var_name, output_schema = _parse_intelligent_agent_exit_function(params.get("exit_function"))
    selected_model = _unwrap_option(params.get("llm")) or "gpt-5"
    schema_text = json.dumps(output_schema, ensure_ascii=False) if isinstance(output_schema, dict) else None

    system_prompt_parts = [
        "Você é um agente inteligente do orquestrador.",
        "Responda APENAS com JSON válido, sem markdown, sem texto fora do JSON.",
    ]
    if schema_text:
        system_prompt_parts.append(
            "O JSON de saída deve respeitar EXATAMENTE este schema (mesmas chaves):\n"
            f"{schema_text}"
        )
    system_prompt = "\n\n".join(system_prompt_parts)

    workspace_uuid: str | None = None
    workspace_api_key: str | None = None
    try:
        workspace_uuid = get_current_workspace_uuid()
    except Exception:
        workspace_uuid = None
    if workspace_uuid:
        workspace_api_key = await fetch_workspace_otima_billing_api_key(
            db_session,
            workspace_uuid=workspace_uuid,
        )

    try:
        llm_result = execute_otima_llm_prompt(
            model=selected_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            workspace_uuid=workspace_uuid,
            workspace_api_key=workspace_api_key,
        )
    except Exception as exc:
        raise WorkflowExecutionError("intelligent_agent_provider_error", f"Falha na Otima LLM: {exc}") from exc

    parsed = llm_result.get("parsed_json")
    if not isinstance(parsed, dict):
        raise WorkflowExecutionError("intelligent_agent_invalid_response", "Resposta da LLM não retornou JSON válido.")

    mapped = _map_ai_output_to_schema(parsed=parsed, schema=output_schema)
    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs

    intelligent_agent_ref = str(component.get("ref_id") or component.get("id") or "intelligent_agent").strip()
    ia_state = customs.setdefault("intelligent_agent", {})
    if isinstance(ia_state, dict):
        ia_state[intelligent_agent_ref] = {
            "model": selected_model,
            "output_var_name": output_var_name,
            "schema": output_schema,
            "result": mapped if mapped else parsed,
        }

    if output_var_name:
        customs[output_var_name] = mapped if mapped else parsed
        _set_by_path(variables, output_var_name, mapped if mapped else parsed)
    if isinstance(mapped, dict):
        for key, value in mapped.items():
            if isinstance(key, str) and key.strip():
                _set_by_path(variables, key.strip(), value)
                customs[key.strip()] = value

    runtime_variables["intelligent_agent_last_result"] = {
        "model": selected_model,
        "output_var_name": output_var_name,
        "result": mapped if mapped else parsed,
        "raw_text": llm_result.get("raw_text"),
        "endpoint": llm_result.get("endpoint"),
        "status_code": llm_result.get("status_code"),
    }
    return None


async def execute_workflow_m2_for_session(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    session_id: int,
) -> WorkflowExecutionResult:
    settings = get_settings()
    if not _read_enabled(settings):
        return WorkflowExecutionResult(
            enabled=False,
            executed_steps=0,
            stopped_reason="workflow_m2_disabled",
            last_card_uuid=None,
            next_card_uuid=None,
        )

    safe_schema = get_current_workspace_schema().replace('"', '""')
    max_steps = _read_max_steps(settings)
    execution_started_at = datetime.now(timezone.utc)
    execution_started_perf = time.perf_counter()
    session_uuid_for_metrics: str | None = None
    revision_id_for_metrics: str | None = None
    metrics: list[dict[str, Any]] = []

    def _append_metric(
        *,
        metric_type: str,
        status: str,
        started_at: datetime,
        finished_at: datetime,
        latency_ms: float,
        stopped_reason: str | None,
        step_index: int | None = None,
        card_cursor: str | None = None,
        component_kind_value: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        metrics.append(
            {
                "session_id": session_id,
                "session_uuid": session_uuid_for_metrics,
                "flow_uuid": flow_uuid,
                "revision_id": revision_id_for_metrics,
                "metric_type": metric_type,
                "step_index": step_index,
                "card_uuid": _to_uuid_or_none(card_cursor),
                "card_cursor": card_cursor,
                "component_kind": component_kind_value,
                "status": status,
                "stopped_reason": stopped_reason,
                "latency_ms": round(latency_ms, 2),
                "started_at": started_at,
                "finished_at": finished_at,
                "details": details or {},
            }
        )

    async def _finalize(result: WorkflowExecutionResult) -> WorkflowExecutionResult:
        finished_at = datetime.now(timezone.utc)
        total_latency_ms = (time.perf_counter() - execution_started_perf) * 1000
        success_stop_reasons = {
            "finished_by_component",
            "scheduled_wait",
            "end_of_branch",
            "blocked_send_with_whatsapp",
            "blocked_process_whatsapp_response",
            "blocked_send_with_dialer",
            "blocked_process_dialer_response",
        }
        workflow_status = "success"
        if result.stopped_reason == "session_execution_locked":
            workflow_status = "locked"
        elif result.stopped_reason not in success_stop_reasons:
            workflow_status = "stopped"

        _append_metric(
            metric_type="workflow",
            status=workflow_status,
            started_at=execution_started_at,
            finished_at=finished_at,
            latency_ms=total_latency_ms,
            stopped_reason=result.stopped_reason,
            details={
                "executed_steps": result.executed_steps,
                "last_card_uuid": result.last_card_uuid,
                "next_card_uuid": result.next_card_uuid,
            },
        )
        await persist_session_metrics(db_session, metrics=metrics)
        logger.info(
            "workflow m2 execution metrics",
            extra={
                "event": "orch.workflow.m2.metrics",
                "flow_uuid": flow_uuid,
                "session_id": session_id,
                "session_uuid": session_uuid_for_metrics,
                "metric_type": "workflow",
                "total_latency_ms": round(total_latency_ms, 2),
                "stopped_reason": result.stopped_reason,
            },
        )
        return result

    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        lock_result = await db_session.execute(
            text("SELECT pg_try_advisory_xact_lock(:class_id, :object_id) AS locked"),
            {
                "class_id": 92021,
                "object_id": int(session_id),
            },
        )
        locked = bool(lock_result.scalar_one())
        if not locked:
            return await _finalize(
                WorkflowExecutionResult(
                    enabled=True,
                    executed_steps=0,
                    stopped_reason="session_execution_locked",
                    last_card_uuid=None,
                    next_card_uuid=None,
                )
            )

        flow_row = await fetch_flow_row(db_session, flow_uuid=flow_uuid)
        if flow_row is None:
            return await _finalize(WorkflowExecutionResult(True, 0, "flow_not_found", None, None))

        selected_revision = await fetch_selected_revision(db_session, flow_id=str(flow_row["id"]))
        if selected_revision is None:
            return await _finalize(WorkflowExecutionResult(True, 0, "revision_not_found", None, None))
        revision_id_for_metrics = str(selected_revision["id"])

        definition = selected_revision.get("definition")
        if not isinstance(definition, dict):
            raise WorkflowExecutionError("invalid_definition", "Definição do fluxo inválida para execução M2.")

        session_state = await fetch_session_workflow_state(db_session, session_id=session_id)
        if session_state is None:
            return await _finalize(WorkflowExecutionResult(True, 0, "session_not_found", None, None))
        session_uuid_for_metrics = session_state.get("uuid")

        runtime_variables = session_state.get("runtime_variables")
        if not isinstance(runtime_variables, dict):
            runtime_variables = {}

        has_pending_whatsapp_events = False
        resume_cursor = _read_whatsapp_resume_cursor(runtime_variables)
        if resume_cursor is not None:
            has_pending_whatsapp_events = await has_pending_channel_events(
                db_session,
                session_id=session_id,
                channel="whatsapp",
            )

        current_next_card_uuid = session_state.get("next_card_uuid")
        if current_next_card_uuid is None:
            current_next_card_uuid = _read_next_cursor(runtime_variables)
        blocking_stop_reason = _read_blocking_stop_reason(runtime_variables)

        frozen_until = session_state.get("frozen_until")
        should_preempt_to_whatsapp_resume_cursor = _should_preempt_to_whatsapp_resume_cursor(
            runtime_variables,
            has_pending_whatsapp_events=has_pending_whatsapp_events,
            current_next_card_uuid=current_next_card_uuid,
            blocking_stop_reason=blocking_stop_reason,
        )
        if isinstance(frozen_until, datetime):
            frozen_until_utc = frozen_until if frozen_until.tzinfo is not None else frozen_until.replace(tzinfo=timezone.utc)
            if frozen_until_utc > datetime.now(timezone.utc) and not should_preempt_to_whatsapp_resume_cursor:
                return await _finalize(
                    WorkflowExecutionResult(
                        True,
                        0,
                        "frozen_wait_active",
                        session_state.get("last_card_uuid"),
                        session_state.get("next_card_uuid"),
                    )
                )

        if should_preempt_to_whatsapp_resume_cursor:
            preempt_signature = _extract_whatsapp_status_signature_from_runtime(runtime_variables)
            if preempt_signature is not None:
                _set_whatsapp_last_preempt_signature(runtime_variables, preempt_signature)
            current_card_uuid = resume_cursor
        else:
            current_card_uuid = current_next_card_uuid
            if current_card_uuid is None and _extract_whatsapp_status_from_runtime(runtime_variables) is not None:
                current_card_uuid = _read_whatsapp_resume_cursor(runtime_variables)
        if blocking_stop_reason is not None:
            if _should_resume_whatsapp_blocking_execution(runtime_variables):
                _clear_blocking_execution(runtime_variables)
                await replace_session_workflow_state(
                    db_session,
                    session_id=session_id,
                    runtime_variables=runtime_variables,
                    last_card_uuid=_to_uuid_or_none(session_state.get("last_card_uuid")),
                    next_card_uuid=_to_uuid_or_none(current_card_uuid),
                )
            else:
                return await _finalize(
                    WorkflowExecutionResult(
                        True,
                        0,
                        blocking_stop_reason,
                        session_state.get("last_card_uuid"),
                        current_card_uuid,
                    )
                )
        if current_card_uuid is None:
            return await _finalize(WorkflowExecutionResult(True, 0, "no_next_card", session_state.get("last_card_uuid"), None))

        components = index_components(definition)
        executed_steps = 0
        last_card_uuid = session_state.get("last_card_uuid")
        next_card_uuid = current_card_uuid

        for _ in range(max_steps):
            if not next_card_uuid:
                break

            step_started_at = datetime.now(timezone.utc)
            step_started_perf = time.perf_counter()
            component = components.get(next_card_uuid)
            if component is None:
                step_finished_at = datetime.now(timezone.utc)
                _append_metric(
                    metric_type="card",
                    status="error",
                    started_at=step_started_at,
                    finished_at=step_finished_at,
                    latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                    stopped_reason="component_not_found",
                    step_index=executed_steps + 1,
                    card_cursor=next_card_uuid,
                    component_kind_value=None,
                )
                return await _finalize(
                    WorkflowExecutionResult(True, executed_steps, "component_not_found", last_card_uuid, next_card_uuid)
                )

            kind = component_kind(component)
            branch_label: str | None = None

            try:
                if kind == "set_variables":
                    _run_set_variables(component, runtime_variables)
                elif kind == "condition":
                    branch_label = _run_condition(component, runtime_variables)
                elif kind == "code_editor":
                    branch_candidates = outgoing_branch_labels(definition, current_card_uuid=next_card_uuid)
                    branch_label = _run_code_editor(
                        component=component,
                        runtime_variables=runtime_variables,
                        branch_labels=branch_candidates,
                    )
                elif kind == "api_call":
                    branch_label = _run_api_call(
                        component=component,
                        runtime_variables=runtime_variables,
                    )
                elif kind == "generate_file":
                    branch_label = await _run_generate_file(
                        db_session=db_session,
                        flow_uuid=flow_uuid,
                        component=component,
                        runtime_variables=runtime_variables,
                        session_id=session_id,
                    )
                elif kind == "intelligent_agent":
                    branch_label = await _run_intelligent_agent(
                        db_session=db_session,
                        component=component,
                        runtime_variables=runtime_variables,
                    )
                elif kind in {"proccess_whatsapp_response", "process_whatsapp_response"}:
                    _set_whatsapp_resume_cursor(
                        runtime_variables,
                        process_card_cursor=next_card_uuid,
                    )
                    pending_whatsapp_event = await claim_next_pending_channel_event(
                        db_session,
                        session_id=session_id,
                        channel="whatsapp",
                    )
                    if isinstance(pending_whatsapp_event, dict):
                        payload = pending_whatsapp_event.get("payload")
                        if isinstance(payload, dict):
                            runtime_variables["last_payload"] = payload
                    if (defer_until := _compute_whatsapp_status_order_delay(
                        runtime_variables=runtime_variables,
                        session_state=session_state,
                    )) is not None:
                        await replace_session_workflow_state(
                            db_session,
                            session_id=session_id,
                            runtime_variables=runtime_variables,
                            last_card_uuid=_to_uuid_or_none(last_card_uuid),
                            next_card_uuid=_to_uuid_or_none(next_card_uuid),
                            frozen_until=defer_until,
                        )
                        step_latency_ms = (time.perf_counter() - step_started_perf) * 1000
                        step_finished_at = datetime.now(timezone.utc)
                        _append_metric(
                            metric_type="card",
                            status="success",
                            started_at=step_started_at,
                            finished_at=step_finished_at,
                            latency_ms=step_latency_ms,
                            stopped_reason="frozen_wait_active",
                            step_index=executed_steps,
                            card_cursor=next_card_uuid,
                            component_kind_value=kind,
                            details={
                                "defer_until": defer_until.isoformat(),
                                "reason": "waiting_previous_whatsapp_status",
                            },
                        )
                        return await _finalize(
                            WorkflowExecutionResult(
                                True,
                                executed_steps,
                                "frozen_wait_active",
                                last_card_uuid,
                                next_card_uuid,
                            )
                        )
                    branch_label = _run_process_whatsapp_response(
                        component=component,
                        runtime_variables=runtime_variables,
                    )
                elif (blocking_stop_reason := _blocking_stop_reason_for_component(kind)) is not None:
                    if kind == "send_with_whatsapp":
                        await _prepare_send_with_whatsapp_contact_member(
                            db_session=db_session,
                            flow_uuid=flow_uuid,
                            session_id=session_id,
                            component=component,
                            runtime_variables=runtime_variables,
                        )
                    resolved_next = resolve_next_card_uuid(definition, next_card_uuid)
                    last_card_uuid = next_card_uuid
                    next_card_uuid = resolved_next
                    executed_steps += 1
                    _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=next_card_uuid)
                    _mark_blocking_execution(runtime_variables, stopped_reason=blocking_stop_reason)

                    await replace_session_workflow_state(
                        db_session,
                        session_id=session_id,
                        runtime_variables=runtime_variables,
                        last_card_uuid=_to_uuid_or_none(last_card_uuid),
                        next_card_uuid=_to_uuid_or_none(next_card_uuid),
                    )
                    step_latency_ms = (time.perf_counter() - step_started_perf) * 1000
                    step_finished_at = datetime.now(timezone.utc)
                    _append_metric(
                        metric_type="card",
                        status="success",
                        started_at=step_started_at,
                        finished_at=step_finished_at,
                        latency_ms=step_latency_ms,
                        stopped_reason=blocking_stop_reason,
                        step_index=executed_steps,
                        card_cursor=last_card_uuid,
                        component_kind_value=kind,
                        details={"next_card_uuid": next_card_uuid},
                    )
                    logger.info(
                        "workflow m2 card blocking",
                        extra={
                            "event": "orch.workflow.m2.card.blocking",
                            "flow_uuid": flow_uuid,
                            "session_id": session_id,
                            "session_uuid": session_uuid_for_metrics,
                            "card_uuid": _to_uuid_or_none(last_card_uuid) or last_card_uuid,
                            "component_kind": kind,
                            "step_index": executed_steps,
                            "latency_ms": round(step_latency_ms, 2),
                            "stopped_reason": blocking_stop_reason,
                            "metric_type": "card",
                        },
                    )
                    return await _finalize(
                        WorkflowExecutionResult(
                            True,
                            executed_steps,
                            blocking_stop_reason,
                            last_card_uuid,
                            next_card_uuid,
                        )
                    )
                elif kind in {"wait", "scheduling_moment", "scheduling-moment"}:
                    resolved_next = resolve_next_card_uuid(definition, next_card_uuid)
                    frozen_until = _compute_frozen_until(component, runtime_variables)
                    last_card_uuid = next_card_uuid
                    next_card_uuid = resolved_next
                    executed_steps += 1
                    _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=next_card_uuid)

                    await replace_session_workflow_state(
                        db_session,
                        session_id=session_id,
                        runtime_variables=runtime_variables,
                        last_card_uuid=_to_uuid_or_none(last_card_uuid),
                        next_card_uuid=_to_uuid_or_none(next_card_uuid),
                        frozen_until=frozen_until,
                    )
                    step_finished_at = datetime.now(timezone.utc)
                    _append_metric(
                        metric_type="card",
                        status="success",
                        started_at=step_started_at,
                        finished_at=step_finished_at,
                        latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                        stopped_reason="scheduled_wait",
                        step_index=executed_steps,
                        card_cursor=last_card_uuid,
                        component_kind_value=kind,
                        details={"next_card_uuid": next_card_uuid},
                    )
                    logger.info(
                        "workflow m2 card latency",
                        extra={
                            "event": "orch.workflow.m2.card",
                            "flow_uuid": flow_uuid,
                            "session_id": session_id,
                            "session_uuid": session_uuid_for_metrics,
                            "card_uuid": _to_uuid_or_none(last_card_uuid) or last_card_uuid,
                            "component_kind": kind,
                            "step_index": executed_steps,
                            "latency_ms": round((time.perf_counter() - step_started_perf) * 1000, 2),
                            "stopped_reason": "scheduled_wait",
                            "metric_type": "card",
                        },
                    )
                    return await _finalize(
                        WorkflowExecutionResult(True, executed_steps, "scheduled_wait", last_card_uuid, next_card_uuid)
                    )
                elif kind in {"finish_flow", "finish-flow"}:
                    resume_cursor_for_channel = _read_whatsapp_resume_cursor(runtime_variables)
                    has_more_whatsapp_events = False
                    if resume_cursor_for_channel is not None:
                        has_more_whatsapp_events = await has_pending_channel_events(
                            db_session,
                            session_id=session_id,
                            channel="whatsapp",
                        )
                    if has_more_whatsapp_events and resume_cursor_for_channel is not None:
                        last_card_uuid = next_card_uuid
                        next_card_uuid = resume_cursor_for_channel
                        executed_steps += 1
                        _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=next_card_uuid)
                        await replace_session_workflow_state(
                            db_session,
                            session_id=session_id,
                            runtime_variables=runtime_variables,
                            last_card_uuid=_to_uuid_or_none(last_card_uuid),
                            next_card_uuid=_to_uuid_or_none(next_card_uuid),
                            ended_at=None,
                            state=2,
                        )
                        step_finished_at = datetime.now(timezone.utc)
                        _append_metric(
                            metric_type="card",
                            status="success",
                            started_at=step_started_at,
                            finished_at=step_finished_at,
                            latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                            stopped_reason="continue_with_pending_channel_event",
                            step_index=executed_steps,
                            card_cursor=last_card_uuid,
                            component_kind_value=kind,
                            details={"next_card_uuid": next_card_uuid},
                        )
                        continue

                    finished_at = datetime.now(timezone.utc)
                    last_card_uuid = next_card_uuid
                    next_card_uuid = None
                    executed_steps += 1
                    _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=next_card_uuid)

                    await replace_session_workflow_state(
                        db_session,
                        session_id=session_id,
                        runtime_variables=runtime_variables,
                        last_card_uuid=_to_uuid_or_none(last_card_uuid),
                        next_card_uuid=_to_uuid_or_none(next_card_uuid),
                        ended_at=finished_at,
                        state=3,
                    )
                    step_finished_at = datetime.now(timezone.utc)
                    _append_metric(
                        metric_type="card",
                        status="success",
                        started_at=step_started_at,
                        finished_at=step_finished_at,
                        latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                        stopped_reason="finished_by_component",
                        step_index=executed_steps,
                        card_cursor=last_card_uuid,
                        component_kind_value=kind,
                    )
                    logger.info(
                        "workflow m2 card latency",
                        extra={
                            "event": "orch.workflow.m2.card",
                            "flow_uuid": flow_uuid,
                            "session_id": session_id,
                            "session_uuid": session_uuid_for_metrics,
                            "card_uuid": _to_uuid_or_none(last_card_uuid) or last_card_uuid,
                            "component_kind": kind,
                            "step_index": executed_steps,
                            "latency_ms": round((time.perf_counter() - step_started_perf) * 1000, 2),
                            "stopped_reason": "finished_by_component",
                            "metric_type": "card",
                        },
                    )
                    return await _finalize(
                        WorkflowExecutionResult(True, executed_steps, "finished_by_component", last_card_uuid, None)
                    )
                else:
                    step_finished_at = datetime.now(timezone.utc)
                    _append_metric(
                        metric_type="card",
                        status="stopped",
                        started_at=step_started_at,
                        finished_at=step_finished_at,
                        latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                        stopped_reason=f"component_not_supported:{kind}",
                        step_index=executed_steps + 1,
                        card_cursor=next_card_uuid,
                        component_kind_value=kind,
                    )
                    return await _finalize(
                        WorkflowExecutionResult(True, executed_steps, f"component_not_supported:{kind}", last_card_uuid, next_card_uuid)
                    )
            except Exception as exc:
                step_finished_at = datetime.now(timezone.utc)
                _append_metric(
                    metric_type="card",
                    status="error",
                    started_at=step_started_at,
                    finished_at=step_finished_at,
                    latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                    stopped_reason=type(exc).__name__,
                    step_index=executed_steps + 1,
                    card_cursor=next_card_uuid,
                    component_kind_value=kind,
                    details={"message": str(exc)},
                )
                raise

            current = next_card_uuid
            if branch_label is not None:
                resolved_next = resolve_next_card_uuid_by_branch(
                    definition,
                    current_card_uuid=current,
                    branch_label=branch_label,
                )
            else:
                resolved_next = resolve_next_card_uuid(definition, current)

            last_card_uuid = current
            next_card_uuid = resolved_next
            executed_steps += 1
            _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=next_card_uuid)

            await replace_session_workflow_state(
                db_session,
                session_id=session_id,
                runtime_variables=runtime_variables,
                last_card_uuid=_to_uuid_or_none(last_card_uuid),
                next_card_uuid=_to_uuid_or_none(next_card_uuid),
            )
            step_latency_ms = (time.perf_counter() - step_started_perf) * 1000
            _append_metric(
                metric_type="card",
                status="success",
                started_at=step_started_at,
                finished_at=datetime.now(timezone.utc),
                latency_ms=step_latency_ms,
                stopped_reason=None,
                step_index=executed_steps,
                card_cursor=last_card_uuid,
                component_kind_value=kind,
                details={"branch_label": branch_label, "next_card_uuid": next_card_uuid},
            )
            logger.info(
                "workflow m2 card latency",
                extra={
                    "event": "orch.workflow.m2.card",
                    "flow_uuid": flow_uuid,
                    "session_id": session_id,
                    "session_uuid": session_uuid_for_metrics,
                    "card_uuid": _to_uuid_or_none(last_card_uuid) or last_card_uuid,
                    "component_kind": kind,
                    "step_index": executed_steps,
                    "latency_ms": round(step_latency_ms, 2),
                    "metric_type": "card",
                },
            )

            if resolved_next is None:
                return await _finalize(
                    WorkflowExecutionResult(True, executed_steps, "end_of_branch", last_card_uuid, None)
                )

        return await _finalize(
            WorkflowExecutionResult(True, executed_steps, "max_steps_reached", last_card_uuid, next_card_uuid)
        )
