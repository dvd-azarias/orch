from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkflowCardRef:
    card_uuid: str


@dataclass(frozen=True)
class WorkflowBootstrap:
    start_card_uuid: str | None
    next_card_uuid: str | None


@dataclass(frozen=True)
class BranchEdge:
    source: str
    target: str
    label: str | None


def _component_card_uuid(component: dict[str, Any]) -> str | None:
    for key in ("uuid", "id", "card_uuid", "component_uuid", "ref_id"):
        value = component.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _component_aliases(component: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    for key in ("uuid", "id", "card_uuid", "component_uuid", "ref_id"):
        value = component.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in aliases:
            aliases.append(text)
    return aliases


def _alias_index(definition: dict[str, Any]) -> dict[str, str]:
    components = definition.get("components")
    if not isinstance(components, list):
        return {}

    aliases: dict[str, str] = {}
    for entry in components:
        if not isinstance(entry, dict):
            continue
        primary = _component_card_uuid(entry)
        if primary is None:
            continue
        for alias in _component_aliases(entry):
            aliases[alias] = primary
    return aliases


def extract_components(definition: dict[str, Any]) -> list[WorkflowCardRef]:
    components = definition.get("components")
    if not isinstance(components, list):
        return []

    items: list[WorkflowCardRef] = []
    for entry in components:
        if not isinstance(entry, dict):
            continue
        card_uuid = _component_card_uuid(entry)
        if card_uuid:
            items.append(WorkflowCardRef(card_uuid=card_uuid))
    return items


def index_components(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    components = definition.get("components")
    if not isinstance(components, list):
        return {}

    indexed: dict[str, dict[str, Any]] = {}
    for entry in components:
        if not isinstance(entry, dict):
            continue
        primary = _component_card_uuid(entry)
        if not primary:
            continue
        indexed[primary] = entry
        for alias in _component_aliases(entry):
            indexed.setdefault(alias, entry)
    return indexed


def component_kind(component: dict[str, Any]) -> str:
    for key in ("component_id", "component", "type", "slug", "name"):
        value = component.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().lower()
    return ""


def _extract_edge(branch: Any) -> tuple[str | None, str | None]:
    if not isinstance(branch, dict):
        return None, None

    source = None
    target = None

    for source_key in ("from", "source", "origin", "card_uuid", "current", "from_card_uuid"):
        if source_key in branch:
            raw = branch.get(source_key)
            if raw is not None and str(raw).strip():
                source = str(raw).strip()
                break

    for target_key in ("to", "target", "next", "next_card_uuid", "dest", "destination"):
        if target_key in branch:
            raw = branch.get(target_key)
            if raw is not None and str(raw).strip():
                target = str(raw).strip()
                break

    return source, target


def _extract_edge_label(branch: Any) -> str | None:
    if not isinstance(branch, dict):
        return None
    for key in ("label", "branch", "name", "condition", "value"):
        value = branch.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().lower()
    return None


def extract_edges(definition: dict[str, Any]) -> list[BranchEdge]:
    branches = definition.get("branches")
    edges: list[BranchEdge] = []
    aliases = _alias_index(definition)

    if isinstance(branches, list):
        for branch in branches:
            source, target = _extract_edge(branch)
            if source is None or target is None:
                continue
            source = aliases.get(source, source)
            target = aliases.get(target, target)
            edges.append(BranchEdge(source=source, target=target, label=_extract_edge_label(branch)))
        return edges

    if isinstance(branches, dict):
        for source, value in branches.items():
            source_key = str(source).strip()
            if not source_key:
                continue
            source_key = aliases.get(source_key, source_key)

            if isinstance(value, str) and value.strip():
                target = aliases.get(value.strip(), value.strip())
                edges.append(BranchEdge(source=source_key, target=target, label=None))
                continue

            if isinstance(value, dict):
                _, target = _extract_edge({"from": source_key, **value})
                if target:
                    target = aliases.get(target, target)
                    edges.append(BranchEdge(source=source_key, target=target, label=_extract_edge_label(value)))
                continue

            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        target = aliases.get(item.strip(), item.strip())
                        edges.append(BranchEdge(source=source_key, target=target, label=None))
                        continue
                    s, t = _extract_edge(item)
                    if s is None:
                        s = source_key
                    else:
                        s = aliases.get(s, s)
                    if s == source_key and t:
                        t = aliases.get(t, t)
                        edges.append(BranchEdge(source=source_key, target=t, label=_extract_edge_label(item)))
        return edges

    return edges


def build_adjacency(definition: dict[str, Any]) -> dict[str, list[str]]:
    adjacency: dict[str, list[str]] = {}
    for edge in extract_edges(definition):
        adjacency.setdefault(edge.source, [])
        if edge.target not in adjacency[edge.source]:
            adjacency[edge.source].append(edge.target)
    return adjacency


def resolve_start_card_uuid(definition: dict[str, Any]) -> str | None:
    aliases = _alias_index(definition)
    explicit = (
        definition.get("start_card_uuid")
        or definition.get("entrypoint")
        or definition.get("start_component_uuid")
        or definition.get("trigger_start_by_ref_id")
    )
    if explicit is not None and str(explicit).strip():
        raw = str(explicit).strip()
        return aliases.get(raw, raw)

    components = extract_components(definition)
    if not components:
        return None

    component_ids = [item.card_uuid for item in components]
    adjacency = build_adjacency(definition)

    indegree: dict[str, int] = {card_id: 0 for card_id in component_ids}
    for source, targets in adjacency.items():
        if source not in indegree:
            indegree[source] = 0
        for target in targets:
            indegree[target] = indegree.get(target, 0) + 1

    for card_id in component_ids:
        if indegree.get(card_id, 0) == 0:
            return card_id

    return component_ids[0]


def resolve_next_card_uuid(definition: dict[str, Any], current_card_uuid: str | None) -> str | None:
    if current_card_uuid is None:
        return resolve_start_card_uuid(definition)

    adjacency = build_adjacency(definition)
    next_candidates = adjacency.get(current_card_uuid, [])
    if next_candidates:
        return next_candidates[0]
    return None


def resolve_next_card_uuid_by_branch(
    definition: dict[str, Any],
    *,
    current_card_uuid: str,
    branch_label: str | None,
) -> str | None:
    normalized = str(branch_label).strip().lower() if branch_label is not None else None
    edges = [edge for edge in extract_edges(definition) if edge.source == current_card_uuid]
    if not edges:
        return None

    if normalized:
        for edge in edges:
            if edge.label == normalized:
                return edge.target

    return edges[0].target


def outgoing_branch_labels(definition: dict[str, Any], *, current_card_uuid: str) -> list[str]:
    labels: list[str] = []
    for edge in extract_edges(definition):
        if edge.source != current_card_uuid:
            continue
        if edge.label and edge.label not in labels:
            labels.append(edge.label)
    return labels


def build_bootstrap(definition: dict[str, Any]) -> WorkflowBootstrap:
    start = resolve_start_card_uuid(definition)
    return WorkflowBootstrap(start_card_uuid=start, next_card_uuid=start)
