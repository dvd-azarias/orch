from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class JourneyRevisionSummary(BaseModel):
    revision_id: str
    version: int | None = None
    selection_mode: str | None = None
    sessions: int = 0
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class JourneyNode(BaseModel):
    id: str
    kind: str
    label: str
    description: str | None = None
    position_x: float
    position_y: float


class JourneyEdge(BaseModel):
    id: str
    source: str
    target: str
    branch: str | None = None


class JourneyNodeAggregate(BaseModel):
    node_id: str
    sessions: int
    visits: int
    successes: int
    errors: int
    stopped: int
    average_latency_ms: float
    maximum_latency_ms: float


class JourneyEdgeAggregate(BaseModel):
    edge_id: str
    sessions: int
    visits: int


class JourneyFlowSummary(BaseModel):
    api_version: str = "v1"
    flow_uuid: str
    period_from: datetime
    period_to: datetime
    revision: JourneyRevisionSummary
    available_revisions: list[JourneyRevisionSummary]
    nodes: list[JourneyNode]
    edges: list[JourneyEdge]
    node_metrics: list[JourneyNodeAggregate]
    edge_metrics: list[JourneyEdgeAggregate]
    observed_sessions: int
    observed_visits: int


class JourneySessionSummary(BaseModel):
    session_uuid: str
    state: int
    state_label: str
    entity_display: str
    entity_type: str
    entity_address_display: str
    revision_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_card_uuid: str | None = None
    next_card_uuid: str | None = None


class JourneySessionListResponse(BaseModel):
    api_version: str = "v1"
    flow_uuid: str
    period_from: datetime
    period_to: datetime
    items: list[JourneySessionSummary]
    next_cursor: str | None = None


class JourneyTraceStep(BaseModel):
    visit_index: int = Field(ge=1)
    step_index: int | None = None
    node_id: str
    component_kind: str | None = None
    status: str
    stopped_reason: str | None = None
    branch: str | None = None
    next_node_id: str | None = None
    latency_ms: float
    started_at: datetime
    finished_at: datetime


class JourneySessionTrace(BaseModel):
    api_version: str = "v1"
    flow_uuid: str
    session: JourneySessionSummary
    revision: JourneyRevisionSummary
    nodes: list[JourneyNode]
    edges: list[JourneyEdge]
    steps: list[JourneyTraceStep]
    visited_node_ids: list[str]
    visited_edge_ids: list[str]
    current_node_id: str | None = None
    terminal_node_id: str | None = None
    truncated_before: bool = False
