from __future__ import annotations

from datetime import datetime, timezone

import pytest
import app.services.workflow_m2_service as workflow_m2_service
from app.services.workflow_m2_service import (
    WorkflowExecutionError,
    _build_create_contact_records,
    _blocking_stop_reason_for_component,
    _clear_blocking_execution,
    _compute_whatsapp_status_order_delay,
    _compute_frozen_until,
    _extract_send_with_whatsapp_number_policies,
    _extract_send_whatsapp_interactive_number_policies,
    _extract_whatsapp_status_signature_from_runtime,
    _extract_whatsapp_status_from_runtime,
    _extract_whatsapp_message_branch_key_from_runtime,
    _extract_send_with_whatsapp_numbers,
    _resolve_send_whatsapp_interactive_branch_label,
    _inject_contact_runtime_scope,
    _inject_callback_runtime_scope,
    _inject_system_runtime_scope,
    _is_send_with_whatsapp_limit_exhausted,
    _mark_blocking_execution,
    _normalize_contact_extra_data,
    _prepare_send_with_whatsapp_contact_member,
    _read_blocking_stop_reason,
    _read_whatsapp_last_preempt_signature,
    _read_whatsapp_resume_cursor,
    _render_value,
    _run_api_call,
    _run_cache_get,
    _run_cache_post,
    _run_code_editor,
    _run_condition,
    _run_generate_file,
    _run_intelligent_agent,
    _read_loop_guard_repeat_threshold,
    _resolve_send_with_dialer_branch_label,
    _run_process_dialer_response,
    _run_run_flow,
    _run_process_whatsapp_response,
    _run_set_variables,
    _set_synthetic_whatsapp_status_payload,
    _set_whatsapp_last_preempt_signature,
    _set_whatsapp_resume_cursor,
    _set_dialer_resume_cursor,
    _read_dialer_resume_cursor,
    _register_loop_guard_step,
    _reset_loop_guard_counter,
    _ensure_variables,
    _resolve_code_editor_branch,
    _resolve_component_exception_branch_label,
    _should_preempt_to_whatsapp_resume_cursor,
    _should_resume_dialer_blocking_execution,
    _should_resume_run_flow_blocking_execution,
    _should_resume_whatsapp_blocking_execution,
    _set_run_flow_waiting,
)


def test_blocking_stop_reason_for_whatsapp_components() -> None:
    assert _blocking_stop_reason_for_component("send_with_whatsapp") == "blocked_send_with_whatsapp"
    assert _blocking_stop_reason_for_component("send_whatsapp_interactive") == "blocked_send_whatsapp_interactive"
    assert _blocking_stop_reason_for_component("process_whatsapp_response") == "blocked_process_whatsapp_response"
    assert _blocking_stop_reason_for_component("send_with_dialer") == "blocked_send_with_dialer"
    assert _blocking_stop_reason_for_component("process_dialer_response") == "blocked_process_dialer_response"
    assert _blocking_stop_reason_for_component("run_flow") == "blocked_run_flow"
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


def test_loop_guard_counter_roundtrip() -> None:
    runtime_variables: dict[str, object] = {}
    first = _register_loop_guard_step(runtime_variables, transition_signature="card-a->card-b")
    second = _register_loop_guard_step(runtime_variables, transition_signature="card-b->card-c")
    assert first == 1
    assert second == 2
    assert runtime_variables["workflow_v2"]["loop_guard"]["last_transition_signature"] == "card-b->card-c"
    _reset_loop_guard_counter(runtime_variables)
    assert runtime_variables["workflow_v2"]["loop_guard"]["continuous_steps"] == 0


def test_read_loop_guard_repeat_threshold_bounds_values() -> None:
    class _Settings:
        workflow_m2_loop_guard_repeat_threshold = "0"

    assert _read_loop_guard_repeat_threshold(_Settings()) == 1

    _Settings.workflow_m2_loop_guard_repeat_threshold = "999999"
    assert _read_loop_guard_repeat_threshold(_Settings()) == 5000


def test_ensure_variables_seeds_callback_and_file_content_scopes() -> None:
    runtime_variables: dict[str, object] = {"input_payload": {"a": 1}}
    variables = _ensure_variables(runtime_variables)
    assert isinstance(variables["payload"], dict)
    assert isinstance(variables["customs"], dict)
    assert isinstance(variables["callback"], dict)
    assert isinstance(variables["file"], dict)
    assert isinstance(variables["file"]["content"], dict)


def test_build_create_contact_records_renders_required_fields() -> None:
    component = {
        "parameters": {
            "mapping": [
                {"key": "identificador", "value": "{{api_body.id}}"},
                {"key": "endereço", "value": "{{api_body.phone}}"},
                {"key": "nome", "value": "{{api_body.name}}"},
                {"key": "origem", "value": "campanha_xyz"},
            ]
        }
    }
    scope = {
        "api_body": {
            "id": "10392279998",
            "phone": "5594975620806",
            "name": "Maria Antonieta Dos Reis",
        }
    }
    records = _build_create_contact_records(component=component, resolution_scope=scope)
    assert len(records) == 1
    assert records[0]["identifier"] == "10392279998"
    assert records[0]["address"] == "5594975620806"
    assert records[0]["full_name"] == "Maria Antonieta Dos Reis"
    assert records[0]["extras"]["origem"] == "campanha_xyz"


def test_build_create_contact_records_supports_list_values() -> None:
    component = {
        "parameters": {
            "mapping": [
                {"key": "identifier", "value": ["1001", "1002"]},
                {"key": "address", "value": ["551190000001", "551190000002"]},
            ]
        }
    }
    records = _build_create_contact_records(component=component, resolution_scope={})
    assert [item["identifier"] for item in records] == ["1001", "1002"]
    assert [item["address"] for item in records] == ["551190000001", "551190000002"]


def test_build_create_contact_records_raises_when_required_missing() -> None:
    component = {
        "parameters": {
            "mapping": [
                {"key": "identifier", "value": "1001"},
                {"key": "address", "value": ""},
            ]
        }
    }
    with pytest.raises(WorkflowExecutionError) as exc:
        _build_create_contact_records(component=component, resolution_scope={})
    assert exc.value.code == "create_contact_missing_required_fields"


def test_resolve_component_exception_branch_label_returns_exception_alias() -> None:
    definition = {
        "components": [
            {"ref_id": "card-1", "component_id": "create_contact"},
            {"ref_id": "card-2", "component_id": "finish_flow"},
            {"ref_id": "card-3", "component_id": "api_call"},
        ],
        "branches": [
            {"from": "card-1", "to": "card-2", "branch": "action_after_creating_contact"},
            {"from": "card-1", "to": "card-3", "branch": "exception_hgdxh542k"},
        ],
    }
    assert (
        _resolve_component_exception_branch_label(
            definition=definition,
            current_card_uuid="card-1",
        )
        == "exception_hgdxh542k"
    )


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


def test_extract_whatsapp_message_branch_key_from_runtime_interactive_id() -> None:
    runtime_variables = {
        "last_payload": {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.1",
                                        "interactive": {"button_reply": {"id": "OTIMO"}},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        }
    }
    assert _extract_whatsapp_message_branch_key_from_runtime(runtime_variables) == "otimo"


def test_extract_whatsapp_message_branch_key_from_runtime_text_fallback() -> None:
    runtime_variables = {
        "last_payload": {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.2",
                                        "text": {"body": "olá. bom dia!"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        }
    }
    assert _extract_whatsapp_message_branch_key_from_runtime(runtime_variables) == "ola_bom_dia"


def test_resolve_send_whatsapp_interactive_branch_label_uses_provider_and_key() -> None:
    component = {"parameters": {"whatsapp_interactive_config": {"selected_number": "1147371486"}}}
    runtime_variables = {
        "last_payload": {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"display_phone_number": "551147371486"},
                                "messages": [
                                    {
                                        "id": "wamid.3",
                                        "interactive": {"button_reply": {"id": "simular_emprestimo"}},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ],
        }
    }
    assert (
        _resolve_send_whatsapp_interactive_branch_label(
            component=component,
            runtime_variables=runtime_variables,
        )
        == "wic:1147371486:simular_emprestimo"
    )


def test_run_process_dialer_response_maps_status_to_branch() -> None:
    runtime_variables = {
        "last_payload": {
            "hangup": {
                "Disposition": "BUSY",
                "DialerClassifierStatus": "",
                "Cause-txt": "",
            }
        }
    }
    branch = _run_process_dialer_response(
        component={"ref_id": "dialer-1"},
        runtime_variables=runtime_variables,
    )
    assert branch == "busy"
    assert runtime_variables["dialer_last_response"]["status"] == "busy"
    assert runtime_variables["dialer_last_response"]["branch"] == "busy"


def test_render_value_resolves_array_index_path() -> None:
    variables = {
        "dados_contato": [
            {"phone": "5511999990001"},
            {"phone": "5511999990002"},
        ]
    }
    rendered = _render_value(
        "Valor do segundo telefone: {{dados_contato[1].phone}}",
        variables,
    )
    assert rendered == "Valor do segundo telefone: 5511999990002"


def test_render_value_resolves_array_index_path_as_single_token() -> None:
    variables = {
        "dados_contato": [
            {"phone": "5511999990001"},
            {"phone": "5511999990002"},
        ]
    }
    rendered = _render_value("{{dados_contato[0].phone}}", variables)
    assert rendered == "5511999990001"


def test_render_value_resolves_bracket_string_key_path() -> None:
    variables = {"dados_contato": [{"phone": "5511999990003"}]}
    rendered = _render_value("{{dados_contato[0]['phone']}}", variables)
    assert rendered == "5511999990003"


def test_render_value_returns_none_or_empty_for_out_of_range_index() -> None:
    variables = {"dados_contato": [{"phone": "5511999990001"}]}
    assert _render_value("{{dados_contato[3].phone}}", variables) is None
    rendered = _render_value(
        "Valor do segundo telefone: {{dados_contato[3].phone}}",
        variables,
    )
    assert rendered == "Valor do segundo telefone: "


def test_resolve_send_with_dialer_branch_label_maps_status_to_branch() -> None:
    runtime_variables = {
        "last_payload": {
            "hangup": {
                "Disposition": "NO ANSWER",
                "DialerClassifierStatus": "",
                "Cause-txt": "",
            }
        }
    }
    branch = _resolve_send_with_dialer_branch_label(
        component={"ref_id": "dialer-send-1"},
        runtime_variables=runtime_variables,
    )
    assert branch == "no_answer"
    assert runtime_variables["dialer_last_response"]["status"] == "no_answer"
    assert runtime_variables["dialer_last_response"]["branch"] == "no_answer"


def test_resolve_send_with_dialer_branch_label_returns_none_without_status() -> None:
    runtime_variables = {"last_payload": {"external_id": "generic-event"}}
    branch = _resolve_send_with_dialer_branch_label(
        component={"ref_id": "dialer-send-1"},
        runtime_variables=runtime_variables,
    )
    assert branch is None
    assert "dialer_last_response" not in runtime_variables


def test_extract_send_with_whatsapp_numbers_deduplicates_and_ignores_invalid() -> None:
    component = {
        "parameters": {
            "whatsapp_numbers_config": {
                "numbers": [
                    {"number": "1147371485"},
                    {"number": "1147371485"},
                    {"number": "1147371486"},
                    {"number": "   "},
                    {},
                    "invalid",
                ]
            }
        }
    }
    assert _extract_send_with_whatsapp_numbers(component) == ["1147371485", "1147371486"]


def test_extract_send_with_whatsapp_number_policies_normalizes_country_code() -> None:
    component = {
        "parameters": {
            "whatsapp_numbers_config": {
                "numbers": [
                    {"number": "551147371485", "percentual_consumo": 50},
                    {"number": "1147371486", "percentual_consumo": 30},
                ]
            }
        }
    }
    numbers, percentual_by_phone = _extract_send_with_whatsapp_number_policies(component)
    assert numbers == ["1147371485", "1147371486"]
    assert percentual_by_phone == {"1147371485": 50, "1147371486": 30}


def test_extract_send_whatsapp_interactive_number_policies_reads_percentual() -> None:
    component = {
        "parameters": {
            "whatsapp_interactive_config": {
                "numbers": [
                    {"number": "551147371485", "value": {"max_daily_rate_limit_consumption": 100}},
                    {"number": "1147371486", "value": {"max_daily_rate_limit_consumption": 35}},
                ]
            }
        }
    }
    numbers, percentual_by_phone = _extract_send_whatsapp_interactive_number_policies(component)
    assert numbers == ["1147371485", "1147371486"]
    assert percentual_by_phone == {"1147371485": 100, "1147371486": 35}


def test_is_send_with_whatsapp_limit_exhausted() -> None:
    assert _is_send_with_whatsapp_limit_exhausted({"linked_actuator": "whatsapp_without_limit"}) is True
    assert _is_send_with_whatsapp_limit_exhausted({"linked_actuator": "whatsapp_without_limit_by_rate_limit"}) is True
    assert _is_send_with_whatsapp_limit_exhausted({"linked_actuator": "whatsapp"}) is False
    assert _is_send_with_whatsapp_limit_exhausted(None) is False


def test_set_synthetic_whatsapp_status_payload_sets_limit_reached() -> None:
    runtime_variables: dict[str, object] = {}
    _set_synthetic_whatsapp_status_payload(
        runtime_variables,
        status="limit_reached",
        reason="send_with_whatsapp_limit_exhausted",
    )
    assert _extract_whatsapp_status_from_runtime(runtime_variables) == "limit_reached"


def test_normalize_contact_extra_data_accepts_dict_and_json_string() -> None:
    assert _normalize_contact_extra_data({"data_ocorrencia": "01/01/2026"}) == {"data_ocorrencia": "01/01/2026"}
    assert _normalize_contact_extra_data('{"data_ocorrencia":"10/05/2026"}') == {"data_ocorrencia": "10/05/2026"}
    assert _normalize_contact_extra_data("") == {}


def test_inject_contact_runtime_scope_sets_contact_extra() -> None:
    runtime_variables: dict[str, object] = {}
    _inject_contact_runtime_scope(
        runtime_variables=runtime_variables,
        contact_row={
            "contact_list_member_id": 10196,
            "contact_identifier": "70000700001",
            "contact_name": "Cliente 0001",
            "contact_full_name": "Cliente 0001",
            "contact_gender": None,
            "contact_country": None,
            "contact_province": None,
            "contact_city": None,
            "contact_birth_date": None,
            "contact_age": None,
            "contact_channel_type": "voice",
            "contact_channel_label": "tel1",
            "contact_channel_address": "5511900700001",
            "contact_channel_extra_data": {"data_ocorrencia": "01/01/2026"},
            "person_uuid": "0254807a-840b-4072-93a8-4193d5626fe7",
        },
    )
    variables = runtime_variables["variables"]
    assert variables["contact"]["identifier"] == "70000700001"
    assert variables["contact"]["extra"]["data_ocorrencia"] == "01/01/2026"
    assert variables["customs"]["contact"]["extra"]["data_ocorrencia"] == "01/01/2026"


def test_inject_callback_runtime_scope_sets_callback_builtin() -> None:
    runtime_variables: dict[str, object] = {
        "callback": {
            "event_name": "callback",
            "entity": "30392287848",
            "result": "success",
            "data": {"ticket_id": "abc-123"},
            "received_at": "2026-05-17T01:10:00+00:00",
        }
    }
    _inject_callback_runtime_scope(runtime_variables=runtime_variables)
    variables = runtime_variables["variables"]
    assert variables["callback"]["result"] == "success"
    assert variables["customs"]["callback"]["data"]["ticket_id"] == "abc-123"


def test_inject_system_runtime_scope_sets_whatsapp_payload_callback_and_file_content() -> None:
    runtime_variables: dict[str, object] = {
        "input_payload": {
            "object": "whatsapp_business_account",
            "external_id": "evt-001",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "referral": {
                                            "headline": "Honda Migoto",
                                            "source_id": "120247",
                                            "source_url": "https://instagram.com/p/abc",
                                            "source_type": "ad",
                                        }
                                    }
                                ],
                                "statuses": [{"status": "sent"}],
                            }
                        }
                    ]
                }
            ],
            "file": {"content": {"cpf": "09089978634", "nome": "Ana"}},
        },
        "callback": {
            "event_name": "callback",
            "entity": "30392287848",
            "result": "success",
            "data": {"protocol": "cbk-001"},
            "received_at": "2026-05-19T01:10:00+00:00",
        },
    }
    _inject_callback_runtime_scope(runtime_variables=runtime_variables)
    _inject_contact_runtime_scope(
        runtime_variables=runtime_variables,
        contact_row={
            "contact_list_member_id": 10196,
            "contact_identifier": "70000700001",
            "contact_name": "Cliente 0001",
            "contact_full_name": "Cliente 0001",
            "contact_gender": None,
            "contact_country": None,
            "contact_province": None,
            "contact_city": None,
            "contact_birth_date": None,
            "contact_age": None,
            "contact_channel_type": "voice",
            "contact_channel_label": "tel1",
            "contact_channel_address": "5511900700001",
            "contact_channel_extra_data": {"data_ocorrencia": "01/01/2026"},
            "person_uuid": "0254807a-840b-4072-93a8-4193d5626fe7",
        },
    )
    _inject_system_runtime_scope(
        runtime_variables=runtime_variables,
        session_state={
            "whatsapp_sent_at": None,
            "whatsapp_delivered_at": None,
            "whatsapp_read_at": None,
            "whatsapp_failed_at": None,
        },
        contact_row=None,
    )

    variables = runtime_variables["variables"]
    system = variables["system"]
    assert system["external_id"] == "evt-001"
    assert system["whatsapp"]["sent"] is True
    assert system["whatsapp.sent"] is True
    assert system["whatsapp"]["referral"]["head_line"] == "Honda Migoto"
    assert system["callback"]["result"] == "success"
    assert system["file"]["content"]["cpf"] == "09089978634"
    assert variables["file"]["content"]["nome"] == "Ana"


def test_run_run_flow_maps_callback_result_to_branch() -> None:
    runtime_variables = {
        "callback": {
            "event_name": "callback",
            "entity": "30392287848",
            "result": "unsuccess",
            "data": {"reason": "timeout"},
            "received_at": "2026-05-17T01:10:00+00:00",
        }
    }
    branch = _run_run_flow(component={"ref_id": "run-flow-1"}, runtime_variables=runtime_variables)
    assert branch == "unsuccess"
    assert runtime_variables["run_flow_last_callback"]["result"] == "unsuccess"


def test_run_run_flow_maps_hangup_result_to_branch() -> None:
    runtime_variables = {
        "callback": {
            "event_name": "hangup",
            "entity": "5511975620806",
            "result": "hangup",
            "data": {"disposition": "ANSWERED"},
            "received_at": "2026-06-11T22:58:01+00:00",
        }
    }
    branch = _run_run_flow(component={"ref_id": "run-flow-1"}, runtime_variables=runtime_variables)
    assert branch == "hangup"
    assert runtime_variables["run_flow_last_callback"]["event_name"] == "hangup"


def test_should_resume_run_flow_blocking_execution_only_with_new_callback() -> None:
    runtime_variables = {
        "workflow_v2": {
            "blocking_execution": True,
            "blocking_stop_reason": "blocked_run_flow",
        },
    }
    _set_run_flow_waiting(runtime_variables, card_cursor="run-1")
    assert _should_resume_run_flow_blocking_execution(runtime_variables) is False

    runtime_variables["callback"] = {
        "event_name": "callback",
        "entity": "30392287848",
        "result": "success",
        "data": {},
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    assert _should_resume_run_flow_blocking_execution(runtime_variables) is True


@pytest.mark.asyncio
async def test_prepare_send_with_whatsapp_contact_member_updates_runtime(monkeypatch) -> None:
    runtime_variables: dict[str, object] = {}
    component = {
        "parameters": {
            "whatsapp_numbers_config": {
                "numbers": [
                    {"number": "1147371485"},
                    {"number": "1147371486"},
                ]
            }
        }
    }

    async def fake_assign_whatsapp_routing_for_session(  # noqa: ANN001
        db_session,
        *,
        flow_uuid,
        session_id,
        numbers,
        percentual_by_phone,
    ):
        assert flow_uuid == "0300054c-5f39-4cda-ae88-fe993fd9044b"
        assert session_id == 101
        assert numbers == ["1147371485", "1147371486"]
        assert percentual_by_phone == {"1147371485": 0, "1147371486": 0}
        return {
            "contact_list_member_id": 10,
            "ani": "1147371485",
            "linked_actuator": "whatsapp",
            "mode": "balanced_ani",
        }

    monkeypatch.setattr(
        workflow_m2_service,
        "assign_whatsapp_routing_for_session",
        fake_assign_whatsapp_routing_for_session,
    )

    await _prepare_send_with_whatsapp_contact_member(
        db_session=None,
        flow_uuid="0300054c-5f39-4cda-ae88-fe993fd9044b",
        session_id=101,
        component=component,
        runtime_variables=runtime_variables,
    )

    route_data = runtime_variables["send_with_whatsapp_routing"]
    assert route_data["numbers"] == ["1147371485", "1147371486"]
    assert route_data["assignment"]["ani"] == "1147371485"


def test_should_resume_whatsapp_blocking_execution_when_status_or_message_or_pending() -> None:
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

    runtime_variables["last_payload"] = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [{"text": {"body": "OTIMO"}}],
                        }
                    }
                ]
            }
        ],
    }
    assert _should_resume_whatsapp_blocking_execution(runtime_variables) is True

    runtime_variables["last_payload"] = {"external_id": "generic-event"}
    assert _should_resume_whatsapp_blocking_execution(runtime_variables) is False
    assert _should_resume_whatsapp_blocking_execution(
        runtime_variables,
        has_pending_whatsapp_events=True,
    ) is True

    runtime_variables["workflow_v2"]["blocking_stop_reason"] = "blocked_send_with_dialer"
    assert _should_resume_whatsapp_blocking_execution(runtime_variables) is False


def test_should_resume_dialer_blocking_execution_only_when_status_available() -> None:
    runtime_variables = {
        "workflow_v2": {
            "blocking_execution": True,
            "blocking_stop_reason": "blocked_send_with_dialer",
        },
        "last_payload": {
            "hangup": {
                "Disposition": "ANSWERED",
            },
        },
    }
    assert _should_resume_dialer_blocking_execution(runtime_variables) is True

    runtime_variables["last_payload"] = {"external_id": "generic-event"}
    assert _should_resume_dialer_blocking_execution(runtime_variables) is False


def test_set_and_read_whatsapp_resume_cursor_roundtrip() -> None:
    runtime_variables: dict[str, object] = {}
    assert _read_whatsapp_resume_cursor(runtime_variables) is None
    _set_whatsapp_resume_cursor(runtime_variables, process_card_cursor="card-process-1")
    assert _read_whatsapp_resume_cursor(runtime_variables) == "card-process-1"


def test_set_and_read_dialer_resume_cursor_roundtrip() -> None:
    runtime_variables: dict[str, object] = {}
    assert _read_dialer_resume_cursor(runtime_variables) is None
    _set_dialer_resume_cursor(runtime_variables, process_card_cursor="card-dialer-process-1")
    assert _read_dialer_resume_cursor(runtime_variables) == "card-dialer-process-1"


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


def test_resolve_code_editor_branch_redirects_to_exception_on_runtime_error() -> None:
    runtime_variables: dict[str, object] = {}
    resolved = _resolve_code_editor_branch(
        branch_label=None,
        branch_labels=["success", "exception_abc123"],
        runtime_variables=runtime_variables,  # type: ignore[arg-type]
        execution_error=WorkflowExecutionError("code_editor_runtime_error", "boom"),
    )
    assert resolved == "exception_abc123"
    assert runtime_variables["code_editor_last_error"]["code"] == "code_editor_runtime_error"


def test_resolve_code_editor_branch_redirects_to_exception_on_unmapped_branch() -> None:
    runtime_variables: dict[str, object] = {}
    resolved = _resolve_code_editor_branch(
        branch_label="failure",
        branch_labels=["success", "exception_abc123"],
        runtime_variables=runtime_variables,  # type: ignore[arg-type]
    )
    assert resolved == "exception_abc123"
    assert "code_editor_last_error" not in runtime_variables


def test_code_editor_branches_payload_maps_failure_to_exception_alias() -> None:
    runtime_variables = {"variables": {"customs": {}}}
    component = {
        "parameters": {
            "timeout_ms": 500,
            "code": """
export default async function main(ctx) {
  return { branch: ctx.branches.failure, payload: { ok: false } };
}
""",
        }
    }
    branch = _run_code_editor(
        component=component,
        runtime_variables=runtime_variables,
        branch_labels=["success", "exception_6509b0nud"],
    )
    assert branch == "exception_6509b0nud"


def test_resolve_code_editor_branch_raises_when_unmapped_and_no_exception() -> None:
    runtime_variables: dict[str, object] = {}
    with pytest.raises(WorkflowExecutionError) as exc:
        _resolve_code_editor_branch(
            branch_label="failure",
            branch_labels=["success"],
            runtime_variables=runtime_variables,  # type: ignore[arg-type]
        )
    assert exc.value.code == "code_editor_branch_not_mapped"


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
async def test_intelligent_agent_resolves_runtime_placeholders_without_payload_prefix(monkeypatch) -> None:
    runtime_variables = {
        "variables": {
            "payload": {
                "file": {
                    "content": {
                        "data_ocorrencia": "01/01/2026",
                    }
                }
            },
            "customs": {},
        }
    }
    component = {
        "ref_id": "ia-2",
        "component_id": "intelligent_agent",
        "parameters": {
            "llm": {"id": "gpt-4.1-mini", "name": "gpt-4.1-mini"},
            "user_prompt": "Data em runtime: {{file.content.data_ocorrencia}}",
            "exit_function": {
                "json": {"ok": None},
                "output_var_name": "dados_ia",
            },
        },
    }

    async def fake_fetch_workspace_api_key(db_session, *, workspace_uuid):  # noqa: ANN001
        return "workspace-key"

    def fake_execute_otima_llm_prompt(**kwargs):  # noqa: ANN003
        assert "Data em runtime: 01/01/2026" in kwargs["user_prompt"]
        return {
            "status_code": 200,
            "endpoint": "http://llm.test/v1/chat/completions",
            "raw_text": '{"ok": true}',
            "parsed_json": {"ok": True},
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
    assert runtime_variables["variables"]["customs"]["dados_ia"]["ok"] is True


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


class _FakeExecuteResult:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def first(self) -> object | None:
        return self._row


class _FakeDbSession:
    def __init__(self, *, rows: list[object | None] | None = None, fail_on_call: int | None = None) -> None:
        self.rows = rows or []
        self.fail_on_call = fail_on_call
        self.calls: list[dict[str, object]] = []
        self._execute_count = 0

    async def execute(self, statement, params=None):  # noqa: ANN001
        self._execute_count += 1
        if self.fail_on_call is not None and self._execute_count == self.fail_on_call:
            raise RuntimeError("db failure")
        self.calls.append(
            {
                "statement": str(statement),
                "params": params or {},
            }
        )
        row = self.rows.pop(0) if self.rows else None
        return _FakeExecuteResult(row)


@pytest.mark.asyncio
async def test_run_cache_post_persists_payload_and_returns_proximo() -> None:
    db_session = _FakeDbSession()
    runtime_variables = {
        "variables": {
            "payload": {"contact_id": "abc-123", "name": "Deivid"},
            "customs": {},
        }
    }
    component = {
        "ref_id": "15fd9afb-5d57-4601-b9d3-c73985e2f2dc",
        "component_id": "cache_post",
        "parameters": {
            "name": "dados_cliente",
            "primary_key_field": "identificador",
            "ttl_days": "10",
            "mapping": [
                {"key": "identificador", "value": "{{payload.contact_id}}"},
                {"key": "nome", "value": "{{payload.name}}"},
            ],
        },
    }

    branch = await _run_cache_post(
        db_session=db_session,
        flow_uuid="190a9c32-a8af-457b-b954-81af70704f00",
        component=component,
        runtime_variables=runtime_variables,
    )

    assert branch == "proximo"
    assert len(db_session.calls) == 2
    assert runtime_variables["cache_post_last_result"]["name"] == "dados_cliente"
    assert runtime_variables["cache_post_last_result"]["cache_key"] == "abc-123"
    assert runtime_variables["cache_post_last_result"]["ttl_days"] == 10
    assert runtime_variables["cache_post_last_result"]["data"]["nome"] == "Deivid"


@pytest.mark.asyncio
async def test_run_cache_get_found_routes_encontrado_and_sets_output_var() -> None:
    db_session = _FakeDbSession(rows=[({"nome": "Maria", "identificador": "77"},)])
    runtime_variables = {"variables": {"payload": {}, "customs": {}}}
    definition = {
        "components": [
            {
                "ref_id": "f51c9448-9aea-46db-b1ea-c3fce56f964b",
                "component_id": "cache_post",
                "parameters": {"name": "dados_cliente", "mapping": []},
            }
        ]
    }
    component = {
        "ref_id": "6f05349f-458b-4fa5-b0eb-35b594f5cd1d",
        "component_id": "cache_get",
        "parameters": {
            "name": "dados_cliente",
            "key": "77",
            "output_var": "cache_hit",
        },
    }

    branch = await _run_cache_get(
        db_session=db_session,
        definition=definition,
        flow_uuid="190a9c32-a8af-457b-b954-81af70704f00",
        component=component,
        runtime_variables=runtime_variables,
        branch_labels=["encontrado", "nao_encontrado", "exception"],
    )

    assert branch == "encontrado"
    assert runtime_variables["variables"]["customs"]["cache_hit"]["nome"] == "Maria"
    assert runtime_variables["cache_get_last_result"]["found"] is True


@pytest.mark.asyncio
async def test_run_cache_get_not_found_without_branch_raises() -> None:
    db_session = _FakeDbSession(rows=[None])
    runtime_variables = {"variables": {"payload": {}, "customs": {}}}
    definition = {
        "components": [
            {
                "ref_id": "dc757bba-c9be-45d6-b797-9bf432a6f3e4",
                "component_id": "cache_post",
                "parameters": {"name": "dados_cliente", "mapping": []},
            }
        ]
    }
    component = {
        "ref_id": "f0ea3490-aeba-4a5d-b388-8dd2f5912739",
        "component_id": "cache_get",
        "parameters": {
            "name": "dados_cliente",
            "key": "nao-existe",
        },
    }

    with pytest.raises(WorkflowExecutionError) as exc:
        await _run_cache_get(
            db_session=db_session,
            definition=definition,
            flow_uuid="190a9c32-a8af-457b-b954-81af70704f00",
            component=component,
            runtime_variables=runtime_variables,
            branch_labels=["encontrado", "exception"],
        )

    assert exc.value.code == "cache_get.not_found"


@pytest.mark.asyncio
async def test_run_cache_get_raises_when_name_not_configured() -> None:
    db_session = _FakeDbSession(rows=[None])
    runtime_variables = {"variables": {"payload": {}, "customs": {}}}
    definition = {"components": []}
    component = {
        "ref_id": "9ef4f096-469f-41d4-8f95-8e20f24314f4",
        "component_id": "cache_get",
        "parameters": {
            "name": "dados_cliente",
            "key": "abc",
        },
    }

    with pytest.raises(WorkflowExecutionError) as exc:
        await _run_cache_get(
            db_session=db_session,
            definition=definition,
            flow_uuid="190a9c32-a8af-457b-b954-81af70704f00",
            component=component,
            runtime_variables=runtime_variables,
            branch_labels=["encontrado", "nao_encontrado", "exception"],
        )

    assert exc.value.code == "cache_get.cache_name_not_configured"
