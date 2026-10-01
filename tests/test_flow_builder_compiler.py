from __future__ import annotations

from uuid import UUID

from app.schemas.orch_flow_builder import FlowBuilderPlan
from app.services.flow_builder_compiler import compile_orchestration_flow


CATALOG = [
    {
        "id": "set_variables",
        "next_task_allowed": ["condition"],
        "branches": [{"key_value": "proximo", "required": False}],
    },
    {
        "id": "condition",
        "next_task_allowed": ["finish_flow"],
        "branches": None,
    },
    {
        "id": "finish_flow",
        "next_task_allowed": None,
        "branches": None,
    },
]


def _plan() -> FlowBuilderPlan:
    return FlowBuilderPlan.model_validate(
        {
            "plan_id": "9f40f862-2775-47a6-914a-23566f571c87",
            "name": "Canário criado por IA",
            "description": "Fluxo mínimo de homologação.",
            "session_mode": "person",
            "trigger_key": "entrada",
            "nodes": [
                {
                    "key": "entrada",
                    "component_id": "set_variables",
                    "description": "Preparar contexto",
                    "stage": "entrada",
                    "parameters": {"instructions": []},
                },
                {
                    "key": "decidir",
                    "component_id": "condition",
                    "description": "Escolher resultado",
                    "stage": "decisao",
                    "parameters": {
                        "conditions": [
                            {"id": "aceite", "label": "ACEITE", "match": "all", "rules": []}
                        ]
                    },
                },
                {
                    "key": "fim",
                    "component_id": "finish_flow",
                    "description": "Encerrar",
                    "stage": "desfecho",
                    "parameters": {"result": {"id": "success", "name": "Success"}},
                },
            ],
            "edges": [
                {"source": "entrada", "target": "decidir", "branch": "proximo"},
                {"source": "decidir", "target": "fim", "branch": "aceite"},
            ],
        }
    )


def test_compiler_produces_stable_legacy_definition() -> None:
    first = compile_orchestration_flow(_plan(), component_catalog=CATALOG)
    second = compile_orchestration_flow(_plan(), component_catalog=CATALOG)

    assert first.valid is True
    assert first.definition == second.definition
    definition = first.definition or {}
    assert definition["mode"] == "orchestration"
    assert definition["session_mode"] == "person"
    assert definition["source"] == "manual"
    assert definition["builder_metadata"]["owner"] == "orch"
    assert len(definition["components"]) == 3
    assert len(definition["branches"]) == 2
    assert UUID(definition["trigger_start_by_ref_id"])
    assert definition["components"][0]["parameters"]["stage"] == {
        "id": "entrada",
        "name": "ENTRADA",
    }
    assert definition["canvas_properties"]["positions"][0]["ref_id"] == "trigger-start-node"


def test_compiler_rejects_unknown_component_and_does_not_emit_definition() -> None:
    plan = _plan().model_copy(deep=True)
    plan.nodes[0].component_id = "invented_by_model"

    compiled = compile_orchestration_flow(plan, component_catalog=CATALOG)

    assert compiled.valid is False
    assert compiled.definition is None
    assert {issue.code for issue in compiled.issues} >= {"component_not_in_catalog"}


def test_compiler_enforces_catalog_next_allowed() -> None:
    plan = _plan().model_copy(deep=True)
    plan.edges[0].target = "fim"

    compiled = compile_orchestration_flow(plan, component_catalog=CATALOG)

    assert compiled.valid is False
    assert any(issue.code == "next_component_not_allowed" for issue in compiled.issues)


def test_compiler_accepts_condition_dynamic_branch_and_rejects_unknown_one() -> None:
    accepted = compile_orchestration_flow(_plan(), component_catalog=CATALOG)
    assert accepted.valid is True

    plan = _plan().model_copy(deep=True)
    plan.edges[1].branch = "talvez"
    rejected = compile_orchestration_flow(plan, component_catalog=CATALOG)

    assert rejected.valid is False
    assert any(issue.code == "branch_not_declared" for issue in rejected.issues)
