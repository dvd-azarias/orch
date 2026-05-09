from __future__ import annotations

import json
import re
import subprocess
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.flow_v2_repository import fetch_flow_row, fetch_selected_revision
from app.repositories.orch_sessions_repository import fetch_session_workflow_state, replace_session_workflow_state
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


def _set_cursors(runtime_variables: dict[str, Any], *, last_cursor: str | None, next_cursor: str | None) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    workflow_meta["last_card_cursor"] = last_cursor
    workflow_meta["next_card_cursor"] = next_cursor


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
                resolved = customs.get(token)
                if resolved is None:
                    resolved = payload.get(token)
            return resolved

        rendered = template
        for match in matches:
            token = match.group(1).strip()
            value = _get_by_dot_path(variables, token)
            if value is None:
                customs = variables.get("customs") if isinstance(variables.get("customs"), dict) else {}
                payload = variables.get("payload") if isinstance(variables.get("payload"), dict) else {}
                value = customs.get(token)
                if value is None:
                    value = payload.get(token)
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
                        actual = customs.get(field)
                        if actual is None:
                            actual = payload.get(field)
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

    if isinstance(status_var, str) and status_var.strip():
        customs[status_var.strip()] = status_code
    if isinstance(body_var, str) and body_var.strip():
        try:
            customs[body_var.strip()] = json.loads(resp_body) if resp_body else None
        except Exception:
            customs[body_var.strip()] = resp_body
    if isinstance(headers_var, str) and headers_var.strip():
        customs[headers_var.strip()] = resp_headers
    if isinstance(error_var, str) and error_var.strip():
        customs[error_var.strip()] = error_msg

    runtime_variables["api_call_last_result"] = {
        "status_code": status_code,
        "url": url,
        "error": error_msg,
        "attempts": attempt,
        "max_attempts": max_attempts,
    }

    return "success" if 200 <= status_code < 300 else "error"


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

    safe_schema = settings.database_schema.replace('"', '""')
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
        workflow_status = "success"
        if result.stopped_reason == "session_execution_locked":
            workflow_status = "locked"
        elif result.stopped_reason not in {"finished_by_component", "scheduled_wait", "end_of_branch"}:
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

        frozen_until = session_state.get("frozen_until")
        if isinstance(frozen_until, datetime):
            frozen_until_utc = frozen_until if frozen_until.tzinfo is not None else frozen_until.replace(tzinfo=timezone.utc)
            if frozen_until_utc > datetime.now(timezone.utc):
                return await _finalize(
                    WorkflowExecutionResult(
                        True,
                        0,
                        "frozen_wait_active",
                        session_state.get("last_card_uuid"),
                        session_state.get("next_card_uuid"),
                    )
                )

        current_card_uuid = session_state.get("next_card_uuid")
        if current_card_uuid is None:
            current_card_uuid = _read_next_cursor(runtime_variables)
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
