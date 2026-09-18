from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class OrchUnassignSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_address: str


class OrchUnassignSessionResponse(BaseModel):
    api_version: str = "v1"
    status: str
    flow_uuid: str
    entity_address: str
    updated_count: int


class OrchSwitchBotFlowCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    session_id: str
    status: str
    error: str | None = None


class OrchSwitchBotFlowCallbackResponse(BaseModel):
    api_version: str = "v1"
    status: str
    accepted: bool
    flow_uuid: str
    target_session_id: str
    orch_session_id: int
    orch_session_uuid: str
    idempotent: bool


class OrchDialerSupplierV2TerminalRequest(BaseModel):
    """Terminal decision emitted by Target Core for one pinned dialer cycle."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    cycle_id: UUID
    attempt_id: UUID
    session_uuid: UUID
    flow_revision_id: UUID
    component_ref_id: str = Field(min_length=1, max_length=255)
    outcome: Literal[
        "answered",
        "busy",
        "machine",
        "no_answer",
        "rejected",
        "invalid_number",
        "failed",
        "limit_reached",
    ]
    terminal: Literal[True]
    terminal_reason: str | None = Field(default=None, max_length=255)
    contact_list_member_id: int | None = Field(default=None, gt=0)
    dial_profile_id: UUID | None = None
    dial_profile_revision_id: UUID | None = None
    attempt_policy_id: UUID | None = None
    decision: Literal[
        "next_phone",
        "finish_person",
        "pause_person",
        "block_phone",
    ] | None = None
    decision_source: Literal[
        "telephone_outcome",
        "shared_limit",
        "dial_profile",
        "safety_default",
    ] | None = None
    decision_effective_until: datetime | None = None
    release_mapping_version: Literal["pdial_v1"] | None = None
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_operational_decision_contract(
        self,
    ) -> "OrchDialerSupplierV2TerminalRequest":
        identity_fields = (
            self.contact_list_member_id,
            self.dial_profile_id,
            self.dial_profile_revision_id,
            self.attempt_policy_id,
        )
        if self.decision is None:
            if (
                self.decision_source is not None
                or self.decision_effective_until is not None
                or any(value is not None for value in identity_fields)
            ):
                raise ValueError(
                    "decision é obrigatória quando o contrato operacional V2 é enviado."
                )
            return self
        if self.decision_source is None or any(
            value is None for value in identity_fields
        ):
            raise ValueError(
                "A decisão operacional V2 exige origem, membro e revisões do Perfil."
            )
        if self.decision == "pause_person" and self.decision_effective_until is None:
            raise ValueError(
                "pause_person exige decision_effective_until."
            )
        if (
            self.decision
            not in {"next_phone", "pause_person", "block_phone"}
            and self.decision_effective_until is not None
        ):
            raise ValueError(
                "A decisão informada não aceita decision_effective_until."
            )
        if self.outcome == "answered" and self.decision != "finish_person":
            raise ValueError("answered exige a decisão finish_person.")
        return self


class OrchDialerSupplierV2TerminalResponse(BaseModel):
    api_version: str = "v1"
    status: str
    accepted: bool
    flow_uuid: str
    session_uuid: str
    cycle_id: str
    event_id: str
    idempotent: bool


class OrchChannelSupplierV2CallbackResponse(BaseModel):
    api_version: str = "v1"
    status: Literal["accepted"] = "accepted"
    accepted: bool = True
    channel: Literal["sms", "rcs"]
    event_kind: Literal["dlr", "mo", "status"]
    session_id: int
    session_uuid: str
    accepted_count: int
    inserted_count: int
    idempotent_count: int
    resume_required: bool
    late_callback: bool
    reason: str | None = None


class OrchFlowAliasSummary(BaseModel):
    api_version: str = "v1"
    alias: str
    workspace_uuid: str
    flow_uuid: str
    is_active: bool


class OrchFlowAliasCreateResponse(BaseModel):
    api_version: str = "v1"
    status: str
    item: OrchFlowAliasSummary


class OrchWhatsappLimitUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone: str
    allowed_limit: int = Field(ge=-1)


class OrchWhatsappLimitUpsertResponse(BaseModel):
    api_version: str = "v1"
    status: str
    id: int
    phone: str
    allowed_limit: int
    received_from_meta_at: datetime
    in_use: bool


class OrchResubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    contact_channel_address: str
    contact_identifier: str | None = None
    person_uuid: str | None = None
    external_identifier: str | None = None
    contact_name: str | None = None
    contact_list_member_id: str | None = None
    contact_list_id: str | None = None
    mailing_id: str | None = None
    reason: str | None = None
    payload: dict | None = None


class OrchBillingReprocessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billing_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    reason: str = Field(min_length=1, max_length=1000)


class OrchBillingReprocessResponse(BaseModel):
    api_version: str = "v1"
    status: str
    request_id: str
    workspace_uuid: str
    billing_period: str
    idempotent: bool
    enqueued: bool


class OrchBillingEventCounts(BaseModel):
    pending: int
    batched: int
    sent: int


class OrchBillingSnapshotCounts(BaseModel):
    pending: int
    processing: int
    sent: int
    failed: int
    blocked: int


class OrchBillingStatusResponse(BaseModel):
    api_version: str = "v1"
    workspace_uuid: str
    billing_period: str
    events: OrchBillingEventCounts
    snapshots: OrchBillingSnapshotCounts
    quantity_sent: int
    oldest_pending_at: datetime | None
    max_attempt_count: int
