from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SessionExtraction(BaseModel):
    entity: str
    entity_type: str
    entity_address: str
    entity_session_id: str


class OrchTriggerAccepted(BaseModel):
    api_version: str = "v1"
    status: str
    accepted: bool
    flow_uuid: str
    app: str
    persistence: str
    extracted: SessionExtraction
    session_id: int
    session_uuid: str
    session_state: int
    session_created: bool
    workflow_bootstrap: dict | None = None
    workflow_execution: dict | None = None


class OrchSessionSummary(BaseModel):
    api_version: str = "v1"
    id: int
    uuid: str
    flow_uuid: str
    state: int
    entity_origin_app: str | None
    entity: str
    entity_type: str
    entity_address: str
    entity_session_id: str | None
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OrchSessionListResponse(BaseModel):
    api_version: str = "v1"
    total: int
    items: list[OrchSessionSummary]
    next_cursor: str | None = None


class OrchAlarmSummary(BaseModel):
    api_version: str = "v1"
    id: int
    uuid: str
    session_uuid: str | None
    flow_uuid: str | None
    app_name: str | None
    entity: str | None
    entity_type: str | None
    entity_address: str | None
    level: str
    code: str
    message: str
    details: dict
    request_id: str | None
    created_at: datetime


class OrchAlarmListResponse(BaseModel):
    api_version: str = "v1"
    total: int
    items: list[OrchAlarmSummary]
    next_cursor: str | None = None


class OrchErrorResponse(BaseModel):
    api_version: str = "v1"
    code: str
    detail: str
    request_id: str | None


class OrchMigrateWorkspaceResponse(BaseModel):
    api_version: str = "v1"
    workspace_uuid: str
    workspace_schema: str
    applied_versions: list[str]
    skipped_versions: list[str]


class OrchMigrateAllResponse(BaseModel):
    api_version: str = "v1"
    total: int
    items: list[OrchMigrateWorkspaceResponse]


class OrchCreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_name: str
    entity: str
    entity_type: str
    entity_address: str
    payload: dict | None = None
