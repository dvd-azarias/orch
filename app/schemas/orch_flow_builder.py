from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


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


class FlowBuilderPlannerQuestion(BaseModel):
    key: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_.-]+$")
    question: str = Field(min_length=1, max_length=500)
    parameter_path: str | None = Field(default=None, max_length=255)
    choices: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class FlowBuilderPlannerOutcome(BaseModel):
    status: Literal["needs_input", "preview_ready"]
    assistant_message: str = Field(min_length=1, max_length=4000)
    questions: list[FlowBuilderPlannerQuestion] = Field(default_factory=list, max_length=10)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    plan: FlowBuilderPlan | None = None

    @model_validator(mode="after")
    def validate_status_contract(self) -> "FlowBuilderPlannerOutcome":
        if self.status == "needs_input" and not self.questions:
            raise ValueError("needs_input exige ao menos uma pergunta objetiva")
        if self.status == "preview_ready" and self.plan is None:
            raise ValueError("preview_ready exige um FlowPlan")
        if self.status == "preview_ready" and self.questions:
            raise ValueError("preview_ready não pode conter perguntas pendentes")
        return self


class FlowBuilderAssistRequest(BaseModel):
    expected_version: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=20_000)


class FlowBuilderPreviewNode(BaseModel):
    key: str
    component_id: str
    description: str
    stage: OrchestrationStage
    outgoing_branches: list[str] = Field(default_factory=list)


class FlowBuilderPreview(BaseModel):
    valid: bool
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    stages: dict[str, int] = Field(default_factory=dict)
    nodes: list[FlowBuilderPreviewNode] = Field(default_factory=list)
    issues: list[FlowBuilderIssue] = Field(default_factory=list)


class FlowBuilderAssistResponse(BaseModel):
    session: FlowBuilderSession
    outcome: FlowBuilderPlannerOutcome
    compilation: FlowBuilderCompilation | None = None
    preview: FlowBuilderPreview | None = None


class FlowBuilderDraftCreateRequest(BaseModel):
    expected_version: int = Field(ge=1)


class FlowBuilderDraftCreateResponse(BaseModel):
    session: FlowBuilderSession
    flow_uuid: UUID
    draft_checksum: str
    already_created: bool = False
