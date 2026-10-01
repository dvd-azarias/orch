from __future__ import annotations

from uuid import UUID

from app.schemas.orch_flow_builder import FlowBuilderPlan
from app.services.flow_builder_compiler import (
    build_flow_builder_preview,
    compile_orchestration_flow,
)


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


def test_compiler_applies_catalog_defaults_and_rejects_missing_dynamic_id() -> None:
    catalog = [
        {
            "id": "set_variables",
            "next_task_allowed": ["finish_flow"],
            "parameters": [
                {
                    "id": "mode",
                    "required": True,
                    "value": "safe",
                    "options": [{"id": "safe", "name": "Seguro"}],
                },
                {
                    "id": "queue_id",
                    "required": True,
                    "value": None,
                    "external_request_url": "/queues",
                },
            ],
            "branches": [{"key_value": "proximo", "required": False}],
        },
        CATALOG[2],
    ]
    original = _plan()
    plan = FlowBuilderPlan.model_validate(
        {
            **original.model_dump(mode="python"),
            "nodes": [original.nodes[0].model_dump(), original.nodes[2].model_dump()],
            "edges": [{"source": "entrada", "target": "fim", "branch": "proximo"}],
        }
    )

    missing = compile_orchestration_flow(plan, component_catalog=catalog)
    assert missing.valid is False
    assert any(issue.code == "required_parameter_missing" for issue in missing.issues)

    plan.nodes[0].parameters["queue_id"] = "queue-real"
    compiled = compile_orchestration_flow(plan, component_catalog=catalog)
    assert compiled.valid is True
    definition = compiled.definition or {}
    assert definition["components"][0]["parameters"]["mode"] == "safe"


def test_compiler_rejects_static_option_outside_catalog() -> None:
    plan = _plan().model_copy(deep=True)
    plan.nodes[2].parameters["result"] = "invented"
    catalog = [
        CATALOG[0],
        CATALOG[1],
        {
            **CATALOG[2],
            "parameters": [
                {
                    "id": "result",
                    "required": True,
                    "options": [{"id": "success", "name": "Success"}],
                }
            ],
        },
    ]

    compiled = compile_orchestration_flow(plan, component_catalog=catalog)

    assert compiled.valid is False
    assert any(issue.code == "parameter_option_not_allowed" for issue in compiled.issues)


def test_compiler_compares_structured_options_without_unhashable_values() -> None:
    plan = _plan().model_copy(deep=True)
    plan.nodes[0].parameters["display"] = {"label": "Seguro", "nested": ["a", "b"]}
    catalog = [
        {
            **CATALOG[0],
            "parameters": [
                {
                    "id": "display",
                    "required": True,
                    "options": [{"label": "Seguro", "nested": ["a", "b"]}],
                }
            ],
        },
        CATALOG[1],
        CATALOG[2],
    ]

    compiled = compile_orchestration_flow(plan, component_catalog=catalog)

    assert compiled.valid is True


def test_preview_summarizes_stages_and_pending_issues() -> None:
    plan = _plan().model_copy(deep=True)
    plan.name = "N" * 41
    compilation = compile_orchestration_flow(plan, component_catalog=CATALOG)

    preview = build_flow_builder_preview(plan, compilation=compilation)

    assert preview.valid is False
    assert preview.node_count == 3
    assert preview.edge_count == 2
    assert preview.stages == {"entrada": 1, "decisao": 1, "desfecho": 1}
    assert any(issue.code == "flow_name_too_long" for issue in preview.issues)
