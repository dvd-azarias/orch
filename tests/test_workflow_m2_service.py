from __future__ import annotations

from datetime import datetime, timezone

import pytest
import app.services.workflow_m2_service as workflow_m2_service
from app.services.workflow_m2_service import (
    _blocking_stop_reason_for_component,
    _clear_blocking_execution,
    _compute_whatsapp_status_order_delay,
    _compute_frozen_until,
    _extract_whatsapp_status_signature_from_runtime,
    _extract_whatsapp_status_from_runtime,
    _mark_blocking_execution,
    _read_blocking_stop_reason,
    _read_whatsapp_last_preempt_signature,
    _read_whatsapp_resume_cursor,
    _run_api_call,
    _run_code_editor,
    _run_condition,
    _run_generate_file,
    _run_intelligent_agent,
    _run_process_whatsapp_response,
    _run_set_variables,
    _set_whatsapp_last_preempt_signature,
    _set_whatsapp_resume_cursor,
    _should_preempt_to_whatsapp_resume_cursor,
    _should_resume_whatsapp_blocking_execution,
)


def test_blocking_stop_reason_for_whatsapp_components() -> None:
    assert _blocking_stop_reason_for_component("send_with_whatsapp") == "blocked_send_with_whatsapp"
    assert _blocking_stop_reason_for_component("proccess_whatsapp_response") == "blocked_process_whatsapp_response"
    assert _blocking_stop_reason_for_component("process_whatsapp_response") == "blocked_process_whatsapp_response"
    assert _blocking_stop_reason_for_component("send_with_dialer") == "blocked_send_with_dialer"
    assert _blocking_stop_reason_for_component("proccess_dialer_response") == "blocked_process_dialer_response"
    assert _blocking_stop_reason_for_component("process_dialer_response") == "blocked_process_dialer_response"
    assert _blocking_stop_reason_for_component("generate_file") is None


def test_blocking_execution_roundtrip_in_runtime_meta() -> None:
    runtime_variables: dict[str, object] = {}
    assert _read_blocking_stop_reason(runtime_variables) is None

    _mark_blocking_execution(
        runtime_variables, stopped_reason="blocked_process_whatsapp_response"
    )

    assert _read_blocking_stop_reason(runtime_variables) == "blocked_process_whatsapp_response"


def test_clear_blocking_execution_resets_runtime_meta() -> None:
    runtime_variables: dict[str, object] = {}
    _mark_blocking_execution(runtime_variables, stopped_reason="blocked_process_whatsapp_response")
    _clear_blocking_execution(runtime_variables)
    assert _read_blocking_stop_reason(runtime_variables) is None


def test_extract_whatsapp_status_from_runtime_uses_last_payload() -> None:
    runtime_variables = {
        "last_payload": {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [{"status": "delivered"}],
                            }
                        }
                    ]
                }
            ],
        }
    }
    assert _extract_whatsapp_status_from_runtime(runtime_variables) == "delivered"


def test_run_process_whatsapp_response_maps_status_to_branch() -> None:
    runtime_variables = {
        "last_payload": {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [{"status": "read"}],
                            }
                        }
                    ]
                }
            ],
        }
    }
    branch = _run_process_whatsapp_response(
        component={"ref_id": "resp-1"},
        runtime_variables=runtime_variables,
    )
    assert branch == "read"
    assert runtime_variables["whatsapp_last_response"]["status"] == "read"
    assert runtime_variables["whatsapp_last_response"]["branch"] == "read"


def test_should_resume_whatsapp_blocking_execution_only_when_status_available() -> None:
    runtime_variables = {
        "workflow_v2": {
            "blocking_execution": True,
            "blocking_stop_reason": "blocked_send_with_whatsapp",
        },
        "last_payload": {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [{"status": "sent"}],
                            }
                        }
                    ]
                }
            ],
        },
    }
    assert _should_resume_whatsapp_blocking_execution(runtime_variables) is True

    runtime_variables["last_payload"] = {"external_id": "generic-event"}
    assert _should_resume_whatsapp_blocking_execution(runtime_variables) is False


def test_set_and_read_whatsapp_resume_cursor_roundtrip() -> None:
    runtime_variables: dict[str, object] = {}
    assert _read_whatsapp_resume_cursor(runtime_variables) is None
    _set_whatsapp_resume_cursor(runtime_variables, process_card_cursor="card-process-1")
    assert _read_whatsapp_resume_cursor(runtime_variables) == "card-process-1"


def test_should_preempt_to_whatsapp_resume_cursor_when_status_and_cursor_exist() -> None:
    runtime_variables = {
        "workflow_v2": {
            "channel_resume": {"whatsapp": {"process_card_cursor": "card-process-1"}},
        },
        "last_payload": {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"statuses": [{"status": "read", "id": "wamid-1", "timestamp": "1700"}]}}]}],
        },
    }
    assert _should_preempt_to_whatsapp_resume_cursor(runtime_variables) is True


def test_should_preempt_to_whatsapp_resume_cursor_when_pending_events_exist() -> None:
    runtime_variables = {
        "workflow_v2": {
            "channel_resume": {"whatsapp": {"process_card_cursor": "card-process-1"}},
        }
    }
    assert _should_preempt_to_whatsapp_resume_cursor(
        runtime_variables,
        has_pending_whatsapp_events=True,
    ) is True


def test_should_not_preempt_when_downstream_card_is_pending() -> None:
    runtime_variables = {
        "workflow_v2": {
            "channel_resume": {"whatsapp": {"process_card_cursor": "card-process-1"}},
        }
    }
    assert _should_preempt_to_whatsapp_resume_cursor(
        runtime_variables,
        has_pending_whatsapp_events=True,
        current_next_card_uuid="card-api-call-1",
    ) is False


def test_extract_whatsapp_status_signature_from_runtime() -> None:
    runtime_variables = {
        "last_payload": {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "status": "delivered",
                                        "id": "wamid-2",
                                        "timestamp": "1701",
                                        "recipient_id": "5511999999999",
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        }
    }
    assert _extract_whatsapp_status_signature_from_runtime(runtime_variables) == "delivered|wamid-2|1701|5511999999999"


def test_should_not_preempt_again_when_signature_already_consumed() -> None:
    runtime_variables = {
        "workflow_v2": {
            "channel_resume": {"whatsapp": {"process_card_cursor": "card-process-1"}},
        },
        "last_payload": {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"statuses": [{"status": "sent", "id": "wamid-3", "timestamp": "1702"}]}}]}],
        },
    }
    assert _should_preempt_to_whatsapp_resume_cursor(runtime_variables) is True
    signature = _extract_whatsapp_status_signature_from_runtime(runtime_variables)
    assert signature is not None
    _set_whatsapp_last_preempt_signature(runtime_variables, signature)
    assert _read_whatsapp_last_preempt_signature(runtime_variables) == signature
    assert _should_preempt_to_whatsapp_resume_cursor(runtime_variables) is False


def test_should_not_preempt_without_whatsapp_resume_cursor() -> None:
    runtime_variables = {
        "last_payload": {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}],
        },
    }
    assert _should_preempt_to_whatsapp_resume_cursor(runtime_variables) is False


def test_compute_whatsapp_status_order_delay_for_delivered_without_sent() -> None:
    runtime_variables = {
        "last_payload": {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}],
        }
    }
    session_state = {"whatsapp_sent_at": None}

    defer_until = _compute_whatsapp_status_order_delay(
        runtime_variables=runtime_variables,
        session_state=session_state,
    )
    assert defer_until is not None


def test_compute_whatsapp_status_order_delay_releases_after_prerequisite() -> None:
    runtime_variables = {
        "last_payload": {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}],
        }
    }
    session_state_missing = {"whatsapp_delivered_at": None}
    session_state_ready = {"whatsapp_delivered_at": datetime.now(timezone.utc)}

    first_defer = _compute_whatsapp_status_order_delay(
        runtime_variables=runtime_variables,
        session_state=session_state_missing,
    )
    assert first_defer is not None

    second_defer = _compute_whatsapp_status_order_delay(
        runtime_variables=runtime_variables,
        session_state=session_state_ready,
    )
    assert second_defer is None


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
    assert runtime_variables["variables"]["api_body"] == {"ok": True}
    assert runtime_variables["api_call_last_result"]["body"] == {"ok": True}
    assert runtime_variables["api_call_last_result"]["headers"] == {"x-a": "b"}


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


@pytest.mark.asyncio
async def test_generate_file_enqueue_success_maps_runtime(monkeypatch) -> None:
    runtime_variables = {"variables": {"customs": {"valor_recebido": 114}, "payload": {"external_id": "x1"}}}
    component = {
        "parameters": {
            "destination_type": "sftp",
            "host": "storage.otima.io",
            "port": 45884,
            "user": "usr",
            "password": "pwd",
            "destination_path": "exports",
            "file_name_template": "arquivo_teste",
            "format_type": "csv",
            "write_mode": "overwrite",
            "scheduling_run_mode": "agendado",
            "scheduling_date": "2099-01-01",
            "scheduling_time_agendado": "00:00",
            "include_header": True,
            "fields_mapping": [
                {"column": "external_id", "source": "payload.external_id", "data_type": "text"},
                {"column": "valor", "source": "customs.valor_recebido", "data_type": "integer"},
            ],
            "response": {
                "status": "gf_status",
                "path": "gf_path",
                "file_name": "gf_name",
                "md5": "gf_md5",
                "error": "gf_error",
            },
            "output_var_prefix": "arquivo",
        }
    }

    captured = {}

    async def fake_upsert_job_and_buffer_row(
        db_session, *, workspace_uuid, flow_id, component_ref_id, session_id, config, row_payload
    ):  # noqa: ANN001
        captured.update(
            {
                "workspace_uuid": workspace_uuid,
                "flow_id": flow_id,
                "component_ref_id": component_ref_id,
                "session_id": session_id,
                "config": config,
                "row_payload": row_payload,
            }
        )
        return {"job_id": "job-123", "queued_row": True, "mode": "agendado", "next_run_at": "2099-01-01T03:00:00+00:00"}

    monkeypatch.setattr(workflow_m2_service, "upsert_job_and_buffer_row", fake_upsert_job_and_buffer_row)
    monkeypatch.setattr(workflow_m2_service, "get_current_workspace_uuid", lambda: "ba7eb0ec-e565-447c-8c11-8f870cf72a60")

    branch = await _run_generate_file(
        db_session=None,
        flow_uuid="2cb9482a-131e-4b2a-8507-484745661836",
        component=component,
        runtime_variables=runtime_variables,
        session_id=77,
    )

    assert branch == "success"
    assert captured["workspace_uuid"] is not None
    assert captured["flow_id"] == "2cb9482a-131e-4b2a-8507-484745661836"
    assert captured["session_id"] == 77

    result = runtime_variables["generate_file_last_result"]
    customs = runtime_variables["variables"]["customs"]
    assert result["destination_type"] == "sftp"
    assert result["status"] == "queued"
    assert result["job_id"] == "job-123"
    assert customs["gf_status"] == "queued"
    assert customs["gf_path"] is None
    assert customs["gf_name"] == result["file_name"]
    assert customs["gf_md5"] is None
    assert customs["gf_error"] is None
    assert customs["arquivo"]["job_id"] == "job-123"


@pytest.mark.asyncio
async def test_generate_file_missing_mapping_raises() -> None:
    runtime_variables = {"variables": {"customs": {}, "payload": {}}}
    component = {
        "parameters": {
            "destination_type": "sftp",
            "host": "storage.otima.io",
            "port": 45884,
            "user": "usr",
            "password": "pwd",
            "destination_path": "exports",
            "file_name_template": "arquivo_teste",
            "format_type": "csv",
        }
    }

    try:
        await _run_generate_file(
            db_session=None,
            flow_uuid="2cb9482a-131e-4b2a-8507-484745661836",
            component=component,
            runtime_variables=runtime_variables,
            session_id=10,
        )
        assert False, "esperava exceção por mapping ausente"
    except Exception as exc:
        assert getattr(exc, "code", "") == "generate_file_missing_mapping"


@pytest.mark.asyncio
async def test_generate_file_resolves_dot_paths_inside_templates(monkeypatch) -> None:
    runtime_variables = {
        "variables": {
            "payload": {"file": {"content": {"cpf": "09089978634"}}},
            "customs": {"api_body": {"name": "Ana", "phone": "+5511999999999"}},
        }
    }
    component = {
        "parameters": {
            "destination_type": "sftp",
            "host": "storage.otima.io",
            "port": 45884,
            "user": "usr",
            "password": "pwd",
            "destination_path": "exports",
            "file_name_template": "arquivo_teste",
            "format_type": "csv",
            "write_mode": "overwrite",
            "scheduling_run_mode": "agendado",
            "scheduling_date": "2099-01-01",
            "scheduling_time_agendado": "00:00",
            "include_header": True,
            "mapping": [
                {"key": "cpf", "value": "{{payload.file.content.cpf}}"},
                {"key": "telefone", "value": "{{customs.api_body.phone}}"},
                {"key": "nome", "value": "{{api_body.name}}"},
            ],
        }
    }

    captured = {}

    async def fake_upsert_job_and_buffer_row(
        db_session, *, workspace_uuid, flow_id, component_ref_id, session_id, config, row_payload
    ):  # noqa: ANN001
        captured["row_payload"] = row_payload
        return {"job_id": "job-123", "queued_row": True, "mode": "agendado", "next_run_at": "2099-01-01T03:00:00+00:00"}

    monkeypatch.setattr(workflow_m2_service, "upsert_job_and_buffer_row", fake_upsert_job_and_buffer_row)
    monkeypatch.setattr(workflow_m2_service, "get_current_workspace_uuid", lambda: "ba7eb0ec-e565-447c-8c11-8f870cf72a60")

    branch = await _run_generate_file(
        db_session=None,
        flow_uuid="2cb9482a-131e-4b2a-8507-484745661836",
        component=component,
        runtime_variables=runtime_variables,
        session_id=77,
    )

    assert branch == "success"
    assert captured["row_payload"]["cpf"] == "09089978634"
    assert captured["row_payload"]["telefone"] == "+5511999999999"
    assert captured["row_payload"]["nome"] == "Ana"


@pytest.mark.asyncio
async def test_generate_file_uses_api_call_last_result_body_as_fallback(monkeypatch) -> None:
    runtime_variables = {
        "variables": {"payload": {"file": {"content": {"cpf": "09089978634"}}}, "customs": {}},
        "api_call_last_result": {"body": {"name": "Bruna", "phone": "+5511988887777"}},
    }
    component = {
        "parameters": {
            "destination_type": "sftp",
            "host": "storage.otima.io",
            "port": 45884,
            "user": "usr",
            "password": "pwd",
            "destination_path": "exports",
            "file_name_template": "arquivo_teste",
            "format_type": "csv",
            "write_mode": "overwrite",
            "scheduling_run_mode": "agendado",
            "scheduling_date": "2099-01-01",
            "scheduling_time_agendado": "00:00",
            "include_header": True,
            "mapping": [
                {"key": "cpf", "value": "{{file.content.cpf}}"},
                {"key": "telefone", "value": "{{api_body.phone}}"},
                {"key": "nome", "value": "{{api_body.name}}"},
            ],
        }
    }

    captured = {}

    async def fake_upsert_job_and_buffer_row(
        db_session, *, workspace_uuid, flow_id, component_ref_id, session_id, config, row_payload
    ):  # noqa: ANN001
        captured["row_payload"] = row_payload
        return {"job_id": "job-123", "queued_row": True, "mode": "agendado", "next_run_at": "2099-01-01T03:00:00+00:00"}

    monkeypatch.setattr(workflow_m2_service, "upsert_job_and_buffer_row", fake_upsert_job_and_buffer_row)
    monkeypatch.setattr(workflow_m2_service, "get_current_workspace_uuid", lambda: "ba7eb0ec-e565-447c-8c11-8f870cf72a60")

    branch = await _run_generate_file(
        db_session=None,
        flow_uuid="2cb9482a-131e-4b2a-8507-484745661836",
        component=component,
        runtime_variables=runtime_variables,
        session_id=77,
    )

    assert branch == "success"
    assert captured["row_payload"]["cpf"] == "09089978634"
    assert captured["row_payload"]["telefone"] == "+5511988887777"
    assert captured["row_payload"]["nome"] == "Bruna"


@pytest.mark.asyncio
async def test_intelligent_agent_maps_schema_output_to_customs(monkeypatch) -> None:
    runtime_variables = {"variables": {"payload": {"valor": 144}, "customs": {}}}
    component = {
        "ref_id": "ia-1",
        "component_id": "intelligent_agent",
        "parameters": {
            "llm": {"id": "gpt-4.1-mini", "name": "gpt-4.1-mini"},
            "user_prompt": "Qual a raiz quadrada de {{payload.valor}}?",
            "exit_function": {
                "json": {"resultado_raiz_quadrada": None},
                "output_var_name": "dados_ia",
            },
            "bargein": [],
        },
    }

    async def fake_fetch_workspace_api_key(db_session, *, workspace_uuid):  # noqa: ANN001
        return "workspace-key"

    def fake_execute_otima_llm_prompt(**kwargs):  # noqa: ANN003
        assert kwargs["model"] == "gpt-4.1-mini"
        assert "Qual a raiz quadrada de 144?" in kwargs["user_prompt"]
        return {
            "status_code": 200,
            "endpoint": "http://llm.test/v1/chat/completions",
            "raw_text": '{"resultado_raiz_quadrada": 12}',
            "parsed_json": {"resultado_raiz_quadrada": 12},
            "response_json": {},
        }

    monkeypatch.setattr(workflow_m2_service, "fetch_workspace_otima_billing_api_key", fake_fetch_workspace_api_key)
    monkeypatch.setattr(workflow_m2_service, "execute_otima_llm_prompt", fake_execute_otima_llm_prompt)
    monkeypatch.setattr(workflow_m2_service, "get_current_workspace_uuid", lambda: "ba7eb0ec-e565-447c-8c11-8f870cf72a60")

    branch = await _run_intelligent_agent(
        db_session=None,
        component=component,
        runtime_variables=runtime_variables,
    )

    assert branch is None
    assert runtime_variables["variables"]["customs"]["dados_ia"]["resultado_raiz_quadrada"] == 12
    assert runtime_variables["variables"]["customs"]["resultado_raiz_quadrada"] == 12
    assert runtime_variables["intelligent_agent_last_result"]["result"]["resultado_raiz_quadrada"] == 12


@pytest.mark.asyncio
async def test_intelligent_agent_raises_when_llm_response_not_json(monkeypatch) -> None:
    runtime_variables = {"variables": {"payload": {"valor": 144}, "customs": {}}}
    component = {
        "ref_id": "ia-1",
        "component_id": "intelligent_agent",
        "parameters": {
            "llm": {"id": "gpt-4.1-mini", "name": "gpt-4.1-mini"},
            "user_prompt": "Qual a raiz quadrada de {{payload.valor}}?",
            "exit_function": {"json": {"resultado_raiz_quadrada": None}, "output_var_name": "dados_ia"},
            "bargein": ["yes"],
        },
    }

    async def fake_fetch_workspace_api_key(db_session, *, workspace_uuid):  # noqa: ANN001
        return "workspace-key"

    def fake_execute_otima_llm_prompt(**kwargs):  # noqa: ANN003
        return {
            "status_code": 200,
            "endpoint": "http://llm.test/v1/chat/completions",
            "raw_text": "12",
            "parsed_json": None,
            "response_json": {},
        }

    monkeypatch.setattr(workflow_m2_service, "fetch_workspace_otima_billing_api_key", fake_fetch_workspace_api_key)
    monkeypatch.setattr(workflow_m2_service, "execute_otima_llm_prompt", fake_execute_otima_llm_prompt)
    monkeypatch.setattr(workflow_m2_service, "get_current_workspace_uuid", lambda: "ba7eb0ec-e565-447c-8c11-8f870cf72a60")

    with pytest.raises(Exception) as exc:
        await _run_intelligent_agent(
            db_session=None,
            component=component,
            runtime_variables=runtime_variables,
        )

    assert getattr(exc.value, "code", "") == "intelligent_agent_invalid_response"
