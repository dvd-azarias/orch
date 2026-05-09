from __future__ import annotations

from datetime import datetime, timezone

import app.services.workflow_m2_service as workflow_m2_service
from app.services.workflow_m2_service import _compute_frozen_until, _run_api_call, _run_code_editor, _run_condition, _run_set_variables


def test_set_variables_renders_template_into_runtime() -> None:
    runtime_variables = {"variables": {"valor_recebido": 114}}
    component = {
        "parameters": {
            "instructions": [
                {"variable": "resultado", "value": "{{valor_recebido}}"},
                {"variable": "variables.copia", "value": "{{valor_recebido}}"},
            ]
        }
    }

    _run_set_variables(component, runtime_variables)

    assert runtime_variables["variables"]["customs"]["resultado"] == 114
    assert runtime_variables["variables"]["copia"] == 114


def test_condition_returns_true_branch_when_rule_matches() -> None:
    runtime_variables = {"variables": {"valor": 10}}
    component = {
        "parameters": {
            "conditions": [
                {
                    "match": "all",
                    "branch": "true",
                    "rules": [{"field": "valor", "operator": "gt", "value": 5}],
                }
            ]
        }
    }

    branch = _run_condition(component, runtime_variables)
    assert branch == "true"


def test_condition_supports_rule_id_branch_and_template_field() -> None:
    runtime_variables = {"variables": {"customs": {"var_teste": 8}}}
    component = {
        "parameters": {
            "conditions": [
                {
                    "id": "branch-maior",
                    "label": "eh_maior",
                    "match": "all",
                    "rules": [{"field": "{{var_teste}}", "operator": "greater", "value": "100"}],
                },
                {
                    "id": "branch-menor",
                    "label": "eh_menor",
                    "match": "all",
                    "rules": [{"field": "{{var_teste}}", "operator": "less", "value": "100"}],
                },
            ]
        }
    }

    branch = _run_condition(component, runtime_variables)
    assert branch == "branch-menor"


def test_condition_is_operator_matches_numeric_string_and_number() -> None:
    runtime_variables = {"variables": {"customs": {"var_teste": 100}}}
    component = {
        "parameters": {
            "conditions": [
                {
                    "id": "branch-igual",
                    "match": "all",
                    "rules": [{"field": "{{var_teste}}", "operator": "is", "value": "100"}],
                }
            ]
        }
    }
    branch = _run_condition(component, runtime_variables)
    assert branch == "branch-igual"


def test_compute_frozen_until_from_wait_ms() -> None:
    runtime_variables = {"variables": {}}
    component = {"parameters": {"tempo_ms": 1200}}

    frozen_until = _compute_frozen_until(component, runtime_variables)

    now = datetime.now(timezone.utc)
    delta_ms = int((frozen_until - now).total_seconds() * 1000)
    assert 0 <= delta_ms <= 2000


def test_compute_frozen_until_from_iso() -> None:
    runtime_variables = {"variables": {}}
    component = {"parameters": {"resume_at": "2030-01-01T00:00:00Z"}}

    frozen_until = _compute_frozen_until(component, runtime_variables)

    assert frozen_until.isoformat().startswith("2030-01-01T00:00:00")


def test_code_editor_executes_and_returns_branch() -> None:
    runtime_variables = {"variables": {"customs": {"valor_recebido": 114}}}
    component = {
        "parameters": {
            "timeout_ms": 500,
            "code": """
export default async function main(ctx) {
  const v = ctx.variables.customs.valor_recebido;
  ctx.variables.customs.resultado = v * 2;
  return { branch: ctx.branches.success, payload: { ok: true } };
}
""",
        }
    }

    branch = _run_code_editor(
        component=component,
        runtime_variables=runtime_variables,
        branch_labels=["success", "error"],
    )

    assert branch == "success"
    assert runtime_variables["variables"]["customs"]["resultado"] == 228


def test_api_call_maps_response_into_customs(monkeypatch) -> None:
    runtime_variables = {"variables": {"customs": {"resultado_primo": "primo"}}}
    component = {
        "parameters": {
            "request": {
                "url": "https://example.test/hook",
                "method": "POST",
                "timeout": 1000,
                "headers": [],
                "query": [],
                "body": {"mode": "json", "json": "{\"resultado\": \"{{resultado_primo}}\"}"},
                "response": {
                    "status": "api_status",
                    "body": "api_body",
                    "headers": "api_headers",
                    "error": "api_error",
                },
            }
        }
    }

    def fake_http_execute(req, timeout_seconds):
        return 200, {"x-a": "b"}, "{\"ok\": true}", None

    monkeypatch.setattr(workflow_m2_service, "_http_execute", fake_http_execute)

    branch = _run_api_call(component=component, runtime_variables=runtime_variables)
    customs = runtime_variables["variables"]["customs"]

    assert branch == "success"
    assert customs["api_status"] == 200
    assert customs["api_body"] == {"ok": True}
    assert customs["api_headers"] == {"x-a": "b"}
    assert customs["api_error"] is None


def test_api_call_retry_succeeds_after_transient_failures(monkeypatch) -> None:
    runtime_variables = {"variables": {"customs": {}}}
    component = {
        "parameters": {
            "request": {
                "url": "https://example.test/hook",
                "method": "POST",
                "timeout": 1000,
                "retry": {
                    "max_attempts": 3,
                    "backoff_ms": 0,
                },
                "response": {
                    "status": "api_status",
                    "error": "api_error",
                },
            }
        }
    }

    calls = {"n": 0}

    def fake_http_execute(req, timeout_seconds):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < 3:
            return 503, {}, '{"error":"temporario"}', "503 Service Unavailable"
        return 200, {}, '{"ok":true}', None

    monkeypatch.setattr(workflow_m2_service, "_http_execute", fake_http_execute)

    branch = _run_api_call(component=component, runtime_variables=runtime_variables)
    assert branch == "success"
    assert calls["n"] == 3
    assert runtime_variables["api_call_last_result"]["attempts"] == 3
    assert runtime_variables["variables"]["customs"]["api_status"] == 200


def test_api_call_retry_exhausted_returns_error(monkeypatch) -> None:
    runtime_variables = {"variables": {"customs": {}}}
    component = {
        "parameters": {
            "request": {
                "url": "https://example.test/hook",
                "method": "POST",
                "timeout": 1000,
                "retry": {
                    "max_attempts": 2,
                    "backoff_ms": 0,
                    "on_statuses": [503],
                },
                "response": {
                    "status": "api_status",
                    "error": "api_error",
                },
            }
        }
    }

    calls = {"n": 0}

    def fake_http_execute(req, timeout_seconds):  # noqa: ANN001
        calls["n"] += 1
        return 503, {}, '{"error":"temporario"}', "503 Service Unavailable"

    monkeypatch.setattr(workflow_m2_service, "_http_execute", fake_http_execute)

    branch = _run_api_call(component=component, runtime_variables=runtime_variables)
    assert branch == "error"
    assert calls["n"] == 2
    assert runtime_variables["api_call_last_result"]["attempts"] == 2
    assert runtime_variables["variables"]["customs"]["api_status"] == 503
