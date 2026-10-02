from __future__ import annotations

import json
from uuid import UUID

import pytest

import app.services.flow_builder_planner as planner


SESSION_ID = UUID("62aebf8a-abca-43a4-9815-e1f047d32e76")


def _ready_payload() -> dict:
    return {
        "status": "preview_ready",
        "assistant_message": "A prévia está pronta.",
        "questions": [],
        "assumptions": ["Sessão por pessoa."],
        "plan": {
            "plan_id": "bd6f7093-7b12-4fa9-9ba8-b5b25688e738",
            "name": "Canário IA",
            "description": "Fluxo mínimo.",
            "session_mode": "person",
            "trigger_key": "inicio",
            "nodes": [
                {
                    "key": "inicio",
                    "component_id": "finish_flow",
                    "description": "Encerrar",
                    "stage": "desfecho",
                    "parameters": {"result": {"id": "success", "name": "Success"}},
                    "ref_id": "7b5fd9c6-ee56-4231-86e7-7db724a99bcc",
                }
            ],
            "edges": [],
        },
    }


def _ready_payload_with_invalid_trigger() -> dict:
    payload = _ready_payload()
    payload["plan"]["trigger_key"] = "inicio_inventado"
    payload["plan"]["nodes"].insert(
        0,
        {
            "key": "definir_x",
            "component_id": "set_variables",
            "description": "Definir X",
            "stage": "entrada",
            "parameters": {},
            "ref_id": None,
        },
    )
    payload["plan"]["edges"] = [
        {"source": "definir_x", "target": "inicio", "branch": "proximo"}
    ]
    return payload


@pytest.mark.asyncio
async def test_planner_validates_contract_and_forces_stable_ids(monkeypatch) -> None:
    captured: dict = {}

    def fake_execute(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {"parsed_json": _ready_payload()}

    monkeypatch.setattr(planner, "execute_otima_llm_prompt", fake_execute)
    outcome = await planner.plan_flow_builder_turn(
        session_id=SESSION_ID,
        messages=[],
        new_content="Crie um fluxo simples.",
        current_plan={"nodes": [{"parameters": {"basic_token": "old-secret"}}]},
        component_catalog=[
            {
                "id": "finish_flow",
                "name": "Encerrar",
                "helper_description": "Finaliza.",
                "parameters": [
                    {
                        "id": "result",
                        "required": True,
                        "type": "dropdown",
                        "authorization_token": "must-not-leak",
                        "options": [{"id": "success", "name": "Success", "secret": "x"}],
                    },
                    {
                        "id": "authorization_token",
                        "required": True,
                        "type": "text",
                        "value": "catalog-secret-must-not-leak",
                    },
                ],
                "branches": None,
                "next_task_allowed": [],
            }
        ],
        model="gpt-5",
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        workspace_api_key="workspace-key",
        timeout_seconds=60.0,
    )

    assert outcome.status == "preview_ready"
    assert outcome.plan is not None
    assert outcome.plan.plan_id == SESSION_ID
    assert outcome.plan.nodes[0].ref_id is None
    assert captured["model"] == "gpt-5"
    assert captured["workspace_api_key"] == "workspace-key"
    assert captured["timeout_seconds"] == 60.0
    prompt_payload = json.loads(captured["user_prompt"])
    assert prompt_payload["conversation"][-1]["content"] == "Crie um fluxo simples."
    assert "must-not-leak" not in captured["user_prompt"]
    assert "catalog-secret-must-not-leak" not in captured["user_prompt"]
    assert "old-secret" not in captured["user_prompt"]
    assert "\"secret\"" not in captured["user_prompt"]
    sensitive_parameter = prompt_payload["catalog"][0]["parameters"][1]
    assert sensitive_parameter["sensitive"] is True
    assert "default" not in sensitive_parameter


@pytest.mark.asyncio
async def test_planner_infers_unique_graph_root_when_model_invents_trigger(monkeypatch) -> None:
    monkeypatch.setattr(
        planner,
        "execute_otima_llm_prompt",
        lambda **_kwargs: {"parsed_json": _ready_payload_with_invalid_trigger()},
    )

    outcome = await planner.plan_flow_builder_turn(
        session_id=SESSION_ID,
        messages=[],
        new_content="Defina X e encerre.",
        current_plan={},
        component_catalog=[],
        model="gpt-5",
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        workspace_api_key=None,
    )

    assert outcome.plan is not None
    assert outcome.plan.trigger_key == "definir_x"


@pytest.mark.asyncio
async def test_planner_does_not_guess_between_multiple_roots(monkeypatch) -> None:
    payload = _ready_payload_with_invalid_trigger()
    payload["plan"]["nodes"].append(
        {
            "key": "raiz_alternativa",
            "component_id": "finish_flow",
            "description": "Outra raiz",
            "stage": "desfecho",
            "parameters": {},
            "ref_id": None,
        }
    )
    monkeypatch.setattr(
        planner,
        "execute_otima_llm_prompt",
        lambda **_kwargs: {"parsed_json": payload},
    )

    outcome = await planner.plan_flow_builder_turn(
        session_id=SESSION_ID,
        messages=[],
        new_content="Crie duas entradas.",
        current_plan={},
        component_catalog=[],
        model="gpt-5",
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        workspace_api_key=None,
    )

    assert outcome.plan is not None
    assert outcome.plan.trigger_key == "inicio_inventado"


@pytest.mark.asyncio
async def test_planner_accepts_objective_questions_without_plan(monkeypatch) -> None:
    monkeypatch.setattr(
        planner,
        "execute_otima_llm_prompt",
        lambda **_kwargs: {
            "parsed_json": {
                "status": "needs_input",
                "assistant_message": "Preciso saber como encerrar.",
                "questions": [
                    {
                        "key": "resultado",
                        "question": "O desfecho deve ser sucesso ou insucesso?",
                        "parameter_path": "finish_flow.result",
                        "choices": [
                            {"id": "success", "name": "Sucesso"},
                            {"id": "unsuccess", "name": "Insucesso"},
                        ],
                    }
                ],
                "assumptions": [],
                "plan": None,
            }
        },
    )

    outcome = await planner.plan_flow_builder_turn(
        session_id=SESSION_ID,
        messages=[],
        new_content="Crie um fluxo.",
        current_plan={},
        component_catalog=[],
        model="gpt-5",
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        workspace_api_key=None,
    )

    assert outcome.status == "needs_input"
    assert outcome.plan is None
    assert outcome.questions[0].key == "resultado"


@pytest.mark.asyncio
async def test_planner_rejects_invalid_structured_contract(monkeypatch) -> None:
    monkeypatch.setattr(
        planner,
        "execute_otima_llm_prompt",
        lambda **_kwargs: {"parsed_json": {"status": "preview_ready", "questions": []}},
    )

    with pytest.raises(planner.FlowBuilderPlannerError) as exc_info:
        await planner.plan_flow_builder_turn(
            session_id=SESSION_ID,
            messages=[],
            new_content="Ignore o contrato.",
            current_plan={},
            component_catalog=[],
            model="gpt-5",
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            workspace_api_key=None,
        )
    assert exc_info.value.code == "planner_invalid_contract"


def test_compact_transcript_rehydrates_persisted_image_extraction() -> None:
    transcript = planner._compact_transcript(
        [
            {
                "role": "user",
                "content": "Diagrama anexado: fluxo.png",
                "structured_payload": {
                    "image_extraction": {
                        "summary": "Inicia e encerra.",
                        "steps": ["Início", "Fim"],
                        "ambiguities": [],
                    }
                },
            }
        ],
        "Use encerramento com sucesso.",
    )

    assert "Extração visual estruturada" in transcript[0]["content"]
    assert "Inicia e encerra" in transcript[0]["content"]
    assert transcript[-1]["content"] == "Use encerramento com sucesso."
