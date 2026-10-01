from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid5

from app.schemas.orch_flow_builder import (
    FlowBuilderCompilation,
    FlowBuilderIssue,
    FlowBuilderPlan,
    FlowBuilderPlanNode,
)


_STAGE_LABELS = {
    "entrada": "ENTRADA",
    "identificacao": "IDENTIFICAÇÃO",
    "qualificacao": "QUALIFICAÇÃO",
    "abordagem": "ABORDAGEM",
    "proposta": "PROPOSTA",
    "decisao": "DECISÃO",
    "desfecho": "DESFECHO",
}


def _issue(severity: str, code: str, path: str, message: str) -> FlowBuilderIssue:
    return FlowBuilderIssue(severity=severity, code=code, path=path, message=message)  # type: ignore[arg-type]


def _component_ref_id(plan_id: UUID, node: FlowBuilderPlanNode) -> str:
    return str(node.ref_id or uuid5(plan_id, node.key))


def _catalog_index(component_catalog: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id") or "").strip(): item
        for item in component_catalog
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }


def _declared_branch_keys(catalog_component: dict[str, Any], node: FlowBuilderPlanNode) -> set[str]:
    keys = {
        str(branch.get("key_value") or "").strip()
        for branch in catalog_component.get("branches") or []
        if isinstance(branch, dict) and str(branch.get("key_value") or "").strip()
    }
    if node.component_id == "condition":
        for condition in node.parameters.get("conditions") or []:
            if isinstance(condition, dict) and str(condition.get("id") or "").strip():
                keys.add(str(condition["id"]).strip())
        keys.update({"false", "exception"})
    return keys


def _required_branch_keys(catalog_component: dict[str, Any]) -> set[str]:
    return {
        str(branch.get("key_value") or "").strip()
        for branch in catalog_component.get("branches") or []
        if isinstance(branch, dict)
        and bool(branch.get("required"))
        and str(branch.get("key_value") or "").strip()
    }


def _layout_positions(plan: FlowBuilderPlan, ref_by_key: dict[str, str]) -> list[dict[str, Any]]:
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in plan.edges:
        outgoing[edge.source].append(edge.target)

    distance: dict[str, int] = {plan.trigger_key: 0}
    queue: deque[str] = deque([plan.trigger_key])
    while queue:
        source = queue.popleft()
        for target in outgoing.get(source, []):
            candidate = distance[source] + 1
            if target not in distance or candidate < distance[target]:
                distance[target] = candidate
                queue.append(target)

    fallback_layer = max(distance.values(), default=0) + 1
    by_layer: dict[int, list[str]] = defaultdict(list)
    for node in plan.nodes:
        by_layer[distance.get(node.key, fallback_layer)].append(node.key)

    positions: list[dict[str, Any]] = [
        {"ref_id": "trigger-start-node", "position": {"x": 40, "y": 220}}
    ]
    for layer in sorted(by_layer):
        for row, key in enumerate(by_layer[layer]):
            positions.append(
                {
                    "ref_id": ref_by_key[key],
                    "position": {"x": 360 + (layer * 360), "y": 100 + (row * 240)},
                }
            )
    return positions


def compile_orchestration_flow(
    plan: FlowBuilderPlan,
    *,
    component_catalog: list[dict[str, Any]],
) -> FlowBuilderCompilation:
    issues: list[FlowBuilderIssue] = []
    catalog = _catalog_index(component_catalog)
    nodes_by_key: dict[str, FlowBuilderPlanNode] = {}

    for index, node in enumerate(plan.nodes):
        if node.key in nodes_by_key:
            issues.append(
                _issue("error", "duplicate_node_key", f"nodes[{index}].key", f"O identificador '{node.key}' está duplicado.")
            )
            continue
        nodes_by_key[node.key] = node
        if node.component_id not in catalog:
            issues.append(
                _issue(
                    "error",
                    "component_not_in_catalog",
                    f"nodes[{index}].component_id",
                    f"O componente '{node.component_id}' não está disponível no catálogo deste workspace.",
                )
            )

    if plan.trigger_key not in nodes_by_key:
        issues.append(
            _issue("error", "trigger_not_found", "trigger_key", "O card inicial não existe no plano.")
        )

    edges_by_source: dict[str, list[tuple[int, Any]]] = defaultdict(list)
    edge_identity: set[tuple[str, str, str]] = set()
    for index, edge in enumerate(plan.edges):
        if edge.source not in nodes_by_key:
            issues.append(_issue("error", "edge_source_not_found", f"edges[{index}].source", "A origem da conexão não existe."))
            continue
        if edge.target not in nodes_by_key:
            issues.append(_issue("error", "edge_target_not_found", f"edges[{index}].target", "O destino da conexão não existe."))
            continue
        identity = (edge.source, edge.target, edge.branch)
        if identity in edge_identity:
            issues.append(_issue("error", "duplicate_edge", f"edges[{index}]", "A conexão está duplicada."))
            continue
        edge_identity.add(identity)
        edges_by_source[edge.source].append((index, edge))

        source_node = nodes_by_key[edge.source]
        target_node = nodes_by_key[edge.target]
        source_catalog = catalog.get(source_node.component_id)
        if source_catalog is None:
            continue
        next_allowed = source_catalog.get("next_task_allowed")
        if isinstance(next_allowed, list) and target_node.component_id not in {
            str(value) for value in next_allowed
        }:
            issues.append(
                _issue(
                    "error",
                    "next_component_not_allowed",
                    f"edges[{index}].target",
                    f"'{source_node.component_id}' não permite '{target_node.component_id}' como próximo card.",
                )
            )
        declared_branches = _declared_branch_keys(source_catalog, source_node)
        if declared_branches and edge.branch not in declared_branches:
            issues.append(
                _issue(
                    "error",
                    "branch_not_declared",
                    f"edges[{index}].branch",
                    f"O branch '{edge.branch}' não é declarado pelo card '{source_node.component_id}'.",
                )
            )

    for key, node in nodes_by_key.items():
        catalog_component = catalog.get(node.component_id)
        if catalog_component is None:
            continue
        required = _required_branch_keys(catalog_component)
        connected = {edge.branch for _, edge in edges_by_source.get(key, [])}
        for missing in sorted(required - connected):
            issues.append(
                _issue(
                    "error",
                    "required_branch_unconnected",
                    f"nodes[{key}]",
                    f"O branch obrigatório '{missing}' não possui destino.",
                )
            )

    if not any(node.component_id == "finish_flow" for node in plan.nodes):
        issues.append(
            _issue("warning", "finish_flow_missing", "nodes", "O fluxo não possui um card explícito de finalização.")
        )

    if any(issue.severity == "error" for issue in issues):
        return FlowBuilderCompilation(valid=False, definition=None, issues=issues)

    ref_by_key = {node.key: _component_ref_id(plan.plan_id, node) for node in plan.nodes}
    components: list[dict[str, Any]] = []
    for node in plan.nodes:
        parameters = deepcopy(node.parameters)
        stage_id = node.stage.value
        parameters["stage"] = {"id": stage_id, "name": _STAGE_LABELS[stage_id]}
        outgoing_branches = {edge.branch for _, edge in edges_by_source.get(node.key, [])}
        component: dict[str, Any] = {
            "ref_id": ref_by_key[node.key],
            "component_id": node.component_id,
            "description": node.description,
            "parameters": parameters,
        }
        if "exception" in outgoing_branches:
            component["has_exception_branch"] = True
        if "timeout" in outgoing_branches:
            component["has_timeout_branch"] = True
            component["timeout_branch_name"] = "Timeout"
        components.append(component)

    branches = [
        {
            "from": ref_by_key[edge.source],
            "to": ref_by_key[edge.target],
            "branch": edge.branch,
        }
        for edge in plan.edges
    ]
    definition = {
        "info": {"name": plan.name, "description": plan.description},
        "session_mode": plan.session_mode,
        "trigger_ids": [],
        "trigger_start_by_ref_id": ref_by_key[plan.trigger_key],
        "mode": "orchestration",
        "source": "manual",
        "prompt": None,
        "reorganized": False,
        "components": components,
        "branches": branches,
        "avatar": {"id": "pt-BR-FranciscaNeural", "name": "FRANCISCA"},
        "variables": {"utils": [], "system": {}, "customs": []},
        "canvas_properties": {
            "positions": _layout_positions(plan, ref_by_key),
            "collapsed_components": [],
        },
        "webhook": [],
        "channels": [],
        "status": "draft",
        "builder_metadata": {
            "owner": "orch",
            "kind": "ai_flow_builder",
            "plan_id": str(plan.plan_id),
            "mode": "orchestration",
        },
    }
    return FlowBuilderCompilation(valid=True, definition=definition, issues=issues)
