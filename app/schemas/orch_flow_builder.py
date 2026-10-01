from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class OrchestrationStage(str, Enum):
    entrada = "entrada"
    identificacao = "identificacao"
    qualificacao = "qualificacao"
    abordagem = "abordagem"
    proposta = "proposta"
    decisao = "decisao"
    desfecho = "desfecho"


class FlowBuilderPlanNode(BaseModel):
    key: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_-]+$")
    component_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")
    description: str = Field(min_length=1, max_length=500)
    stage: OrchestrationStage
    parameters: dict[str, Any] = Field(default_factory=dict)
    ref_id: UUID | None = None


class FlowBuilderPlanEdge(BaseModel):
    source: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=120)
    branch: str = Field(min_length=1, max_length=120)


class FlowBuilderPlan(BaseModel):
    plan_id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    session_mode: Literal["person", "channel"] = "person"
    trigger_key: str = Field(min_length=1, max_length=120)
    nodes: list[FlowBuilderPlanNode] = Field(min_length=1, max_length=250)
    edges: list[FlowBuilderPlanEdge] = Field(default_factory=list, max_length=1000)


class FlowBuilderIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    path: str
    message: str


class FlowBuilderCompilation(BaseModel):
    valid: bool
    definition: dict[str, Any] | None = None
    issues: list[FlowBuilderIssue] = Field(default_factory=list)


class FlowBuilderSessionCreateRequest(BaseModel):
    intent: Literal["create", "edit"] = "create"
    flow_uuid: UUID | None = None
    initial_message: str | None = Field(default=None, max_length=20_000)


class FlowBuilderMessageCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    attachment_metadata: dict[str, Any] = Field(default_factory=dict)


class FlowBuilderCompileRequest(BaseModel):
    expected_version: int = Field(ge=1)
    plan: FlowBuilderPlan


class FlowBuilderMessage(BaseModel):
    id: UUID
    sequence: int
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    attachment_metadata: dict[str, Any] = Field(default_factory=dict)
    structured_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class FlowBuilderSession(BaseModel):
    id: UUID
    workspace_uuid: UUID
    mode: Literal["orchestration"] = "orchestration"
    intent: Literal["create", "edit"]
    status: Literal["planning", "ready", "saved", "abandoned", "error"]
    flow_uuid: UUID | None = None
    draft_checksum: str | None = None
    version: int
    plan: dict[str, Any] = Field(default_factory=dict)
    compiled_definition: dict[str, Any] | None = None
    issues: list[dict[str, Any]] = Field(default_factory=list)
    created_by: str
    created_at: datetime
    updated_at: datetime
    messages: list[FlowBuilderMessage] = Field(default_factory=list)


class FlowBuilderCompileResponse(BaseModel):
    session: FlowBuilderSession
    compilation: FlowBuilderCompilation
