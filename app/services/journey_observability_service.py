from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.workspace import get_current_workspace_schema
from app.repositories.flow_v2_repository import fetch_revision_by_id, fetch_selected_revision
from app.repositories.journey_observability_repository import (
    fetch_journey_aggregate_totals,
    fetch_journey_edge_aggregates,
    fetch_journey_node_aggregates,
    fetch_journey_session,
    fetch_journey_sessions,
    fetch_journey_trace_metrics,
    fetch_observed_revisions,
)
from app.schemas.orch_observability import (
    JourneyEdge,
    JourneyEdgeAggregate,
    JourneyFlowSummary,
    JourneyNode,
    JourneyNodeAggregate,
    JourneyRevisionSummary,
    JourneySessionListResponse,
    JourneySessionSummary,
    JourneySessionTrace,
    JourneyTraceStep,
)


TRIGGER_NODE_ID = "trigger-start-node"
_SESSION_STATE_LABELS = {
    0: "Em processamento",
    1: "Acionada",
    2: "Aguardando evento",
    3: "Finalizada",
    5: "Interrompida",
}


class JourneyObservabilityNotFoundError(LookupError):
    pass


class JourneyObservabilityValidationError(ValueError):
    pass


def validate_period(
    period_from: datetime,
    period_to: datetime,
    *,
    settings: Settings | None = None,
) -> tuple[datetime, datetime]:
    resolved_settings = settings or get_settings()
    if period_from.tzinfo is None or period_to.tzinfo is None:
        raise JourneyObservabilityValidationError("O período deve informar timezone.")
    normalized_from = period_from.astimezone(timezone.utc)
    normalized_to = period_to.astimezone(timezone.utc)
    if normalized_from >= normalized_to:
        raise JourneyObservabilityValidationError("O início do período deve ser anterior ao fim.")
    max_hours = max(1, int(resolved_settings.orch_observability_max_window_hours))
    if (normalized_to - normalized_from).total_seconds() > max_hours * 3600:
        raise JourneyObservabilityValidationError(
            f"O período máximo para rastreamento é de {max_hours} horas."
        )
    return normalized_from, normalized_to


def _edge_id(source: str, target: str, branch: str | None) -> str:
    return f"{source}::{branch or 'default'}::{target}"


def _project_definition(definition: dict[str, Any]) -> tuple[list[JourneyNode], list[JourneyEdge]]:
    canvas = definition.get("canvas_properties")
    canvas = canvas if isinstance(canvas, dict) else {}
    raw_positions = canvas.get("positions")
    raw_positions = raw_positions if isinstance(raw_positions, list) else []
    positions: dict[str, tuple[float, float]] = {}
    for item in raw_positions:
        if not isinstance(item, dict):
            continue
        ref_id = str(item.get("ref_id") or "").strip()
        position = item.get("position")
        if not ref_id or not isinstance(position, dict):
            continue
        try:
            positions[ref_id] = (float(position.get("x") or 0), float(position.get("y") or 0))
        except (TypeError, ValueError):
            continue

    trigger_position = positions.get(TRIGGER_NODE_ID, (80.0, 160.0))
    nodes = [
        JourneyNode(
            id=TRIGGER_NODE_ID,
            kind="trigger_start",
            label="Início",
            description="Entrada da jornada",
            position_x=trigger_position[0],
            position_y=trigger_position[1],
        )
    ]

    raw_components = definition.get("components")
    raw_components = raw_components if isinstance(raw_components, list) else []
    for index, component in enumerate(raw_components):
        if not isinstance(component, dict):
            continue
        ref_id = str(component.get("ref_id") or "").strip()
        if not ref_id:
            continue
        kind = str(component.get("component_id") or "unknown").strip() or "unknown"
        description = str(component.get("description") or "").strip() or None
        position = positions.get(ref_id, (360.0 + (index % 4) * 320.0, 120.0 + (index // 4) * 220.0))
        nodes.append(
            JourneyNode(
                id=ref_id,
                kind=kind,
                label=description or kind.replace("_", " ").strip().title(),
                description=description,
                position_x=position[0],
                position_y=position[1],
            )
        )

    edges: list[JourneyEdge] = []
    start_ref_id = str(definition.get("trigger_start_by_ref_id") or "").strip()
    if start_ref_id:
        edges.append(
            JourneyEdge(
                id=_edge_id(TRIGGER_NODE_ID, start_ref_id, "start"),
                source=TRIGGER_NODE_ID,
                target=start_ref_id,
                branch="start",
            )
        )

    raw_branches = definition.get("branches")
    raw_branches = raw_branches if isinstance(raw_branches, list) else []
    seen_edges: set[str] = {edge.id for edge in edges}
    for branch in raw_branches:
        if not isinstance(branch, dict):
            continue
        source = str(branch.get("from") or "").strip()
        target = str(branch.get("to") or "").strip()
        label = str(branch.get("branch") or "").strip() or None
        if not source or not target:
            continue
        edge_id = _edge_id(source, target, label)
        if edge_id in seen_edges:
            continue
        seen_edges.add(edge_id)
        edges.append(JourneyEdge(id=edge_id, source=source, target=target, branch=label))
    return nodes, edges


def _mask_identifier(value: Any) -> str:
    raw = str(value or "").strip()
    if len(raw) <= 4:
        return "•" * len(raw)
    return f"{raw[:3]}{'•' * min(8, len(raw) - 5)}{raw[-2:]}"


def _mask_address(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "—"
    if "@" in raw:
        local, domain = raw.split("@", 1)
        return f"{local[:1]}•••@{domain}"
    digits = "".join(character for character in raw if character.isdigit())
    if len(digits) >= 4:
        return f"••••{digits[-4:]}"
    return _mask_identifier(raw)


def _session_summary(row: dict[str, Any]) -> JourneySessionSummary:
    state = int(row.get("state") or 0)
    return JourneySessionSummary(
        session_uuid=str(row["session_uuid"]),
        state=state,
        state_label=_SESSION_STATE_LABELS.get(state, f"Estado {state}"),
        entity_display=_mask_identifier(row.get("entity")),
        entity_type=str(row.get("entity_type") or "unknown"),
        entity_address_display=_mask_address(row.get("entity_address")),
        revision_id=str(row.get("revision_id")) if row.get("revision_id") else None,
        started_at=row.get("started_at"),
        ended_at=row.get("ended_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_card_uuid=str(row.get("last_card_uuid")) if row.get("last_card_uuid") else None,
        next_card_uuid=str(row.get("next_card_uuid")) if row.get("next_card_uuid") else None,
    )


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, int | None]:
    if not cursor:
        return None, None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("utf-8")).decode("utf-8")
        created_at_raw, row_id_raw = raw.split("|", 1)
        created_at = datetime.fromisoformat(created_at_raw)
        return created_at, int(row_id_raw)
    except Exception as exc:
        raise JourneyObservabilityValidationError("Cursor inválido.") from exc


def _encode_cursor(row: dict[str, Any] | None) -> str | None:
    if row is None or row.get("created_at") is None or row.get("id") is None:
        return None
    raw = f"{row['created_at'].isoformat()}|{int(row['id'])}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


async def _prepare_read_transaction(db_session: AsyncSession, *, settings: Settings) -> None:
    safe_schema = get_current_workspace_schema().replace('"', '""')
    timeout_ms = max(250, min(30_000, int(settings.orch_observability_statement_timeout_ms)))
    await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
    await db_session.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))


def _revision_summary(
    revision: dict[str, Any],
    observed: dict[str, Any] | None = None,
) -> JourneyRevisionSummary:
    return JourneyRevisionSummary(
        revision_id=str(revision["id"]),
        version=int(revision["version"]) if revision.get("version") is not None else None,
        selection_mode=str(revision.get("selection_mode") or "revision"),
        sessions=int((observed or {}).get("sessions") or 0),
        first_seen_at=(observed or {}).get("first_seen_at"),
        last_seen_at=(observed or {}).get("last_seen_at"),
    )


async def get_flow_journey_summary(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    period_from: datetime,
    period_to: datetime,
    revision_id: str | None,
    settings: Settings | None = None,
) -> JourneyFlowSummary:
    resolved_settings = settings or get_settings()
    period_from, period_to = validate_period(period_from, period_to, settings=resolved_settings)
    UUID(flow_uuid)
    if revision_id:
        UUID(revision_id)

    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await _prepare_read_transaction(db_session, settings=resolved_settings)
        observed_rows = await fetch_observed_revisions(
            db_session,
            flow_uuid=flow_uuid,
            period_from=period_from,
            period_to=period_to,
        )
        observed_by_id = {str(row["revision_id"]): row for row in observed_rows if row.get("revision_id")}
        selected_revision_id = revision_id or next(iter(observed_by_id), None)
        if selected_revision_id:
            selected_revision = await fetch_revision_by_id(
                db_session,
                flow_id=flow_uuid,
                revision_id=selected_revision_id,
            )
        else:
            selected_revision = await fetch_selected_revision(db_session, flow_id=flow_uuid)
        if selected_revision is None:
            raise JourneyObservabilityNotFoundError("Fluxo ou revisão não encontrados no ORCH.")

        available_revisions: list[JourneyRevisionSummary] = []
        for observed in observed_rows[:20]:
            observed_revision = await fetch_revision_by_id(
                db_session,
                flow_id=flow_uuid,
                revision_id=str(observed["revision_id"]),
            )
            if observed_revision is not None:
                available_revisions.append(_revision_summary(observed_revision, observed))
        if not any(item.revision_id == str(selected_revision["id"]) for item in available_revisions):
            available_revisions.insert(
                0,
                _revision_summary(selected_revision, observed_by_id.get(str(selected_revision["id"]))),
            )

        selected_id = str(selected_revision["id"])
        node_rows = await fetch_journey_node_aggregates(
            db_session,
            flow_uuid=flow_uuid,
            revision_id=selected_id,
            period_from=period_from,
            period_to=period_to,
        )
        edge_rows = await fetch_journey_edge_aggregates(
            db_session,
            flow_uuid=flow_uuid,
            revision_id=selected_id,
            period_from=period_from,
            period_to=period_to,
        )
        totals = await fetch_journey_aggregate_totals(
            db_session,
            flow_uuid=flow_uuid,
            revision_id=selected_id,
            period_from=period_from,
            period_to=period_to,
        )

    nodes, edges = _project_definition(selected_revision.get("definition") or {})
    graph_node_ids = {node.id for node in nodes}
    edge_by_exact = {(edge.source, edge.target, edge.branch): edge.id for edge in edges}
    edge_by_pair: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        edge_by_pair.setdefault((edge.source, edge.target), []).append(edge.id)

    node_metrics = [
        JourneyNodeAggregate(
            node_id=str(row["node_id"]),
            sessions=int(row.get("sessions") or 0),
            visits=int(row.get("visits") or 0),
            successes=int(row.get("successes") or 0),
            errors=int(row.get("errors") or 0),
            stopped=int(row.get("stopped") or 0),
            average_latency_ms=float(row.get("average_latency_ms") or 0),
            maximum_latency_ms=float(row.get("maximum_latency_ms") or 0),
        )
        for row in node_rows
        if str(row.get("node_id") or "") in graph_node_ids
    ]
    observed_sessions = int(totals.get("observed_sessions") or 0)
    observed_visits = int(totals.get("observed_visits") or 0)
    node_metrics.insert(
        0,
        JourneyNodeAggregate(
            node_id=TRIGGER_NODE_ID,
            sessions=observed_sessions,
            visits=observed_sessions,
            successes=observed_sessions,
            errors=0,
            stopped=0,
            average_latency_ms=0,
            maximum_latency_ms=0,
        ),
    )

    edge_metrics: list[JourneyEdgeAggregate] = []
    for row in edge_rows:
        source = str(row.get("source") or "")
        target = str(row.get("target") or "")
        branch = str(row.get("branch")) if row.get("branch") else None
        edge_id = edge_by_exact.get((source, target, branch))
        if edge_id is None:
            candidates = edge_by_pair.get((source, target), [])
            edge_id = candidates[0] if len(candidates) == 1 else None
        if edge_id is None:
            continue
        edge_metrics.append(
            JourneyEdgeAggregate(
                edge_id=edge_id,
                sessions=int(row.get("sessions") or 0),
                visits=int(row.get("visits") or 0),
            )
        )
    start_edge = next((edge for edge in edges if edge.source == TRIGGER_NODE_ID), None)
    if start_edge is not None:
        edge_metrics.insert(
            0,
            JourneyEdgeAggregate(
                edge_id=start_edge.id,
                sessions=observed_sessions,
                visits=observed_sessions,
            ),
        )

    return JourneyFlowSummary(
        flow_uuid=flow_uuid,
        period_from=period_from,
        period_to=period_to,
        revision=_revision_summary(selected_revision, observed_by_id.get(selected_id)),
        available_revisions=available_revisions,
        nodes=nodes,
        edges=edges,
        node_metrics=node_metrics,
        edge_metrics=edge_metrics,
        observed_sessions=observed_sessions,
        observed_visits=observed_visits,
    )


async def list_flow_journey_sessions(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    period_from: datetime,
    period_to: datetime,
    person_uuid: str | None,
    limit: int,
    cursor: str | None,
    settings: Settings | None = None,
) -> JourneySessionListResponse:
    resolved_settings = settings or get_settings()
    period_from, period_to = validate_period(period_from, period_to, settings=resolved_settings)
    UUID(flow_uuid)
    if person_uuid:
        UUID(person_uuid)
    safe_limit = max(1, min(100, int(limit)))
    cursor_created_at, cursor_id = _decode_cursor(cursor)
    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await _prepare_read_transaction(db_session, settings=resolved_settings)
        rows = await fetch_journey_sessions(
            db_session,
            flow_uuid=flow_uuid,
            person_uuid=str(person_uuid or "").strip() or None,
            period_from=period_from,
            period_to=period_to,
            limit=safe_limit + 1,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
        )
    has_more = len(rows) > safe_limit
    items = rows[:safe_limit]
    return JourneySessionListResponse(
        flow_uuid=flow_uuid,
        period_from=period_from,
        period_to=period_to,
        items=[_session_summary(row) for row in items],
        next_cursor=_encode_cursor(items[-1]) if has_more and items else None,
    )


async def get_session_journey_trace(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    session_uuid: str,
    settings: Settings | None = None,
) -> JourneySessionTrace:
    resolved_settings = settings or get_settings()
    UUID(flow_uuid)
    UUID(session_uuid)
    max_steps = max(100, min(10_000, int(resolved_settings.orch_observability_max_trace_steps)))
    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await _prepare_read_transaction(db_session, settings=resolved_settings)
        session_row = await fetch_journey_session(
            db_session,
            flow_uuid=flow_uuid,
            session_uuid=session_uuid,
        )
        if session_row is None:
            raise JourneyObservabilityNotFoundError("Sessão não encontrada para este fluxo.")
        metric_rows_desc = await fetch_journey_trace_metrics(
            db_session,
            session_id=int(session_row["id"]),
            limit=max_steps + 1,
        )
        truncated = len(metric_rows_desc) > max_steps
        metric_rows = list(reversed(metric_rows_desc[:max_steps]))
        revision_id = str(session_row.get("revision_id") or "").strip()
        if not revision_id:
            revision_id = next(
                (str(row["revision_id"]) for row in reversed(metric_rows) if row.get("revision_id")),
                "",
            )
        if not revision_id:
            raise JourneyObservabilityNotFoundError("A sessão não possui revisão observável.")
        revision = await fetch_revision_by_id(
            db_session,
            flow_id=flow_uuid,
            revision_id=revision_id,
        )
        if revision is None:
            raise JourneyObservabilityNotFoundError("A revisão executada pela sessão não foi encontrada.")

    nodes, edges = _project_definition(revision.get("definition") or {})
    edge_by_exact = {(edge.source, edge.target, edge.branch): edge.id for edge in edges}
    edge_by_pair: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        edge_by_pair.setdefault((edge.source, edge.target), []).append(edge.id)

    steps: list[JourneyTraceStep] = []
    visited_nodes = [TRIGGER_NODE_ID]
    visited_edges: list[str] = []
    start_edge = next((edge for edge in edges if edge.source == TRIGGER_NODE_ID), None)
    if metric_rows and start_edge is not None:
        visited_edges.append(start_edge.id)
    for visit_index, row in enumerate(metric_rows, start=1):
        node_id = str(row.get("node_id") or "")
        if not node_id:
            continue
        branch = str(row.get("branch")) if row.get("branch") else None
        next_node_id = str(row.get("next_node_id")) if row.get("next_node_id") else None
        steps.append(
            JourneyTraceStep(
                visit_index=visit_index,
                step_index=int(row["step_index"]) if row.get("step_index") is not None else None,
                node_id=node_id,
                component_kind=str(row.get("component_kind")) if row.get("component_kind") else None,
                status=str(row.get("status") or "unknown"),
                stopped_reason=str(row.get("stopped_reason")) if row.get("stopped_reason") else None,
                branch=branch,
                next_node_id=next_node_id,
                latency_ms=float(row.get("latency_ms") or 0),
                started_at=row["started_at"],
                finished_at=row["finished_at"],
            )
        )
        if node_id not in visited_nodes:
            visited_nodes.append(node_id)
        if next_node_id:
            edge_id = edge_by_exact.get((node_id, next_node_id, branch))
            if edge_id is None:
                candidates = edge_by_pair.get((node_id, next_node_id), [])
                edge_id = candidates[0] if len(candidates) == 1 else None
            if edge_id and edge_id not in visited_edges:
                visited_edges.append(edge_id)

    session = _session_summary(session_row)
    terminal_node_id = session.last_card_uuid if session.state in {3, 5} else None
    current_node_id = terminal_node_id or session.next_card_uuid or session.last_card_uuid
    return JourneySessionTrace(
        flow_uuid=flow_uuid,
        session=session,
        revision=_revision_summary(revision),
        nodes=nodes,
        edges=edges,
        steps=steps,
        visited_node_ids=visited_nodes,
        visited_edge_ids=visited_edges,
        current_node_id=current_node_id,
        terminal_node_id=terminal_node_id,
        truncated_before=truncated,
    )
