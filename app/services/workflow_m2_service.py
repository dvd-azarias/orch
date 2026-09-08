from __future__ import annotations

import asyncio
import csv
import copy
import gzip
import hashlib
import io
import json
import math
import os
import posixpath
import re
import subprocess
import textwrap
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.workspace import get_current_workspace_schema, get_current_workspace_uuid
from app.repositories.create_contact_repository import (
    fetch_create_contact_person_by_identifier_for_update,
    fetch_create_contact_person_by_uuid_for_update,
    insert_create_contact_person_if_missing,
    update_create_contact_person_profile,
)
from app.repositories.flow_v2_repository import fetch_flow_row
from app.repositories.identidade_person_repository import (
    ensure_person_in_source_list,
    fetch_active_flow_mailing_link,
    fetch_person_by_identifier_for_update,
    fetch_person_by_uuid_for_update,
    insert_person_if_missing,
    resolve_source_list_by_public_id,
    update_person_from_payload,
)
from app.repositories.orch_channel_events_repository import (
    claim_next_pending_channel_event,
    fetch_channel_event_by_identity,
    fetch_next_pending_channel_event,
    has_pending_channel_events,
    mark_channel_event_processed,
)
from app.repositories.orch_sessions_repository import (
    assign_dialer_routing_for_session,
    assign_whatsapp_routing_for_session,
    clear_session_frozen_until,
    ensure_session_workflow_revision_pin,
    fetch_contact_runtime_context_for_session,
    fetch_session_webhook_snapshot,
    fetch_session_workflow_state,
    persist_contact_member_outbound_hsm,
    replace_session_workflow_state,
)
from app.repositories.select_contact_channel_repository import (
    fetch_select_contact_channel_candidate,
    rebind_person_session_to_contact_channel,
)
from app.repositories.send_with_sms_repository import assign_sms_routing_for_session
from app.repositories.workspaces_repository import fetch_workspace_otima_billing_api_key
from app.services.dialer_release_mapper import resolve_dialer_status_from_release
from app.services.generate_file_dispatch_service import upsert_job_and_buffer_row
from app.services.identidade_person_service import (
    IdentidadePersonQueryResult,
    IdentidadePersonServiceError,
    build_normalized_output,
    mask_document,
    merge_person_payload,
    normalize_document,
    normalize_identidade_person,
    normalize_workspace_id,
    query_identidade_person,
)
from app.services.otima_llm_service import execute_otima_llm_prompt
from app.services.phone_normalizer import normalize_phone_to_canonical_ani
from app.services.session_metrics_service import persist_session_metrics
from app.services.switch_bot_flow_service import (
    SwitchBotFlowError,
    extract_meta_message_ids,
    is_meta_user_message_payload,
    resolve_target_flow_uuid,
)
from app.services.workflow_engine import (
    component_kind,
    extract_edges,
    index_components,
    outgoing_branch_labels,
    resolve_next_card_uuid,
    resolve_next_card_uuid_by_branch,
)
from app.services.workflow_revision_service import resolve_workflow_revision_for_session

_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
logger = get_logger(__name__)
WHATSAPP_BLOCKING_STOP_REASONS_BY_KIND = {
    "send_with_whatsapp": "blocked_send_with_whatsapp",
    "send_whatsapp_interactive": "blocked_send_whatsapp_interactive",
    "process_whatsapp_response": "blocked_process_whatsapp_response",
    "send_with_dialer": "blocked_send_with_dialer",
    "send_with_sms": "blocked_send_with_sms",
    "process_dialer_response": "blocked_process_dialer_response",
    "run_flow": "blocked_run_flow",
    "switch_bot_flow": "blocked_switch_bot_flow",
    "identidade_person": "blocked_identidade_person_flow_link",
}
WAIT_FOR_EVENT_BLOCKING_STOP_REASON = "blocked_wait_for_event"
WAIT_FOR_EVENT_RESULT_RE = re.compile(r"[A-Za-z0-9._:-]+$")
WAIT_FOR_EVENT_OUTPUT_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
WAIT_FOR_EVENT_MAX_TIMEOUT_SECONDS = 30 * 24 * 60 * 60
SPLIT_RANDOM_OUTPUT_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
SPLIT_RANDOM_HASH_STRATEGY = "sha256_mod_100_v1"
SELECT_CONTACT_CHANNEL_TYPES = {"voice", "whatsapp", "sms", "email"}
SELECT_CONTACT_CHANNEL_OUTPUT_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
SELECT_CONTACT_CHANNEL_MAX_LABEL_LENGTH = 128

WHATSAPP_RESPONSE_BRANCH_BY_STATUS = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "limit_reached": "limit_reached",
}
SEND_WITH_WHATSAPP_LIMIT_EXHAUSTED_ACTUATORS = {
    "whatsapp_without_limit",
    "whatsapp_without_limit_by_rate_limit",
}

WHATSAPP_BLOCKING_STOP_REASONS = {
    "blocked_send_with_whatsapp",
    "blocked_send_whatsapp_interactive",
    "blocked_process_whatsapp_response",
}
DIALER_BLOCKING_STOP_REASONS = {
    "blocked_send_with_dialer",
    "blocked_process_dialer_response",
}
RUN_FLOW_BLOCKING_STOP_REASONS = {
    "blocked_run_flow",
}
SWITCH_BOT_FLOW_BLOCKING_STOP_REASONS = {
    "blocked_switch_bot_flow",
}
IDENTIDADE_PERSON_BLOCKING_STOP_REASONS = {
    "blocked_identidade_person_flow_link",
}
WHATSAPP_STATUS_ORDER_PREREQUISITES = {
    "delivered": "whatsapp_sent_at",
    "read": "whatsapp_delivered_at",
}
WHATSAPP_STATUS_ORDER_TTL_SECONDS = {
    "delivered": 20,
    "read": 45,
}
WHATSAPP_STATUS_ORDER_RETRY_SECONDS = 3
DIALER_RESPONSE_BRANCH_BY_STATUS = {
    "answered": "answered",
    "busy": "busy",
    "rejected": "rejected",
    "invalid_number": "invalid_number",
    "no_answer": "no_answer",
    "failed": "failed",
    "machine": "machine",
}
FINISH_FLOW_WEBHOOK_DISPATCHED_REASON = "finish_flow_webhook_dispatched"
LOOP_GUARD_WORKFLOW_META_KEY = "loop_guard"
LOOP_GUARD_COUNTER_KEY = "continuous_steps"
LOOP_GUARD_LAST_TRANSITION_KEY = "last_transition_signature"
POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807
TERMINAL_WORKFLOW_ERROR_CODES = {
    "api_call_missing_url",
    "condition_branch_not_mapped",
    "contact_member_routing_update_failed",
    "person_scope_channel_component_not_supported",
    "select_contact_channel_invalid_channel_label",
    "select_contact_channel_invalid_channel_type",
    "select_contact_channel_invalid_output_var",
    "select_contact_channel_missing_contact_context",
    "select_contact_channel_persistence_failed",
    "select_contact_channel_rebind_failed",
    "send_with_sms_contact_not_eligible",
    "split_random_invalid_branches",
    "split_random_invalid_output_var",
    "split_random_invalid_percentage",
    "split_random_invalid_total",
    "whatsapp_hsm_contact_missing",
    "whatsapp_hsm_meta_payload_invalid",
    "whatsapp_hsm_number_not_configured",
    "whatsapp_hsm_persist_failed",
    "whatsapp_hsm_template_missing",
    "whatsapp_hsm_variable_unresolved",
    "wait_for_event_invalid_event_source",
    "wait_for_event_invalid_event_result",
    "wait_for_event_invalid_timeout_seconds",
    "wait_for_event_invalid_output_var",
    "wait_for_event_state_mismatch",
}
WHATSAPP_HSM_ERROR_CODES = {
    code for code in TERMINAL_WORKFLOW_ERROR_CODES if code.startswith("whatsapp_hsm_")
}
WHATSAPP_HSM_COMPONENT_KINDS = {
    "send_with_whatsapp",
    "send_whatsapp_interactive",
    "send_whatsapp_template",
}
PERSON_SCOPE_CHANNEL_COMPONENT_KINDS = {
    "send_with_dialer",
    "send_with_sms",
    "send_with_whatsapp",
    "send_whatsapp_interactive",
    "send_whatsapp_template",
}


@dataclass(frozen=True)
class WorkflowExecutionResult:
    enabled: bool
    executed_steps: int
    stopped_reason: str
    last_card_uuid: str | None
    next_card_uuid: str | None


@dataclass(frozen=True)
class _WaitForEventExecution:
    branch_label: str | None
    timeout_at: datetime


@dataclass(frozen=True)
class _SelectContactChannelExecution:
    branch_label: str
    contact_row: dict[str, Any] | None


@dataclass(frozen=True)
class _ContactMemberRoutingScope:
    contact_list_member_id: int | None
    contact_list_id: str | None
    mailing_id: int | None
    explicit: bool
    valid: bool

    def selectors(self) -> dict[str, Any]:
        return {
            "contact_list_member_id": self.contact_list_member_id,
            "contact_list_id": self.contact_list_id,
            "mailing_id": self.mailing_id,
        }


class WorkflowExecutionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _blocking_stop_reason_for_component(kind: str) -> str | None:
    return WHATSAPP_BLOCKING_STOP_REASONS_BY_KIND.get(kind)


def _mark_blocking_execution(runtime_variables: dict[str, Any], *, stopped_reason: str) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    workflow_meta["blocking_execution"] = True
    workflow_meta["blocking_stop_reason"] = stopped_reason


def _clear_blocking_execution(runtime_variables: dict[str, Any]) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    workflow_meta["blocking_execution"] = False
    workflow_meta.pop("blocking_stop_reason", None)


def _read_enabled(settings: Any) -> bool:
    raw = getattr(settings, "workflow_v2_execute_m2", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _read_contextual_member_routing_enabled(settings: Any) -> bool:
    raw = getattr(settings, "workflow_contextual_member_routing_enabled", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _read_session_scope(runtime_variables: dict[str, Any]) -> str:
    input_payload = runtime_variables.get("input_payload")
    if not isinstance(input_payload, dict):
        return "channel"
    value = str(input_payload.get("session_scope") or "channel").strip().lower()
    return "person" if value == "person" else "channel"


def _contextual_member_routing_enabled_for_scope(
    *,
    feature_enabled: bool,
    session_scope: str,
) -> bool:
    return feature_enabled or session_scope == "person"


def _normalize_channel_type(raw_value: Any) -> str | None:
    value = str(raw_value or "").strip().lower()
    if not value:
        return None
    if value in {"phone", "voice"}:
        return "voice"
    return value


def _contact_member_channel_type_matches(
    runtime_variables: dict[str, Any],
    contact_row: dict[str, Any],
    *,
    expected_channel_type: str | None = None,
) -> bool:
    expected_type = _normalize_channel_type(expected_channel_type)
    if expected_type is None:
        input_payload = runtime_variables.get("input_payload")
        if not isinstance(input_payload, dict):
            return True
        expected_type = _normalize_channel_type(input_payload.get("channel_type"))
    if expected_type is None:
        return True
    return expected_type == _normalize_channel_type(
        contact_row.get("contact_channel_type")
    )


def _active_selected_contact_channel(
    runtime_variables: dict[str, Any],
) -> dict[str, Any] | None:
    workflow_meta = runtime_variables.get("workflow_v2")
    if not isinstance(workflow_meta, dict):
        return None
    raw_selection = workflow_meta.get("selected_contact_channel")
    if not isinstance(raw_selection, dict):
        return None
    if raw_selection.get("selected") is not True:
        return None
    if str(raw_selection.get("session_scope") or "").strip().lower() != "person":
        return None

    try:
        contact_list_member_id = int(raw_selection.get("contact_list_member_id"))
        mailing_id = int(raw_selection.get("mailing_id"))
        contact_list_id = str(UUID(str(raw_selection.get("contact_list_id"))))
    except (TypeError, ValueError):
        return None
    channel_type = _normalize_channel_type(raw_selection.get("type"))
    address = str(raw_selection.get("address") or "").strip()
    if (
        contact_list_member_id <= 0
        or mailing_id <= 0
        or channel_type not in SELECT_CONTACT_CHANNEL_TYPES
        or not address
    ):
        return None

    try:
        person_uuid = str(UUID(str(raw_selection.get("person_uuid"))))
    except (TypeError, ValueError):
        return None

    return {
        **raw_selection,
        "contact_list_member_id": contact_list_member_id,
        "contact_list_id": contact_list_id,
        "mailing_id": mailing_id,
        "person_uuid": person_uuid,
        "type": channel_type,
        "address": address,
    }


def _routing_scope_from_selected_contact_channel(
    selection: dict[str, Any],
) -> _ContactMemberRoutingScope:
    return _ContactMemberRoutingScope(
        contact_list_member_id=int(selection["contact_list_member_id"]),
        contact_list_id=str(selection["contact_list_id"]),
        mailing_id=int(selection["mailing_id"]),
        explicit=True,
        valid=True,
    )


def _ensure_person_scope_component_supported(
    *,
    session_scope: str,
    component_kind_value: str,
    selected_contact_channel: dict[str, Any] | None = None,
) -> None:
    if (
        session_scope == "person"
        and component_kind_value in PERSON_SCOPE_CHANNEL_COMPONENT_KINDS
        and selected_contact_channel is None
    ):
        raise WorkflowExecutionError(
            "person_scope_channel_component_not_supported",
            "Sessões por pessoa precisam selecionar explicitamente um canal antes de executar componentes de comunicação.",
        )


def _read_max_steps(settings: Any) -> int:
    raw = getattr(settings, "workflow_v2_max_steps", 25)
    try:
        value = int(raw)
    except Exception:
        value = 25
    return max(1, min(200, value))


def _read_loop_guard_repeat_threshold(settings: Any) -> int:
    raw = getattr(settings, "workflow_m2_loop_guard_repeat_threshold", 300)
    try:
        value = int(raw)
    except Exception:
        value = 300
    return max(1, min(5000, value))


def _to_uuid_or_none(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    try:
        return str(UUID(str(raw_value)))
    except Exception:
        return None


def _extract_contact_member_routing_scope(
    runtime_variables: dict[str, Any],
) -> _ContactMemberRoutingScope:
    input_payload = runtime_variables.get("input_payload")
    if not isinstance(input_payload, dict):
        return _ContactMemberRoutingScope(None, None, None, False, True)

    def _non_empty_text(raw_value: Any) -> str | None:
        if raw_value is None:
            return None
        value = str(raw_value).strip()
        return value or None

    raw_member_id = _non_empty_text(input_payload.get("contact_list_member_id"))
    raw_contact_list_id = _non_empty_text(input_payload.get("contact_list_id"))
    raw_mailing_id = _non_empty_text(input_payload.get("mailing_id"))
    explicit = any((raw_member_id, raw_contact_list_id, raw_mailing_id))
    valid = True

    contact_list_member_id: int | None = None
    if raw_member_id is not None:
        try:
            contact_list_member_id = int(raw_member_id)
        except (TypeError, ValueError):
            valid = False
        else:
            if contact_list_member_id <= 0 or contact_list_member_id > POSTGRES_BIGINT_MAX:
                contact_list_member_id = None
                valid = False

    contact_list_id: str | None = None
    if raw_contact_list_id is not None:
        contact_list_id = _to_uuid_or_none(raw_contact_list_id)
        if contact_list_id is None:
            valid = False

    mailing_id: int | None = None
    if raw_mailing_id is not None:
        try:
            mailing_id = int(raw_mailing_id)
        except (TypeError, ValueError):
            valid = False
        else:
            if mailing_id <= 0 or mailing_id > POSTGRES_BIGINT_MAX:
                mailing_id = None
                valid = False

    return _ContactMemberRoutingScope(
        contact_list_member_id=contact_list_member_id,
        contact_list_id=contact_list_id,
        mailing_id=mailing_id,
        explicit=explicit,
        valid=valid,
    )


def _resolved_contact_member_id_for_routing(
    scope: _ContactMemberRoutingScope,
    resolved_contact_list_member_id: int | None,
) -> int | None:
    if not scope.explicit:
        return None
    return resolved_contact_list_member_id


def _ensure_workflow_meta(runtime_variables: dict[str, Any]) -> dict[str, Any]:
    workflow_meta = runtime_variables.get("workflow_v2")
    if isinstance(workflow_meta, dict):
        return workflow_meta
    workflow_meta = {}
    runtime_variables["workflow_v2"] = workflow_meta
    return workflow_meta


def _ensure_loop_guard_meta(runtime_variables: dict[str, Any]) -> dict[str, Any]:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    loop_guard_meta = workflow_meta.get(LOOP_GUARD_WORKFLOW_META_KEY)
    if isinstance(loop_guard_meta, dict):
        return loop_guard_meta
    loop_guard_meta = {}
    workflow_meta[LOOP_GUARD_WORKFLOW_META_KEY] = loop_guard_meta
    return loop_guard_meta


def _register_loop_guard_step(
    runtime_variables: dict[str, Any],
    *,
    transition_signature: str | None,
) -> int:
    loop_guard_meta = _ensure_loop_guard_meta(runtime_variables)
    raw_counter = loop_guard_meta.get(LOOP_GUARD_COUNTER_KEY, 0)
    try:
        counter = int(raw_counter)
    except Exception:
        counter = 0
    counter = max(0, counter) + 1
    loop_guard_meta[LOOP_GUARD_COUNTER_KEY] = counter
    if transition_signature:
        loop_guard_meta[LOOP_GUARD_LAST_TRANSITION_KEY] = transition_signature
    return counter


def _reset_loop_guard_counter(runtime_variables: dict[str, Any]) -> None:
    loop_guard_meta = _ensure_loop_guard_meta(runtime_variables)
    loop_guard_meta[LOOP_GUARD_COUNTER_KEY] = 0


def _read_next_cursor(runtime_variables: dict[str, Any]) -> str | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    raw = workflow_meta.get("next_card_cursor")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _read_blocking_stop_reason(runtime_variables: dict[str, Any]) -> str | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    if not workflow_meta.get("blocking_execution"):
        return None
    raw = workflow_meta.get("blocking_stop_reason")
    if raw is None:
        return "blocked_send_with_whatsapp"
    text = str(raw).strip()
    return text or "blocked_send_with_whatsapp"


def _set_cursors(runtime_variables: dict[str, Any], *, last_cursor: str | None, next_cursor: str | None) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    workflow_meta["last_card_cursor"] = last_cursor
    workflow_meta["next_card_cursor"] = next_cursor


def _read_whatsapp_resume_cursor(runtime_variables: dict[str, Any]) -> str | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    channel_resume = workflow_meta.get("channel_resume")
    if not isinstance(channel_resume, dict):
        return None
    whatsapp_resume = channel_resume.get("whatsapp")
    if not isinstance(whatsapp_resume, dict):
        return None
    raw = whatsapp_resume.get("process_card_cursor")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _read_dialer_resume_cursor(runtime_variables: dict[str, Any]) -> str | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    channel_resume = workflow_meta.get("channel_resume")
    if not isinstance(channel_resume, dict):
        return None
    dialer_resume = channel_resume.get("dialer")
    if not isinstance(dialer_resume, dict):
        return None
    raw = dialer_resume.get("process_card_cursor")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _set_whatsapp_resume_cursor(runtime_variables: dict[str, Any], *, process_card_cursor: str | None) -> None:
    if not process_card_cursor:
        return
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    channel_resume = workflow_meta.get("channel_resume")
    if not isinstance(channel_resume, dict):
        channel_resume = {}
        workflow_meta["channel_resume"] = channel_resume
    whatsapp_resume = channel_resume.get("whatsapp")
    if not isinstance(whatsapp_resume, dict):
        whatsapp_resume = {}
        channel_resume["whatsapp"] = whatsapp_resume
    whatsapp_resume["process_card_cursor"] = process_card_cursor


def _set_dialer_resume_cursor(runtime_variables: dict[str, Any], *, process_card_cursor: str | None) -> None:
    if not process_card_cursor:
        return
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    channel_resume = workflow_meta.get("channel_resume")
    if not isinstance(channel_resume, dict):
        channel_resume = {}
        workflow_meta["channel_resume"] = channel_resume
    dialer_resume = channel_resume.get("dialer")
    if not isinstance(dialer_resume, dict):
        dialer_resume = {}
        channel_resume["dialer"] = dialer_resume
    dialer_resume["process_card_cursor"] = process_card_cursor


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_callback_payload_from_runtime(runtime_variables: dict[str, Any]) -> dict[str, Any] | None:
    callbacks_pending = runtime_variables.get("callbacks_pending")
    if isinstance(callbacks_pending, list):
        for callback in callbacks_pending:
            if not isinstance(callback, dict):
                continue
            entity = str(callback.get("entity", "")).strip()
            result = str(callback.get("result", "")).strip().lower()
            if entity and result:
                return callback

    callback = runtime_variables.get("callback")
    if not isinstance(callback, dict):
        return None
    entity = str(callback.get("entity", "")).strip()
    result = str(callback.get("result", "")).strip().lower()
    if not entity or not result:
        return None
    return callback


def _set_run_flow_waiting(runtime_variables: dict[str, Any], *, card_cursor: str | None) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    workflow_meta["run_flow_waiting"] = {
        "card_cursor": card_cursor,
        "blocked_at": datetime.now(timezone.utc).isoformat(),
    }


def _clear_run_flow_waiting(runtime_variables: dict[str, Any]) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    workflow_meta.pop("run_flow_waiting", None)


def _is_new_callback_for_waiting_run_flow(runtime_variables: dict[str, Any]) -> bool:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    waiting = workflow_meta.get("run_flow_waiting")
    if not isinstance(waiting, dict):
        return _extract_callback_payload_from_runtime(runtime_variables) is not None

    blocked_at = _parse_iso_datetime(waiting.get("blocked_at"))
    if blocked_at is None:
        return True

    callbacks_pending = runtime_variables.get("callbacks_pending")
    if isinstance(callbacks_pending, list):
        for callback in callbacks_pending:
            if not isinstance(callback, dict):
                continue
            entity = str(callback.get("entity", "")).strip()
            result = str(callback.get("result", "")).strip().lower()
            if not entity or not result:
                continue
            callback_at = _parse_iso_datetime(callback.get("received_at"))
            if callback_at is None or callback_at >= blocked_at:
                return True
        return False

    callback = _extract_callback_payload_from_runtime(runtime_variables)
    if not isinstance(callback, dict):
        return False
    callback_at = _parse_iso_datetime(callback.get("received_at"))
    if callback_at is None:
        return True
    return callback_at >= blocked_at


def _consume_callback_for_run_flow(runtime_variables: dict[str, Any]) -> dict[str, Any] | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    waiting = workflow_meta.get("run_flow_waiting")
    blocked_at = _parse_iso_datetime(waiting.get("blocked_at")) if isinstance(waiting, dict) else None

    callbacks_pending = runtime_variables.get("callbacks_pending")
    if isinstance(callbacks_pending, list):
        selected_callback: dict[str, Any] | None = None
        retained: list[Any] = []
        for item in callbacks_pending:
            if not isinstance(item, dict):
                continue
            entity = str(item.get("entity", "")).strip()
            result = str(item.get("result", "")).strip().lower()
            if not entity or not result:
                continue
            callback_at = _parse_iso_datetime(item.get("received_at"))
            if blocked_at is not None and callback_at is not None and callback_at < blocked_at:
                continue
            if selected_callback is None:
                selected_callback = item
                continue
            retained.append(item)

        if selected_callback is not None:
            runtime_variables["callbacks_pending"] = retained
            runtime_variables["callback"] = selected_callback
            return selected_callback

    callback = runtime_variables.get("callback")
    if not isinstance(callback, dict):
        return None
    entity = str(callback.get("entity", "")).strip()
    result = str(callback.get("result", "")).strip().lower()
    if not entity or not result:
        return None
    if blocked_at is not None:
        callback_at = _parse_iso_datetime(callback.get("received_at"))
        if callback_at is not None and callback_at < blocked_at:
            return None
    return callback


def _extract_whatsapp_status_signature_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("object") != "whatsapp_business_account":
        return None
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            statuses = value.get("statuses")
            if not isinstance(statuses, list):
                continue
            for item in statuses:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", "")).strip().lower()
                if not status:
                    continue
                message_id = str(item.get("id", "")).strip()
                timestamp = str(item.get("timestamp", "")).strip()
                recipient_id = str(item.get("recipient_id", "")).strip()
                return f"{status}|{message_id}|{timestamp}|{recipient_id}"
    return None


def _extract_whatsapp_status_signature_from_runtime(runtime_variables: dict[str, Any]) -> str | None:
    if not isinstance(runtime_variables, dict):
        return None
    signature = _extract_whatsapp_status_signature_from_payload(runtime_variables.get("last_payload"))
    if signature is not None:
        return signature
    signature = _extract_whatsapp_status_signature_from_payload(runtime_variables.get("input_payload"))
    if signature is not None:
        return signature
    variables = runtime_variables.get("variables")
    if not isinstance(variables, dict):
        return None
    return _extract_whatsapp_status_signature_from_payload(variables.get("payload"))


def _read_whatsapp_last_preempt_signature(runtime_variables: dict[str, Any]) -> str | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    channel_resume = workflow_meta.get("channel_resume")
    if not isinstance(channel_resume, dict):
        return None
    whatsapp_resume = channel_resume.get("whatsapp")
    if not isinstance(whatsapp_resume, dict):
        return None
    raw = whatsapp_resume.get("last_preempt_signature")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _set_whatsapp_last_preempt_signature(runtime_variables: dict[str, Any], signature: str) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    channel_resume = workflow_meta.get("channel_resume")
    if not isinstance(channel_resume, dict):
        channel_resume = {}
        workflow_meta["channel_resume"] = channel_resume
    whatsapp_resume = channel_resume.get("whatsapp")
    if not isinstance(whatsapp_resume, dict):
        whatsapp_resume = {}
        channel_resume["whatsapp"] = whatsapp_resume
    whatsapp_resume["last_preempt_signature"] = signature


def _should_preempt_to_whatsapp_resume_cursor(
    runtime_variables: dict[str, Any],
    *,
    has_pending_whatsapp_events: bool = False,
    current_next_card_uuid: str | None = None,
    blocking_stop_reason: str | None = None,
) -> bool:
    resume_cursor = _read_whatsapp_resume_cursor(runtime_variables)
    if has_pending_whatsapp_events and resume_cursor is not None:
        if current_next_card_uuid is None:
            return True
        if str(current_next_card_uuid) == str(resume_cursor):
            return True
        if blocking_stop_reason in WHATSAPP_BLOCKING_STOP_REASONS:
            return True
        return False

    status = _extract_whatsapp_status_from_runtime(runtime_variables)
    if status not in WHATSAPP_RESPONSE_BRANCH_BY_STATUS:
        return False
    if resume_cursor is None:
        return False
    signature = _extract_whatsapp_status_signature_from_runtime(runtime_variables)
    if signature is None:
        return False
    return signature != _read_whatsapp_last_preempt_signature(runtime_variables)


def _extract_whatsapp_status_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("object") != "whatsapp_business_account":
        return None
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            statuses = value.get("statuses")
            if not isinstance(statuses, list):
                continue
            for item in statuses:
                if not isinstance(item, dict):
                    continue
                raw = item.get("status")
                if raw is None:
                    continue
                text = str(raw).strip().lower()
                if text:
                    return text
    return None


def _extract_whatsapp_status_from_runtime(runtime_variables: dict[str, Any]) -> str | None:
    if not isinstance(runtime_variables, dict):
        return None

    status = _extract_whatsapp_status_from_payload(runtime_variables.get("last_payload"))
    if status is not None:
        return status

    status = _extract_whatsapp_status_from_payload(runtime_variables.get("input_payload"))
    if status is not None:
        return status

    variables = runtime_variables.get("variables")
    if not isinstance(variables, dict):
        return None
    return _extract_whatsapp_status_from_payload(variables.get("payload"))


def _normalize_whatsapp_message_branch_key(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    text = str(raw_value).strip().lower()
    if not text:
        return None
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return normalized or None


def _extract_whatsapp_message_branch_key_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("object") != "whatsapp_business_account":
        return None
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            messages = value.get("messages")
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                interactive = message.get("interactive")
                if isinstance(interactive, dict):
                    button_reply = interactive.get("button_reply")
                    if isinstance(button_reply, dict):
                        key = _normalize_whatsapp_message_branch_key(button_reply.get("id"))
                        if key:
                            return key
                    list_reply = interactive.get("list_reply")
                    if isinstance(list_reply, dict):
                        key = _normalize_whatsapp_message_branch_key(list_reply.get("id"))
                        if key:
                            return key
                button = message.get("button")
                if isinstance(button, dict):
                    key = _normalize_whatsapp_message_branch_key(button.get("payload"))
                    if key:
                        return key
                text_payload = message.get("text")
                if isinstance(text_payload, dict):
                    key = _normalize_whatsapp_message_branch_key(text_payload.get("body"))
                    if key:
                        return key
    return None


def _extract_whatsapp_message_branch_key_from_runtime(runtime_variables: dict[str, Any]) -> str | None:
    if not isinstance(runtime_variables, dict):
        return None
    key = _extract_whatsapp_message_branch_key_from_payload(runtime_variables.get("last_payload"))
    if key is not None:
        return key
    key = _extract_whatsapp_message_branch_key_from_payload(runtime_variables.get("input_payload"))
    if key is not None:
        return key
    variables = runtime_variables.get("variables")
    if not isinstance(variables, dict):
        return None
    return _extract_whatsapp_message_branch_key_from_payload(variables.get("payload"))


def _extract_whatsapp_provider_number_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("object") != "whatsapp_business_account":
        return None
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            if not isinstance(metadata, dict):
                continue
            number = normalize_phone_to_canonical_ani(metadata.get("display_phone_number"))
            if number:
                return str(number).strip()
    return None


def _extract_dialer_status_from_payload(payload: Any) -> str | None:
    return resolve_dialer_status_from_release(payload)


def _extract_dialer_status_from_runtime(runtime_variables: dict[str, Any]) -> str | None:
    if not isinstance(runtime_variables, dict):
        return None

    status = _extract_dialer_status_from_payload(runtime_variables.get("last_payload"))
    if status is not None:
        return status

    status = _extract_dialer_status_from_payload(runtime_variables.get("input_payload"))
    if status is not None:
        return status

    variables = runtime_variables.get("variables")
    if not isinstance(variables, dict):
        return None
    return _extract_dialer_status_from_payload(variables.get("payload"))


def _set_synthetic_whatsapp_status_payload(
    runtime_variables: dict[str, Any],
    *,
    status: str,
    reason: str,
) -> None:
    runtime_variables["last_payload"] = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "status": status,
                                    "id": f"orch.synthetic.{status}",
                                    "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                                    "recipient_id": "",
                                    "origin_reason": reason,
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }


def _run_process_whatsapp_response(
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str | None:
    status = _extract_whatsapp_status_from_runtime(runtime_variables)
    if status is None:
        return None
    branch = WHATSAPP_RESPONSE_BRANCH_BY_STATUS.get(status)

    runtime_variables["whatsapp_last_response"] = {
        "component_ref_id": component.get("ref_id"),
        "status": status,
        "branch": branch,
    }
    return branch


def _run_process_dialer_response(
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str | None:
    status = _extract_dialer_status_from_runtime(runtime_variables)
    if status is None:
        return None
    branch = DIALER_RESPONSE_BRANCH_BY_STATUS.get(status)
    runtime_variables["dialer_last_response"] = {
        "component_ref_id": component.get("ref_id"),
        "status": status,
        "branch": branch,
    }
    return branch


def _extract_cache_mapping_entries(component: dict[str, Any]) -> list[tuple[str, Any]]:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    raw_mapping = params.get("mapping")
    rows: list[tuple[str, Any]] = []
    if isinstance(raw_mapping, list):
        for item in raw_mapping:
            if not isinstance(item, dict):
                continue
            raw_key = item.get("key") or item.get("field") or item.get("name")
            if raw_key is None:
                continue
            rows.append((str(raw_key), item.get("value")))
    elif isinstance(raw_mapping, dict):
        for raw_key, value in raw_mapping.items():
            rows.append((str(raw_key), value))
    return rows


def _resolve_cache_post_names_for_definition(
    *,
    definition: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> set[str]:
    variables = _ensure_variables(runtime_variables)
    names: set[str] = set()
    components = definition.get("components") if isinstance(definition.get("components"), list) else []
    for component in components:
        if not isinstance(component, dict):
            continue
        if component_kind(component) != "cache_post":
            continue
        params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
        resolved = _render_value(params.get("name"), variables)
        name = str(resolved or "").strip()
        if name:
            names.add(name)
    return names


def _read_cache_ttl_days(raw_value: Any) -> int:
    if raw_value is None:
        return 7
    try:
        parsed = int(float(str(raw_value).strip()))
    except Exception:
        return 7
    if parsed <= 0:
        return 7
    return min(parsed, 3650)


async def _run_cache_post(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    variables = _ensure_variables(runtime_variables)

    name_raw = _render_value(params.get("name"), variables)
    name = str(name_raw or "").strip()
    primary_key_field_raw = _render_value(params.get("primary_key_field"), variables)
    primary_key_field = str(primary_key_field_raw or "").strip()
    mapping_entries = _extract_cache_mapping_entries(component)
    ttl_days = _read_cache_ttl_days(_render_value(params.get("ttl_days"), variables))

    flow_uuid_safe = _to_uuid_or_none(flow_uuid)
    card_uuid_safe = _to_uuid_or_none(
        str(component.get("ref_id") or component.get("uuid") or component.get("id") or "").strip() or None
    )

    if not flow_uuid_safe or not card_uuid_safe or not name or not primary_key_field or not mapping_entries:
        raise WorkflowExecutionError(
            "cache_post.invalid_parameters",
            "cache_post requer flow/card UUID válidos, name, primary_key_field e mapping.",
        )

    payload: dict[str, Any] = {}
    for raw_key, raw_value in mapping_entries:
        key = str(raw_key or "").strip()
        if not key:
            continue
        payload[key] = _render_value(raw_value, variables)

    if primary_key_field not in payload:
        raise WorkflowExecutionError(
            "cache_post.invalid_parameters",
            f"Campo primary_key_field '{primary_key_field}' ausente no mapping resolvido.",
        )
    cache_key = str(payload.get(primary_key_field) or "").strip()
    if not cache_key:
        raise WorkflowExecutionError(
            "cache_post.invalid_parameters",
            f"Valor de cache_key vazio para primary_key_field '{primary_key_field}'.",
        )

    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    try:
        await db_session.execute(
            text(
                """
                DELETE FROM cache_card_store
                WHERE flow_uuid = CAST(:flow_uuid AS uuid)
                  AND name = :name
                  AND cache_key = :cache_key
                  AND card_uuid <> CAST(:card_uuid AS uuid)
                """
            ),
            {
                "flow_uuid": flow_uuid_safe,
                "card_uuid": card_uuid_safe,
                "name": name,
                "cache_key": cache_key,
            },
        )
        await db_session.execute(
            text(
                """
                INSERT INTO cache_card_store (
                    flow_uuid,
                    card_uuid,
                    name,
                    cache_key,
                    data,
                    expires_at,
                    created_at,
                    updated_at
                ) VALUES (
                    CAST(:flow_uuid AS uuid),
                    CAST(:card_uuid AS uuid),
                    :name,
                    :cache_key,
                    CAST(:data AS jsonb),
                    :expires_at,
                    NOW(),
                    NOW()
                )
                ON CONFLICT (flow_uuid, card_uuid, cache_key)
                DO UPDATE
                   SET name = EXCLUDED.name,
                       data = EXCLUDED.data,
                       expires_at = EXCLUDED.expires_at,
                       updated_at = NOW()
                """
            ),
            {
                "flow_uuid": flow_uuid_safe,
                "card_uuid": card_uuid_safe,
                "name": name,
                "cache_key": cache_key,
                "data": json.dumps(payload, ensure_ascii=False),
                "expires_at": expires_at,
            },
        )
    except Exception as exc:
        raise WorkflowExecutionError(
            "cache_post.persist_failed",
            f"Falha ao persistir cache_post: {exc}",
        ) from exc

    runtime_variables["cache_post_last_result"] = {
        "component_ref_id": component.get("ref_id"),
        "flow_uuid": flow_uuid_safe,
        "name": name,
        "cache_key": cache_key,
        "primary_key_field": primary_key_field,
        "ttl_days": ttl_days,
        "expires_at": expires_at.isoformat(),
        "data": payload,
    }
    return "proximo"


async def _run_cache_get(
    *,
    db_session: AsyncSession,
    definition: dict[str, Any],
    flow_uuid: str,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
    branch_labels: list[str],
) -> str:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    variables = _ensure_variables(runtime_variables)

    name_raw = _render_value(params.get("name"), variables)
    name = str(name_raw or "").strip()
    key_raw = _render_value(params.get("key"), variables)
    cache_key = str(key_raw or "").strip()
    output_var_raw = _render_value(params.get("output_var"), variables)
    output_var = str(output_var_raw or "").strip() or name

    flow_uuid_safe = _to_uuid_or_none(flow_uuid)
    if not flow_uuid_safe or not name or not cache_key:
        raise WorkflowExecutionError(
            "cache_get.invalid_parameters",
            "cache_get requer flow_uuid, name e key válidos.",
        )

    configured_names = _resolve_cache_post_names_for_definition(
        definition=definition,
        runtime_variables=runtime_variables,
    )
    if name not in configured_names:
        raise WorkflowExecutionError(
            "cache_get.cache_name_not_configured",
            f"Nome de cache '{name}' não configurado em nenhum cache_post do flow.",
        )

    try:
        query_result = await db_session.execute(
            text(
                """
                SELECT data
                FROM cache_card_store
                WHERE flow_uuid = CAST(:flow_uuid AS uuid)
                  AND name = :name
                  AND cache_key = :cache_key
                  AND expires_at > NOW()
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """
            ),
            {
                "flow_uuid": flow_uuid_safe,
                "name": name,
                "cache_key": cache_key,
            },
        )
    except Exception as exc:
        raise WorkflowExecutionError(
            "cache_get.fetch_failed",
            f"Falha ao consultar cache_get: {exc}",
        ) from exc

    row = query_result.first()
    if row is None:
        runtime_variables["cache_get_last_result"] = {
            "component_ref_id": component.get("ref_id"),
            "flow_uuid": flow_uuid_safe,
            "name": name,
            "cache_key": cache_key,
            "output_var": output_var,
            "found": False,
        }
        normalized_labels = [str(label or "").strip().lower() for label in branch_labels if str(label or "").strip()]
        if "nao_encontrado" not in normalized_labels:
            raise WorkflowExecutionError(
                "cache_get.not_found",
                f"Cache não encontrado para name='{name}' e key='{cache_key}'.",
            )
        return "nao_encontrado"

    payload = row[0]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {"value": payload}

    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs
    customs[output_var] = payload
    _set_by_path(variables, output_var, payload)

    runtime_variables["cache_get_last_result"] = {
        "component_ref_id": component.get("ref_id"),
        "flow_uuid": flow_uuid_safe,
        "name": name,
        "cache_key": cache_key,
        "output_var": output_var,
        "found": True,
        "data": payload,
    }
    return "encontrado"


def _resolve_send_with_dialer_branch_label(
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str | None:
    status = _extract_dialer_status_from_runtime(runtime_variables)
    if status is None:
        return None
    branch = DIALER_RESPONSE_BRANCH_BY_STATUS.get(status)
    runtime_variables["dialer_last_response"] = {
        "component_ref_id": component.get("ref_id"),
        "status": status,
        "branch": branch,
    }
    return branch


def _run_run_flow(
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str | None:
    callback = _consume_callback_for_run_flow(runtime_variables)
    if not isinstance(callback, dict):
        return None
    result = str(callback.get("result", "")).strip().lower()
    if not result:
        return None
    runtime_variables["run_flow_last_callback"] = {
        "component_ref_id": component.get("ref_id"),
        "result": result,
        "event_name": callback.get("event_name"),
        "entity": callback.get("entity"),
        "received_at": callback.get("received_at"),
        "data": callback.get("data") if isinstance(callback.get("data"), dict) else {},
    }
    return result


def _switch_bot_flow_state(runtime_variables: dict[str, Any]) -> dict[str, Any] | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    state = workflow_meta.get("switch_bot_flow")
    return state if isinstance(state, dict) else None


def _identidade_person_flow_link_state(runtime_variables: dict[str, Any]) -> dict[str, Any] | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    state = workflow_meta.get("identidade_person_flow_link")
    return state if isinstance(state, dict) else None


def _run_switch_bot_flow(
    *,
    definition: dict[str, Any],
    current_card_uuid: str,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    component_ref_id = str(component.get("ref_id") or component.get("uuid") or current_card_uuid).strip()
    current_state = _switch_bot_flow_state(runtime_variables)
    if isinstance(current_state, dict) and str(current_state.get("component_ref_id") or "") == component_ref_id:
        status = str(current_state.get("status") or "").strip().lower()
        if status == "completed":
            return "success"
        if status == "failed":
            exception_branch = _resolve_component_exception_branch_label(
                definition=definition,
                current_card_uuid=current_card_uuid,
            )
            if exception_branch is not None:
                return exception_branch
            raise WorkflowExecutionError(
                "switch_bot_flow_failed_without_exception_branch",
                "switch_bot_flow falhou e não possui branch de exception.",
            )
        if status in {"waiting_message", "pending_delivery", "opening", "active"}:
            return None

    settings = get_settings()
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        if not settings.switch_bot_flow_enabled:
            raise SwitchBotFlowError(
                "switch_bot_flow_disabled",
                "switch_bot_flow está desabilitado por configuração.",
            )
        target_flow_uuid = resolve_target_flow_uuid(component)
        last_payload = runtime_variables.get("last_payload")
        if not isinstance(last_payload, dict):
            raise SwitchBotFlowError(
                "switch_bot_flow_last_payload_missing",
                "switch_bot_flow não encontrou o último payload recebido pelo ORCH.",
            )

        is_meta_payload = last_payload.get("object") == "whatsapp_business_account"
        has_user_message = is_meta_user_message_payload(last_payload)
        if not is_meta_payload:
            raise SwitchBotFlowError(
                "switch_bot_flow_meta_payload_required",
                "switch_bot_flow requer um payload original da Meta.",
            )

        state: dict[str, Any] = {
            "component_ref_id": component_ref_id,
            "target_flow_uuid": target_flow_uuid,
            "target_session_id": None,
            "status": "pending_delivery" if has_user_message else "waiting_message",
            "activated_at": now_iso,
            "completed_at": None,
            "last_forwarded_event_id": None,
            "last_error": None,
        }
        if has_user_message:
            state["pending_payload"] = copy.deepcopy(last_payload)
            state["pending_message_ids"] = extract_meta_message_ids(last_payload)
        workflow_meta["switch_bot_flow"] = state
        return None
    except SwitchBotFlowError as exc:
        workflow_meta["switch_bot_flow"] = {
            "component_ref_id": component_ref_id,
            "target_flow_uuid": None,
            "target_session_id": None,
            "status": "failed",
            "activated_at": now_iso,
            "completed_at": now_iso,
            "last_forwarded_event_id": None,
            "last_error": {
                "code": exc.code,
                "message": exc.message,
                "status_code": exc.status_code,
                "updated_at": now_iso,
            },
        }
        exception_branch = _resolve_component_exception_branch_label(
            definition=definition,
            current_card_uuid=current_card_uuid,
        )
        if exception_branch is not None:
            return exception_branch
        raise WorkflowExecutionError(exc.code, exc.message) from exc


def _extract_send_with_whatsapp_number_policies(component: dict[str, Any]) -> tuple[list[str], dict[str, int]]:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    config = params.get("whatsapp_numbers_config") if isinstance(params.get("whatsapp_numbers_config"), dict) else {}
    rows = config.get("numbers") if isinstance(config.get("numbers"), list) else []

    numbers: list[str] = []
    percentual_by_phone: dict[str, int] = {}
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        value = str(normalize_phone_to_canonical_ani(item.get("number")) or "").strip()
        if not value or value in seen:
            continue
        percentual_raw = item.get("percentual_consumo")
        percentual_value = 0
        try:
            percentual_value = int(float(str(percentual_raw).strip()))
        except Exception:
            percentual_value = 0
        if percentual_value < 0:
            percentual_value = 0
        if percentual_value > 100:
            percentual_value = 100
        seen.add(value)
        numbers.append(value)
        percentual_by_phone[value] = percentual_value
    return numbers, percentual_by_phone


def _extract_send_with_whatsapp_numbers(component: dict[str, Any]) -> list[str]:
    numbers, _ = _extract_send_with_whatsapp_number_policies(component)
    return numbers


def _extract_send_whatsapp_interactive_number_policies(component: dict[str, Any]) -> tuple[list[str], dict[str, int]]:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    config = params.get("whatsapp_interactive_config") if isinstance(params.get("whatsapp_interactive_config"), dict) else {}
    rows = config.get("numbers") if isinstance(config.get("numbers"), list) else []

    numbers: list[str] = []
    percentual_by_phone: dict[str, int] = {}
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        value = str(normalize_phone_to_canonical_ani(item.get("number")) or "").strip()
        if not value or value in seen:
            continue
        max_daily_raw = (
            item.get("max_daily_rate_limit_consumption")
            if item.get("max_daily_rate_limit_consumption") is not None
            else (
                item.get("value").get("max_daily_rate_limit_consumption")
                if isinstance(item.get("value"), dict)
                else None
            )
        )
        percentual_value = 0
        try:
            percentual_value = int(float(str(max_daily_raw).strip()))
        except Exception:
            percentual_value = 0
        if percentual_value < 0:
            percentual_value = 0
        if percentual_value > 100:
            percentual_value = 100
        seen.add(value)
        numbers.append(value)
        percentual_by_phone[value] = percentual_value
    return numbers, percentual_by_phone


def _read_send_whatsapp_interactive_selected_number(component: dict[str, Any]) -> str | None:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    config = params.get("whatsapp_interactive_config") if isinstance(params.get("whatsapp_interactive_config"), dict) else {}
    for raw in (
        config.get("selected_number"),
        config.get("active_number"),
    ):
        value = str(normalize_phone_to_canonical_ani(raw) or "").strip()
        if value:
            return value
    return None


def _normalize_whatsapp_recipient_number(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or "").strip())
    digits = digits.lstrip("0")
    if not digits:
        return None
    if not digits.startswith("55"):
        digits = f"55{digits}"
    return digits


def _extract_whatsapp_template_body_text(template_payload: Any) -> str | None:
    if not isinstance(template_payload, dict):
        return None
    components = template_payload.get("components")
    if not isinstance(components, list):
        return None
    for item in components:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip().upper() != "BODY":
            continue
        body_text = item.get("text")
        if isinstance(body_text, str) and body_text.strip():
            return body_text
    return None


def _extract_whatsapp_meta_payload_text(meta_payload: Any) -> str | None:
    if not isinstance(meta_payload, dict):
        return None
    template_payload = meta_payload.get("template")
    template_text = _extract_whatsapp_template_body_text(template_payload)
    if template_text:
        return template_text
    interactive_payload = meta_payload.get("interactive")
    if isinstance(interactive_payload, dict):
        body_payload = interactive_payload.get("body")
        if isinstance(body_payload, dict):
            body_text = body_payload.get("text")
            if isinstance(body_text, str) and body_text.strip():
                return body_text
    body_payload = meta_payload.get("body")
    if isinstance(body_payload, dict):
        body_text = body_payload.get("text")
        if isinstance(body_text, str) and body_text.strip():
            return body_text
    text_payload = meta_payload.get("text")
    if isinstance(text_payload, str) and text_payload.strip():
        return text_payload
    return None


def _extract_whatsapp_template_variable_values(template_payload: Any) -> dict[str, Any]:
    if not isinstance(template_payload, dict):
        return {}
    components = template_payload.get("components")
    if not isinstance(components, list):
        return {}
    for item in components:
        if not isinstance(item, dict) or str(item.get("type") or "").strip().lower() != "body":
            continue
        parameters = item.get("parameters")
        if not isinstance(parameters, list):
            continue
        values: dict[str, Any] = {}
        for parameter in parameters:
            if not isinstance(parameter, dict):
                continue
            if str(parameter.get("type") or "").strip().lower() != "text":
                continue
            values[str(len(values) + 1)] = parameter.get("text")
        if values:
            return values
    return {}


def _read_hsm_language_code(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("code")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _select_send_whatsapp_template_candidate(
    component: dict[str, Any],
    *,
    selected_ani: Any,
) -> dict[str, Any]:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    normalized_ani = str(normalize_phone_to_canonical_ani(selected_ani) or "").strip()
    if not normalized_ani:
        raise WorkflowExecutionError(
            "whatsapp_hsm_number_not_configured",
            "O roteamento WhatsApp não selecionou um número de origem válido.",
        )

    configuration_sources: list[tuple[dict[str, Any], list[Any]]] = []
    interactive_config = params.get("whatsapp_interactive_config")
    if isinstance(interactive_config, dict):
        interactive_entries: list[Any] = []
        for key in ("numbers", "selected_numbers"):
            raw_entries = interactive_config.get(key)
            if isinstance(raw_entries, list):
                interactive_entries.extend(raw_entries)
        for key in ("selected_number", "active_number"):
            raw_entry = interactive_config.get(key)
            if raw_entry is not None:
                interactive_entries.append(raw_entry)
        configuration_sources.append((interactive_config, interactive_entries))

    whatsapp_numbers_config = params.get("whatsapp_numbers_config")
    if isinstance(whatsapp_numbers_config, dict):
        raw_entries = whatsapp_numbers_config.get("numbers")
        configuration_sources.append(
            (whatsapp_numbers_config, raw_entries if isinstance(raw_entries, list) else [])
        )

    addresses_config = params.get("addresses")
    if isinstance(addresses_config, dict):
        numbers_config = addresses_config.get("numbers")
        in_use = numbers_config.get("in_use") if isinstance(numbers_config, dict) else None
        configuration_sources.append(
            (addresses_config, in_use if isinstance(in_use, list) else [])
        )

    if not configuration_sources:
        raise WorkflowExecutionError(
            "whatsapp_hsm_template_missing",
            "O card WhatsApp não possui configuração de números HSM válida.",
        )

    config: dict[str, Any] = {}
    selected_entry: dict[str, Any] | None = None
    for current_config, entries in configuration_sources:
        for raw_entry in entries:
            entry = raw_entry if isinstance(raw_entry, dict) else {"number": raw_entry}
            entry_number = entry.get("number") or entry.get("display_phone_number")
            normalized_entry = str(normalize_phone_to_canonical_ani(entry_number) or "").strip()
            if normalized_entry == normalized_ani:
                config = current_config
                selected_entry = entry
                break
        if selected_entry is not None:
            break

    if selected_entry is None:
        raise WorkflowExecutionError(
            "whatsapp_hsm_number_not_configured",
            f"O número de origem selecionado ({normalized_ani}) não existe na configuração do card.",
        )

    value_payload = selected_entry.get("value") if isinstance(selected_entry.get("value"), dict) else {}
    meta_payload: dict[str, Any] = {}
    for owner in (value_payload, selected_entry, config):
        candidate = owner.get("meta_payload") if isinstance(owner, dict) else None
        if isinstance(candidate, dict) and candidate:
            meta_payload = candidate
            break
    if not meta_payload:
        raise WorkflowExecutionError(
            "whatsapp_hsm_meta_payload_invalid",
            "O número selecionado não possui meta_payload válido.",
        )

    meta_template = meta_payload.get("template") if isinstance(meta_payload.get("template"), dict) else {}
    template_selected: dict[str, Any] = {}
    for owner in (selected_entry, value_payload, config):
        direct = owner.get("template_selected") if isinstance(owner, dict) else None
        if isinstance(direct, dict) and direct:
            template_selected = direct
            break
        template_container = owner.get("template") if isinstance(owner, dict) else None
        if isinstance(template_container, dict):
            nested_selected = template_container.get("selected")
            if isinstance(nested_selected, dict) and nested_selected:
                template_selected = nested_selected
                break
            if template_container.get("name"):
                template_selected = template_container
                break
    if not template_selected:
        template_selected = meta_template

    template_name = template_selected.get("name") or meta_template.get("name")
    if not isinstance(template_name, str) or not template_name.strip():
        raise WorkflowExecutionError(
            "whatsapp_hsm_template_missing",
            "A configuração do número selecionado não informa o nome do template.",
        )

    variable_values: dict[str, Any] = {}
    for owner in (selected_entry, value_payload, config):
        candidate = owner.get("variable_values") if isinstance(owner, dict) else None
        if isinstance(candidate, dict) and candidate:
            variable_values = candidate
            break
    if not variable_values:
        variable_values = _extract_whatsapp_template_variable_values(meta_template)

    return {
        "number": _normalize_whatsapp_recipient_number(normalized_ani),
        "template_name": template_name.strip(),
        "language": (
            _read_hsm_language_code(template_selected.get("language"))
            or _read_hsm_language_code(meta_template.get("language"))
        ),
        "text": (
            _extract_whatsapp_template_body_text(template_selected)
            or _extract_whatsapp_template_body_text(meta_template)
            or _extract_whatsapp_meta_payload_text(meta_payload)
            or template_name.strip()
        ),
        "variable_values": variable_values,
        "meta_payload": meta_payload,
    }


def _render_hsm_value_strict(value: Any, variables: dict[str, Any]) -> Any:
    unresolved: set[str] = set()

    def _collect(candidate: Any) -> None:
        if isinstance(candidate, str):
            for match in _TEMPLATE_PATTERN.finditer(candidate):
                token = match.group(1).strip()
                if _render_value(match.group(0), variables) is None:
                    unresolved.add(token)
            return
        if isinstance(candidate, dict):
            for nested in candidate.values():
                _collect(nested)
            return
        if isinstance(candidate, list):
            for nested in candidate:
                _collect(nested)

    _collect(value)
    if unresolved:
        raise WorkflowExecutionError(
            "whatsapp_hsm_variable_unresolved",
            f"Variáveis não resolvidas no HSM: {', '.join(sorted(unresolved))}.",
        )
    return _render_value(value, variables)


def _build_send_whatsapp_template_hsm(
    *,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
    contact_row: dict[str, Any] | None,
    selected_ani: Any,
) -> dict[str, Any]:
    if not isinstance(contact_row, dict):
        raise WorkflowExecutionError(
            "whatsapp_hsm_contact_missing",
            "Não foi possível carregar o contato em foco para montar o HSM.",
        )
    recipient = _normalize_whatsapp_recipient_number(
        contact_row.get("contact_channel_address")
        or contact_row.get("contact_identifier")
    )
    if recipient is None:
        raise WorkflowExecutionError(
            "whatsapp_hsm_contact_missing",
            "O contato em foco não possui endereço WhatsApp válido.",
        )

    candidate = _select_send_whatsapp_template_candidate(component, selected_ani=selected_ani)
    variables = copy.deepcopy(_ensure_variables(runtime_variables))
    customs = variables.get("customs") if isinstance(variables.get("customs"), dict) else {}
    system = variables.get("system") if isinstance(variables.get("system"), dict) else {}
    contact = variables.get("contact") if isinstance(variables.get("contact"), dict) else {}
    system_contact = system.get("contact") if isinstance(system.get("contact"), dict) else {}
    variables["recipient_phone_number"] = recipient
    customs["recipient_phone_number"] = recipient
    system["recipient_phone_number"] = recipient
    contact["recipient_phone_number"] = recipient
    system_contact["recipient_phone_number"] = recipient
    variables["customs"] = customs
    variables["system"] = system
    variables["contact"] = contact
    system["contact"] = system_contact

    rendered_values: dict[str, Any] = {}
    for key, raw_value in candidate["variable_values"].items():
        rendered_values[str(key)] = _render_hsm_value_strict(raw_value, variables)
        variables[str(key)] = rendered_values[str(key)]

    text_value = str(candidate["text"])
    text_log = re.sub(
        r"\{\{\s*(\d+)\s*\}\}",
        lambda match: str(rendered_values.get(match.group(1), "")),
        text_value,
    )
    text_log = str(_render_hsm_value_strict(text_log, variables) or "")
    rendered_payload = _render_hsm_value_strict(candidate["meta_payload"], variables)
    if not isinstance(rendered_payload, dict) or not rendered_payload:
        raise WorkflowExecutionError(
            "whatsapp_hsm_meta_payload_invalid",
            "A interpolação do meta_payload não produziu um objeto válido.",
        )
    rendered_payload["to"] = recipient

    return {
        "text": text_value,
        "text_log": text_log or text_value,
        "payload": rendered_payload,
        "template_name": candidate["template_name"],
        "language": candidate["language"],
        "component_ref_id": str(component.get("ref_id") or component.get("uuid") or "") or None,
        "number": candidate["number"],
    }


async def _prepare_send_with_whatsapp_contact_member(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    session_id: int,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
    contact_list_member_id: int | None = None,
) -> dict[str, Any] | None:
    numbers, percentual_by_phone = _extract_send_with_whatsapp_number_policies(component)
    assignment = await assign_whatsapp_routing_for_session(
        db_session,
        flow_uuid=flow_uuid,
        session_id=session_id,
        numbers=numbers,
        percentual_by_phone=percentual_by_phone,
        contact_list_member_id=contact_list_member_id,
    )
    runtime_variables["send_with_whatsapp_routing"] = {
        "numbers": numbers,
        "percentual_by_phone": percentual_by_phone,
        "assignment": assignment,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if contact_list_member_id is not None and assignment is None:
        raise WorkflowExecutionError(
            "contact_member_routing_update_failed",
            "O membro contextual deixou de estar ativo antes do roteamento WhatsApp.",
        )
    return assignment


async def _prepare_send_whatsapp_interactive_contact_member(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    session_id: int,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
    contact_list_member_id: int | None = None,
) -> dict[str, Any] | None:
    numbers, percentual_by_phone = _extract_send_whatsapp_interactive_number_policies(component)
    assignment = await assign_whatsapp_routing_for_session(
        db_session,
        flow_uuid=flow_uuid,
        session_id=session_id,
        numbers=numbers,
        percentual_by_phone=percentual_by_phone,
        contact_list_member_id=contact_list_member_id,
    )
    runtime_variables["send_whatsapp_interactive_routing"] = {
        "numbers": numbers,
        "percentual_by_phone": percentual_by_phone,
        "assignment": assignment,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if contact_list_member_id is not None and assignment is None:
        raise WorkflowExecutionError(
            "contact_member_routing_update_failed",
            "O membro contextual deixou de estar ativo antes do roteamento WhatsApp interativo.",
        )
    return assignment


async def _prepare_send_whatsapp_template_contact_member(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    session_id: int,
    session_uuid: str,
    revision_id: str,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
    contact_row: dict[str, Any] | None,
    contact_list_member_id: int | None = None,
) -> dict[str, Any] | None:
    kind = component_kind(component)
    if kind == "send_with_whatsapp":
        numbers, percentual_by_phone = _extract_send_with_whatsapp_number_policies(component)
        routing_runtime_key = "send_with_whatsapp_routing"
    else:
        numbers, percentual_by_phone = _extract_send_whatsapp_interactive_number_policies(component)
        routing_runtime_key = "send_whatsapp_interactive_routing"
    component_ref_id = str(component.get("ref_id") or component.get("uuid") or "").strip()
    if not component_ref_id:
        raise WorkflowExecutionError(
            "whatsapp_hsm_template_missing",
            f"O card {kind or 'WhatsApp'} não possui ref_id.",
        )
    component_fingerprint = hashlib.sha256(
        json.dumps(component, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    idempotency_key = (
        f"orch:{session_uuid}:{revision_id}:{component_ref_id}:{component_fingerprint}"
    )

    try:
        async with db_session.begin_nested():
            assignment = await assign_whatsapp_routing_for_session(
                db_session,
                flow_uuid=flow_uuid,
                session_id=session_id,
                numbers=numbers,
                percentual_by_phone=percentual_by_phone,
                contact_list_member_id=contact_list_member_id,
                outbound_hsm_idempotency_key=idempotency_key,
            )
            if assignment is None:
                raise WorkflowExecutionError(
                    "contact_member_routing_update_failed",
                    "O membro em foco deixou de estar ativo antes da preparação do HSM.",
                )
            if not _is_send_with_whatsapp_limit_exhausted(assignment):
                if assignment.get("mode") == "reuse_prepared_hsm":
                    hsm = assignment["outbound_hsm"]
                else:
                    hsm = _build_send_whatsapp_template_hsm(
                        component=component,
                        runtime_variables=runtime_variables,
                        contact_row=contact_row,
                        selected_ani=assignment.get("ani"),
                    )
                    persisted = await persist_contact_member_outbound_hsm(
                        db_session,
                        flow_uuid=flow_uuid,
                        session_id=session_id,
                        contact_list_member_id=int(assignment["contact_list_member_id"]),
                        hsm=hsm,
                        idempotency_key=idempotency_key,
                        session_uuid=session_uuid,
                        component_ref_id=component_ref_id,
                    )
                    if not persisted:
                        raise WorkflowExecutionError(
                            "whatsapp_hsm_persist_failed",
                            "O membro em foco deixou de estar elegível antes da persistência do HSM.",
                        )
    except WorkflowExecutionError:
        raise
    except Exception as exc:
        raise WorkflowExecutionError(
            "whatsapp_hsm_persist_failed",
            f"Falha ao persistir o HSM materializado: {type(exc).__name__}.",
        ) from exc

    runtime_variables[routing_runtime_key] = {
        "numbers": numbers,
        "percentual_by_phone": percentual_by_phone,
        "assignment": assignment,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    runtime_variables.pop(f"{kind}_last_error", None)
    if not _is_send_with_whatsapp_limit_exhausted(assignment):
        runtime_variables["whatsapp_hsm_outbound"] = {
            "contact_list_member_id": assignment.get("contact_list_member_id"),
            "component_kind": kind,
            "component_ref_id": component_ref_id,
            "template_name": hsm.get("template_name"),
            "idempotency_key": idempotency_key,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
    return assignment


async def _prepare_send_with_dialer_contact_member(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    session_id: int,
    runtime_variables: dict[str, Any],
    contact_list_member_id: int | None = None,
) -> dict[str, Any] | None:
    assignment = await assign_dialer_routing_for_session(
        db_session,
        flow_uuid=flow_uuid,
        session_id=session_id,
        contact_list_member_id=contact_list_member_id,
    )
    runtime_variables["send_with_dialer_routing"] = {
        "assignment": assignment,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if contact_list_member_id is not None and assignment is None:
        raise WorkflowExecutionError(
            "contact_member_routing_update_failed",
            "O membro contextual deixou de estar ativo antes do roteamento Dialer.",
        )
    return assignment


async def _prepare_send_with_sms_contact_member(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    session_id: int,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
    contact_row: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(contact_row, dict):
        raise WorkflowExecutionError(
            "send_with_sms_contact_not_eligible",
            "A sessão não possui contexto de contato válido para o handoff SMS.",
        )

    try:
        contact_list_member_id = int(contact_row.get("contact_list_member_id"))
        contact_list_id = str(UUID(str(contact_row.get("contact_list_id"))))
        mailing_id = int(contact_row.get("mailing_id"))
    except (TypeError, ValueError) as exc:
        raise WorkflowExecutionError(
            "send_with_sms_contact_not_eligible",
            "A sessão não possui membro, lista e mailing válidos para o handoff SMS.",
        ) from exc

    channel_type = _normalize_channel_type(contact_row.get("contact_channel_type"))
    channel_address = str(contact_row.get("contact_channel_address") or "").strip()
    if (
        contact_list_member_id <= 0
        or mailing_id <= 0
        or channel_type != "sms"
        or not channel_address
    ):
        raise WorkflowExecutionError(
            "send_with_sms_contact_not_eligible",
            "O membro em foco não representa um canal SMS válido.",
        )

    raw_person_uuid = contact_row.get("person_uuid")
    person_uuid: str | None = None
    if raw_person_uuid is not None:
        try:
            person_uuid = str(UUID(str(raw_person_uuid)))
        except (TypeError, ValueError) as exc:
            raise WorkflowExecutionError(
                "send_with_sms_contact_not_eligible",
                "O membro SMS em foco possui uma referência de pessoa inválida.",
            ) from exc

    assignment = await assign_sms_routing_for_session(
        db_session,
        flow_uuid=flow_uuid,
        session_id=session_id,
        contact_list_member_id=contact_list_member_id,
        contact_list_id=contact_list_id,
        mailing_id=mailing_id,
        person_uuid=person_uuid,
    )
    if assignment is None:
        raise WorkflowExecutionError(
            "send_with_sms_contact_not_eligible",
            "O membro SMS deixou de estar elegível antes do handoff.",
        )

    runtime_variables["send_with_sms_routing"] = {
        "component_ref_id": component.get("ref_id"),
        "assignment": assignment,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    runtime_variables.pop("send_with_sms_last_error", None)
    return assignment


def _is_send_with_whatsapp_limit_exhausted(assignment: dict[str, Any] | None) -> bool:
    if not isinstance(assignment, dict):
        return False
    linked_actuator = str(assignment.get("linked_actuator") or "").strip().lower()
    return linked_actuator in SEND_WITH_WHATSAPP_LIMIT_EXHAUSTED_ACTUATORS


def _read_status_order_wait_deadline(
    runtime_variables: dict[str, Any],
    *,
    status: str,
) -> datetime | None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    wait_meta = workflow_meta.get("whatsapp_status_order_wait")
    if not isinstance(wait_meta, dict):
        return None
    raw = wait_meta.get(status)
    return _parse_iso_datetime(raw)


def _write_status_order_wait_deadline(
    runtime_variables: dict[str, Any],
    *,
    status: str,
    deadline: datetime,
) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    wait_meta = workflow_meta.get("whatsapp_status_order_wait")
    if not isinstance(wait_meta, dict):
        wait_meta = {}
        workflow_meta["whatsapp_status_order_wait"] = wait_meta
    wait_meta[status] = deadline.isoformat()


def _clear_status_order_wait_deadline(
    runtime_variables: dict[str, Any],
    *,
    status: str,
) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    wait_meta = workflow_meta.get("whatsapp_status_order_wait")
    if not isinstance(wait_meta, dict):
        return
    wait_meta.pop(status, None)
    if not wait_meta:
        workflow_meta.pop("whatsapp_status_order_wait", None)


def _compute_whatsapp_status_order_delay(
    *,
    runtime_variables: dict[str, Any],
    session_state: dict[str, Any],
) -> datetime | None:
    status = _extract_whatsapp_status_from_runtime(runtime_variables)
    if status is None:
        return None
    prerequisite_field = WHATSAPP_STATUS_ORDER_PREREQUISITES.get(status)
    if prerequisite_field is None:
        return None

    if session_state.get(prerequisite_field) is not None:
        _clear_status_order_wait_deadline(runtime_variables, status=status)
        return None

    now_utc = datetime.now(timezone.utc)
    deadline = _read_status_order_wait_deadline(runtime_variables, status=status)
    if deadline is None:
        ttl = WHATSAPP_STATUS_ORDER_TTL_SECONDS.get(status, 20)
        deadline = now_utc + timedelta(seconds=max(1, ttl))
        _write_status_order_wait_deadline(runtime_variables, status=status, deadline=deadline)

    if now_utc >= deadline:
        _clear_status_order_wait_deadline(runtime_variables, status=status)
        return None

    return now_utc + timedelta(seconds=WHATSAPP_STATUS_ORDER_RETRY_SECONDS)


def _should_resume_whatsapp_blocking_execution(
    runtime_variables: dict[str, Any],
    *,
    has_pending_whatsapp_events: bool = False,
) -> bool:
    blocking_stop_reason = _read_blocking_stop_reason(runtime_variables)
    if blocking_stop_reason not in WHATSAPP_BLOCKING_STOP_REASONS:
        return False
    if has_pending_whatsapp_events:
        return True
    if _extract_whatsapp_status_from_runtime(runtime_variables) is not None:
        return True
    return _extract_whatsapp_message_branch_key_from_runtime(runtime_variables) is not None


def _should_resume_dialer_blocking_execution(runtime_variables: dict[str, Any]) -> bool:
    blocking_stop_reason = _read_blocking_stop_reason(runtime_variables)
    if blocking_stop_reason not in DIALER_BLOCKING_STOP_REASONS:
        return False
    return _extract_dialer_status_from_runtime(runtime_variables) is not None


def _should_resume_run_flow_blocking_execution(runtime_variables: dict[str, Any]) -> bool:
    blocking_stop_reason = _read_blocking_stop_reason(runtime_variables)
    if blocking_stop_reason not in RUN_FLOW_BLOCKING_STOP_REASONS:
        return False
    return _is_new_callback_for_waiting_run_flow(runtime_variables)


def _should_resume_switch_bot_flow_blocking_execution(runtime_variables: dict[str, Any]) -> bool:
    blocking_stop_reason = _read_blocking_stop_reason(runtime_variables)
    if blocking_stop_reason not in SWITCH_BOT_FLOW_BLOCKING_STOP_REASONS:
        return False
    state = _switch_bot_flow_state(runtime_variables)
    status = str(state.get("status") or "").strip().lower() if isinstance(state, dict) else ""
    return status in {"completed", "failed"}


def _should_resume_identidade_person_blocking_execution(runtime_variables: dict[str, Any]) -> bool:
    blocking_stop_reason = _read_blocking_stop_reason(runtime_variables)
    if blocking_stop_reason not in IDENTIDADE_PERSON_BLOCKING_STOP_REASONS:
        return False
    state = _identidade_person_flow_link_state(runtime_variables)
    status = str(state.get("status") or "").strip().lower() if isinstance(state, dict) else ""
    return status in {"completed", "failed"}


def _parse_variable_path_tokens(path: str) -> list[str | int] | None:
    raw_path = str(path or "").strip()
    if not raw_path:
        return None

    tokens: list[str | int] = []
    length = len(raw_path)
    cursor = 0

    while cursor < length:
        if raw_path[cursor] == ".":
            return None

        if raw_path[cursor] != "[":
            start = cursor
            while cursor < length and raw_path[cursor] not in ".[":
                cursor += 1
            identifier = raw_path[start:cursor].strip()
            if not identifier:
                return None
            tokens.append(identifier)

        while cursor < length and raw_path[cursor] == "[":
            cursor += 1
            while cursor < length and raw_path[cursor].isspace():
                cursor += 1
            if cursor >= length:
                return None

            token_value: str | int
            if raw_path[cursor] in {"'", '"'}:
                quote = raw_path[cursor]
                cursor += 1
                start = cursor
                while cursor < length and raw_path[cursor] != quote:
                    cursor += 1
                if cursor >= length:
                    return None
                token_value = raw_path[start:cursor]
                cursor += 1
                while cursor < length and raw_path[cursor].isspace():
                    cursor += 1
                if cursor >= length or raw_path[cursor] != "]":
                    return None
                cursor += 1
            else:
                start = cursor
                while cursor < length and raw_path[cursor].isdigit():
                    cursor += 1
                index_text = raw_path[start:cursor]
                while cursor < length and raw_path[cursor].isspace():
                    cursor += 1
                if not index_text or cursor >= length or raw_path[cursor] != "]":
                    return None
                cursor += 1
                token_value = int(index_text)

            tokens.append(token_value)

        if cursor >= length:
            break
        if raw_path[cursor] != ".":
            return None
        cursor += 1
        if cursor >= length:
            return None

    return tokens or None


def _get_by_dot_path(payload: Any, path: str) -> Any:
    tokens = _parse_variable_path_tokens(path)
    if not tokens:
        return None

    current: Any = payload
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(current, list):
                return None
            if token < 0 or token >= len(current):
                return None
            current = current[token]
            continue

        if not isinstance(current, dict) or token not in current:
            return None
        current = current[token]
    return current


def _render_value(template: Any, variables: dict[str, Any]) -> Any:
    if isinstance(template, str):
        matches = list(_TEMPLATE_PATTERN.finditer(template))
        if not matches:
            return template

        utils = variables.get("utils") if isinstance(variables.get("utils"), dict) else {}

        if len(matches) == 1 and matches[0].span() == (0, len(template)):
            token = matches[0].group(1).strip()
            resolved = _get_by_dot_path(variables, token)
            if resolved is None:
                if token and "." not in token and token in utils:
                    resolved = utils.get(token)
            if resolved is None:
                customs = variables.get("customs") if isinstance(variables.get("customs"), dict) else {}
                payload = variables.get("payload") if isinstance(variables.get("payload"), dict) else {}
                resolved = _get_by_dot_path(customs, token)
                if resolved is None:
                    resolved = _get_by_dot_path(payload, token)
            return resolved

        rendered = template
        for match in matches:
            token = match.group(1).strip()
            value = _get_by_dot_path(variables, token)
            if value is None:
                if token and "." not in token and token in utils:
                    value = utils.get(token)
            if value is None:
                customs = variables.get("customs") if isinstance(variables.get("customs"), dict) else {}
                payload = variables.get("payload") if isinstance(variables.get("payload"), dict) else {}
                value = _get_by_dot_path(customs, token)
                if value is None:
                    value = _get_by_dot_path(payload, token)
            rendered = rendered.replace(match.group(0), "" if value is None else str(value))
        return rendered

    if isinstance(template, dict):
        return {key: _render_value(value, variables) for key, value in template.items()}
    if isinstance(template, list):
        return [_render_value(item, variables) for item in template]
    return template


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


RUNTIME_UTILS_TZ = ZoneInfo("America/Sao_Paulo")
WEEKDAY_NAMES_PT_BR = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)


def _is_business_day(date_value) -> bool:  # noqa: ANN001
    return date_value.weekday() < 5


def _next_business_day(date_value):  # noqa: ANN001
    cursor = date_value + timedelta(days=1)
    while not _is_business_day(cursor):
        cursor = cursor + timedelta(days=1)
    return cursor


def _previous_business_day(date_value):  # noqa: ANN001
    cursor = date_value - timedelta(days=1)
    while not _is_business_day(cursor):
        cursor = cursor - timedelta(days=1)
    return cursor


def _fifth_business_day_of_month(date_value):  # noqa: ANN001
    cursor = date_value.replace(day=1)
    business_days_found = 0
    while cursor.month == date_value.month:
        if _is_business_day(cursor):
            business_days_found += 1
            if business_days_found == 5:
                return cursor
        cursor = cursor + timedelta(days=1)
    return date_value


def _build_runtime_utils_payload(*, now_utc: datetime | None = None) -> dict[str, Any]:
    reference_now = now_utc or datetime.now(timezone.utc)
    if reference_now.tzinfo is None:
        reference_now = reference_now.replace(tzinfo=timezone.utc)
    now_local = reference_now.astimezone(RUNTIME_UTILS_TZ)

    current_hour = now_local.hour
    if current_hour < 12:
        periodo_do_dia = "manha"
        saudacao = "Bom dia"
    elif current_hour < 18:
        periodo_do_dia = "tarde"
        saudacao = "Boa tarde"
    else:
        periodo_do_dia = "noite"
        saudacao = "Boa noite"

    today = now_local.date()
    return {
        "saudacao": saudacao,
        "periodo_do_dia": periodo_do_dia,
        "dia_da_semana": WEEKDAY_NAMES_PT_BR[today.weekday()],
        "hora_atual": now_local.strftime("%H:%M:%S"),
        "dia_atual": today.isoformat(),
        "proximo_dia_util": _next_business_day(today).isoformat(),
        "quinto_dia_util": _fifth_business_day_of_month(today).isoformat(),
        "dia_util_anterior": _previous_business_day(today).isoformat(),
        "e_dia_util_hoje": _is_business_day(today),
    }


def _ensure_runtime_utils_scope(variables: dict[str, Any]) -> None:
    utils_scope = variables.get("utils")
    if not isinstance(utils_scope, dict):
        utils_scope = {}
        variables["utils"] = utils_scope
    utils_scope.update(_build_runtime_utils_payload())


def _ensure_variables(runtime_variables: dict[str, Any]) -> dict[str, Any]:
    variables = runtime_variables.get("variables")
    if isinstance(variables, dict):
        payload = runtime_variables.get("input_payload")
        if isinstance(payload, dict) and not isinstance(variables.get("payload"), dict):
            variables["payload"] = dict(payload)
        if not isinstance(variables.get("payload"), dict):
            variables["payload"] = {}
        customs = variables.get("customs")
        if not isinstance(customs, dict):
            seed = variables.get("payload")
            variables["customs"] = dict(seed) if isinstance(seed, dict) else {}
        callback = variables.get("callback")
        if not isinstance(callback, dict):
            variables["callback"] = {}
        disposition = variables.get("disposition")
        if not isinstance(disposition, dict):
            variables["disposition"] = {}
        file_scope = variables.get("file")
        if not isinstance(file_scope, dict):
            file_scope = {}
            variables["file"] = file_scope
        file_content = file_scope.get("content")
        if not isinstance(file_content, dict):
            file_scope["content"] = {}
        _ensure_runtime_utils_scope(variables)
        return variables

    input_payload = runtime_variables.get("input_payload")
    if isinstance(input_payload, dict):
        runtime_variables["variables"] = {
            "payload": dict(input_payload),
            "customs": dict(input_payload),
            "callback": {},
            "disposition": {},
            "file": {"content": {}},
            **dict(input_payload),
        }
    else:
        runtime_variables["variables"] = {
            "payload": {},
            "customs": {},
            "callback": {},
            "disposition": {},
            "file": {"content": {}},
        }
    _ensure_runtime_utils_scope(runtime_variables["variables"])
    return runtime_variables["variables"]


def _set_by_path(root: dict[str, Any], path: str, value: Any) -> None:
    parts = [part for part in path.split(".") if part]
    if not parts:
        return

    current = root
    for part in parts[:-1]:
        existing = current.get(part)
        if not isinstance(existing, dict):
            existing = {}
            current[part] = existing
        current = existing
    current[parts[-1]] = value


def _run_set_variables(component: dict[str, Any], runtime_variables: dict[str, Any]) -> None:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    instructions = params.get("instructions") if isinstance(params.get("instructions"), list) else []
    variables = _ensure_variables(runtime_variables)

    for item in instructions:
        if not isinstance(item, dict):
            continue
        variable_path = str(item.get("variable") or item.get("key") or "").strip()
        if not variable_path:
            continue

        source_type = str(item.get("source_type") or "").strip().lower()
        if source_type == "variable" and isinstance(item.get("value"), str):
            raw_path = str(item.get("value")).strip()
            value = _get_by_dot_path(variables, raw_path)
            if value is None:
                value = _get_by_dot_path(runtime_variables.get("input_payload") if isinstance(runtime_variables.get("input_payload"), dict) else {}, raw_path)
        else:
            value = _render_value(item.get("value"), variables)

        if variable_path.startswith("variables."):
            _set_by_path(variables, variable_path[len("variables.") :], value)
        elif variable_path.startswith("customs."):
            customs = variables.get("customs")
            if not isinstance(customs, dict):
                customs = {}
                variables["customs"] = customs
            _set_by_path(customs, variable_path[len("customs.") :], value)
        else:
            customs = variables.get("customs")
            if not isinstance(customs, dict):
                customs = {}
                variables["customs"] = customs
            _set_by_path(customs, variable_path, value)


def _compare_values(left: Any, op: str, right: Any) -> bool:
    if op in {"eq", "==", "equals", "is", "equal"}:
        if left == right:
            return True
        try:
            return float(left) == float(right)
        except Exception:
            return False
    if op in {"ne", "!=", "not_equals", "is_not"}:
        if left != right:
            try:
                return float(left) != float(right)
            except Exception:
                return True
        return False

    try:
        left_num = float(left)
        right_num = float(right)
    except Exception:
        left_num = None
        right_num = None

    if op in {"gt", ">", "greater", "greater_than"}:
        return left_num is not None and right_num is not None and left_num > right_num
    if op in {"gte", ">=", "greater_or_equal"}:
        return left_num is not None and right_num is not None and left_num >= right_num
    if op in {"lt", "<", "less", "less_than"}:
        return left_num is not None and right_num is not None and left_num < right_num
    if op in {"lte", "<=", "less_or_equal"}:
        return left_num is not None and right_num is not None and left_num <= right_num

    if op in {"contains"}:
        return str(right) in str(left)

    return left == right


def _run_condition(component: dict[str, Any], runtime_variables: dict[str, Any]) -> str:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    conditions = params.get("conditions") if isinstance(params.get("conditions"), list) else []
    variables = _ensure_variables(runtime_variables)

    for condition in conditions:
        if not isinstance(condition, dict):
            continue

        rules = condition.get("rules") if isinstance(condition.get("rules"), list) else []
        match_mode = str(condition.get("match") or "all").strip().lower()

        results: list[bool] = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            field = str(rule.get("field") or "").strip()
            op = str(rule.get("operator") or rule.get("op") or "eq").strip().lower()
            expected = _render_value(rule.get("value"), variables)
            actual = None
            if field:
                if "{{" in field and "}}" in field:
                    actual = _render_value(field, variables)
                else:
                    actual = _get_by_dot_path(variables, field)
                    if actual is None:
                        customs = variables.get("customs") if isinstance(variables.get("customs"), dict) else {}
                        payload = variables.get("payload") if isinstance(variables.get("payload"), dict) else {}
                        actual = _get_by_dot_path(customs, field)
                        if actual is None:
                            actual = _get_by_dot_path(payload, field)
            results.append(_compare_values(actual, op, expected))

        if not results:
            continue

        matched = all(results) if match_mode == "all" else any(results)
        if matched:
            branch = (
                condition.get("id")
                or condition.get("branch")
                or condition.get("name")
                or condition.get("label")
                or "true"
            )
            return str(branch).strip().lower() or "true"

    return "false"


def _read_int(values: list[Any]) -> int | None:
    for raw in values:
        if raw is None:
            continue
        try:
            return int(str(raw).strip())
        except Exception:
            continue
    return None


def _compute_frozen_until(component: dict[str, Any], runtime_variables: dict[str, Any]) -> datetime:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    variables = _ensure_variables(runtime_variables)

    direct_until = _render_value(
        params.get("resume_at")
        or params.get("until")
        or params.get("frozen_until")
        or params.get("datetime"),
        variables,
    )
    parsed_direct = _parse_iso_datetime(direct_until)
    if parsed_direct is not None:
        return parsed_direct

    wait_ms = _read_int(
        [
            _render_value(params.get("tempo_ms"), variables),
            _render_value(params.get("wait_ms"), variables),
            _render_value(params.get("milliseconds"), variables),
            _render_value(params.get("ms"), variables),
            _render_value(params.get("tempo"), variables),
        ]
    )
    wait_seconds = _read_int(
        [
            _render_value(params.get("delay_in_seconds"), variables),
            _render_value(params.get("seconds"), variables),
            _render_value(params.get("tempo_seconds"), variables),
        ]
    )
    wait_minutes = _read_int(
        [
            _render_value(params.get("minutes"), variables),
            _render_value(params.get("tempo_minutes"), variables),
        ]
    )

    total_ms = 0
    if wait_ms is not None:
        total_ms += max(wait_ms, 0)
    if wait_seconds is not None:
        total_ms += max(wait_seconds, 0) * 1000
    if wait_minutes is not None:
        total_ms += max(wait_minutes, 0) * 60 * 1000

    return datetime.now(timezone.utc) + timedelta(milliseconds=max(total_ms, 0))


def _coerce_timeout_ms(raw_value: Any, default: int = 400) -> int:
    try:
        parsed = int(str(raw_value).strip())
    except Exception:
        parsed = default
    return max(100, min(10_000, parsed))


def _unwrap_option(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("id", "value", "name", "label"):
            if key in value and value.get(key) is not None:
                return value.get(key)
    if isinstance(value, list) and value:
        return _unwrap_option(value[0])
    return value


def _normalize_delimiter(value: Any) -> str:
    raw = str(_unwrap_option(value) or "").strip().lower()
    if raw in {"", "pipe", "|"}:
        return "|"
    if raw in {"virgula", "vírgula", ",", "comma"}:
        return ","
    if raw in {"ponto e virgula", "ponto-e-virgula", ";", "semicolon"}:
        return ";"
    if raw in {"tab", "\\t", "t"}:
        return "\t"
    return str(_unwrap_option(value) or "|")


def _normalize_line_break(value: Any) -> str:
    raw = str(_unwrap_option(value) or "").strip().upper()
    if raw in {"CRLF", "WINDOWS"}:
        return "\r\n"
    return "\n"


def _ensure_file_extension(file_name: str, format_type: str) -> str:
    if "." in Path(file_name).name:
        return file_name
    extension_map = {
        "csv": ".csv",
        "json": ".json",
        "jsonl": ".jsonl",
        "txt": ".txt",
    }
    suffix = extension_map.get(format_type, "")
    return f"{file_name}{suffix}" if suffix else file_name


def _append_session_suffix(file_name: str, session_id: int) -> str:
    token = str(session_id).strip()
    if "." not in file_name:
        return f"{file_name}-{token}"
    stem, suffix = file_name.rsplit(".", 1)
    return f"{stem}-{token}.{suffix}"


def _safe_relpath(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    raw = raw.lstrip("/")
    normalized = posixpath.normpath(raw)
    if normalized in {"", "."}:
        return ""
    if normalized.startswith("..") or "/../" in f"/{normalized}/":
        raise WorkflowExecutionError("generate_file_invalid_destination_path", "destination_path inválido.")
    return normalized


def _safe_filename(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise WorkflowExecutionError("generate_file_missing_file_name", "Nome do arquivo não informado.")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise WorkflowExecutionError("generate_file_invalid_file_name", "Nome do arquivo inválido.")
    return name


def _resolve_secret_reference(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    token = raw
    if raw.startswith("{{") and raw.endswith("}}"):
        token = raw[2:-2].strip()
    if token.lower().startswith("env."):
        env_name = token[4:].strip()
        return os.getenv(env_name, "").strip() or raw
    if token.lower().startswith("env:"):
        env_name = token[4:].strip()
        return os.getenv(env_name, "").strip() or raw
    return os.getenv(token, "").strip() or raw


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    token = str(_unwrap_option(value)).strip().lower()
    if token in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if token in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _normalize_generate_file_mapping(raw_mapping: Any) -> list[dict[str, str]]:
    rows: list[dict[str, Any]] = []
    if isinstance(raw_mapping, dict):
        for key in ("items", "rows", "value", "mapping"):
            candidate = raw_mapping.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
    elif isinstance(raw_mapping, list):
        rows = raw_mapping

    normalized: list[dict[str, str]] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        column = str(
            entry.get("column")
            or entry.get("header_name")
            or entry.get("name")
            or entry.get("key")
            or ""
        ).strip()
        source = str(
            entry.get("source")
            or entry.get("variable")
            or entry.get("path")
            or entry.get("value")
            or entry.get("source_value")
            or ""
        ).strip()
        data_type = str(
            entry.get("data_type")
            or entry.get("type")
            or entry.get("field_type")
            or "text"
        ).strip().lower()
        if column and source:
            normalized.append({"column": column, "source": source, "data_type": data_type})
    return normalized


def _coerce_generate_file_value(value: Any, data_type: str) -> Any:
    normalized = (data_type or "text").strip().lower()
    if normalized in {"text", "string", ""}:
        return "" if value is None else str(value)
    if normalized in {"number", "float", "double", "decimal"}:
        try:
            return float(str(value).replace(".", "").replace(",", "."))
        except Exception:
            return "" if value is None else str(value)
    if normalized in {"integer", "int"}:
        try:
            return int(float(str(value).replace(".", "").replace(",", ".")))
        except Exception:
            return "" if value is None else str(value)
    if normalized in {"bool", "boolean"}:
        return _coerce_bool(value)
    if normalized in {"json", "object"}:
        if isinstance(value, (dict, list)):
            return value
        text = "" if value is None else str(value).strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            return {"value": text}
    return "" if value is None else str(value)


def _resolve_generate_file_source(source: str, variables: dict[str, Any]) -> Any:
    candidate = source.strip()
    if not candidate:
        return ""
    if "{{" in candidate and "}}" in candidate:
        return _render_value(candidate, variables)
    resolved = _get_by_dot_path(variables, candidate)
    if resolved is not None:
        return resolved
    customs = variables.get("customs") if isinstance(variables.get("customs"), dict) else {}
    payload = variables.get("payload") if isinstance(variables.get("payload"), dict) else {}
    resolved = _get_by_dot_path(customs, candidate)
    if resolved is not None:
        return resolved
    resolved = _get_by_dot_path(payload, candidate)
    if resolved is not None:
        return resolved
    return candidate


def _build_runtime_resolution_scope(
    *,
    runtime_variables: dict[str, Any],
    variables: dict[str, Any],
) -> dict[str, Any]:
    payload = variables.get("payload") if isinstance(variables.get("payload"), dict) else {}
    customs = variables.get("customs") if isinstance(variables.get("customs"), dict) else {}

    scope: dict[str, Any] = dict(variables)
    if isinstance(payload, dict):
        for key, value in payload.items():
            scope.setdefault(key, value)
        scope["payload"] = payload
    if isinstance(customs, dict):
        for key, value in customs.items():
            scope[key] = value
        scope["customs"] = customs

    api_last_result = runtime_variables.get("api_call_last_result")
    if isinstance(api_last_result, dict):
        api_body = api_last_result.get("body")
        if isinstance(api_body, dict):
            scope.setdefault("api_body", api_body)
            if isinstance(customs, dict):
                customs.setdefault("api_body", api_body)

    return scope


def _normalize_contact_extra_data(raw_extra: Any) -> dict[str, Any]:
    if isinstance(raw_extra, dict):
        return dict(raw_extra)
    if isinstance(raw_extra, str):
        token = raw_extra.strip()
        if not token:
            return {}
        try:
            parsed = json.loads(token)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def _normalize_contact_extra_key(raw_key: Any) -> str:
    token = str(raw_key or "").strip().lower()
    if not token:
        return ""
    token = unicodedata.normalize("NFKD", token).encode("ascii", "ignore").decode("ascii")
    token = re.sub(r"[^a-z0-9]+", "_", token).strip("_")
    return token


def _build_contact_extra_with_aliases(extra: dict[str, Any]) -> dict[str, Any]:
    enriched: dict[str, Any] = dict(extra)
    normalized_index: dict[str, Any] = {}

    for key, value in list(extra.items()):
        normalized_key = _normalize_contact_extra_key(key)
        if not normalized_key:
            continue
        normalized_index.setdefault(normalized_key, value)
        enriched.setdefault(normalized_key, value)

    if "carteira" not in enriched:
        for candidate in ("carteira", "carteira_id", "id_carteira", "carteiraid", "idcarteira"):
            if candidate in normalized_index:
                enriched["carteira"] = normalized_index[candidate]
                break

    return enriched


def _inject_contact_runtime_scope(
    *,
    runtime_variables: dict[str, Any],
    contact_row: dict[str, Any] | None,
) -> None:
    if not isinstance(contact_row, dict):
        return

    variables = _ensure_variables(runtime_variables)
    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs

    contact_extra = _build_contact_extra_with_aliases(
        _normalize_contact_extra_data(contact_row.get("contact_channel_extra_data"))
    )
    contact_channel = {
        "type": contact_row.get("contact_channel_type"),
        "label": contact_row.get("contact_channel_label"),
        "address": contact_row.get("contact_channel_address"),
    }
    contact_birth_date = contact_row.get("contact_birth_date")
    if isinstance(contact_birth_date, (date, datetime)):
        contact_birth_date = contact_birth_date.isoformat()

    contact_payload = {
        "contact_list_member_id": contact_row.get("contact_list_member_id"),
        "identifier": contact_row.get("contact_identifier"),
        "name": contact_row.get("contact_name"),
        "full_name": contact_row.get("contact_full_name"),
        "gender": contact_row.get("contact_gender"),
        "country": contact_row.get("contact_country"),
        "province": contact_row.get("contact_province"),
        "city": contact_row.get("contact_city"),
        "birth_date": contact_birth_date,
        "age": contact_row.get("contact_age"),
        "channel_type": contact_row.get("contact_channel_type"),
        "channel_label": contact_row.get("contact_channel_label"),
        "channel_address": contact_row.get("contact_channel_address"),
        "channel": contact_channel,
        "person_uuid": contact_row.get("person_uuid"),
        "extra": contact_extra,
    }

    variables["contact"] = contact_payload
    customs["contact"] = contact_payload


def _inject_callback_runtime_scope(
    *,
    runtime_variables: dict[str, Any],
) -> None:
    variables = _ensure_variables(runtime_variables)
    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs
    callback_payload = _extract_callback_payload_from_runtime(runtime_variables)
    callback_copy = dict(callback_payload) if isinstance(callback_payload, dict) else {}
    disposition_payload: dict[str, Any] = {}
    if isinstance(callback_copy.get("disposition"), dict):
        disposition_payload = dict(callback_copy.get("disposition"))
    elif callback_copy.get("event_name") == "tabulacao" or callback_copy.get("result") == "tabulacao":
        disposition_payload = {
            "category": callback_copy.get("category"),
            "data": callback_copy.get("data") if isinstance(callback_copy.get("data"), dict) else {},
            "received_at": callback_copy.get("received_at"),
        }
    variables["callback"] = callback_copy
    variables["disposition"] = disposition_payload
    customs["callback"] = callback_copy
    customs["disposition"] = disposition_payload


def _extract_runtime_payload_for_system(runtime_variables: dict[str, Any]) -> dict[str, Any]:
    payload = runtime_variables.get("last_payload")
    if isinstance(payload, dict):
        return dict(payload)

    payload = runtime_variables.get("input_payload")
    if isinstance(payload, dict):
        return dict(payload)

    variables = runtime_variables.get("variables")
    if isinstance(variables, dict):
        payload = variables.get("payload")
        if isinstance(payload, dict):
            return dict(payload)
    return {}


def _extract_file_content_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    file_scope = payload.get("file")
    if isinstance(file_scope, dict):
        file_content = file_scope.get("content")
        if isinstance(file_content, dict):
            return dict(file_content)
    return {}


def _extract_whatsapp_referral_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("object") != "whatsapp_business_account":
        return {
            "head_line": None,
            "source_id": None,
            "source_url": None,
            "source_type": None,
        }
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return {
            "head_line": None,
            "source_id": None,
            "source_url": None,
            "source_type": None,
        }
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            messages = value.get("messages")
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                referral = message.get("referral")
                if not isinstance(referral, dict):
                    continue
                head_line = referral.get("headline")
                return {
                    "head_line": None if head_line is None else str(head_line),
                    "source_id": referral.get("source_id"),
                    "source_url": referral.get("source_url"),
                    "source_type": referral.get("source_type"),
                }
    return {
        "head_line": None,
        "source_id": None,
        "source_url": None,
        "source_type": None,
    }


def _build_system_whatsapp_payload(
    *,
    runtime_variables: dict[str, Any],
    session_state: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    status = _extract_whatsapp_status_from_runtime(runtime_variables)
    sent = bool(session_state.get("whatsapp_sent_at")) or status == "sent"
    delivered = bool(session_state.get("whatsapp_delivered_at")) or status == "delivered"
    read = bool(session_state.get("whatsapp_read_at")) or status == "read"
    failed = bool(session_state.get("whatsapp_failed_at")) or status == "failed"
    limit_reached = status == "limit_reached"
    referral_payload = _extract_whatsapp_referral_from_payload(payload)
    return {
        "read": read,
        "sent": sent,
        "failed": failed,
        "referral": referral_payload,
        "delivered": delivered,
        "limit_reached": limit_reached,
    }


def _inject_system_runtime_scope(
    *,
    runtime_variables: dict[str, Any],
    session_state: dict[str, Any],
    contact_row: dict[str, Any] | None,
) -> None:
    variables = _ensure_variables(runtime_variables)
    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs

    payload = _extract_runtime_payload_for_system(runtime_variables)
    callback_payload = variables.get("callback") if isinstance(variables.get("callback"), dict) else {}
    file_content = _extract_file_content_from_payload(payload)

    contact_payload = variables.get("contact") if isinstance(variables.get("contact"), dict) else {}
    contact_attempts = {
        "busy": None,
        "noanswer": None,
        "machine": None,
        "rejected": None,
        "invalidnumber": None,
        "failure": None,
    }
    contact_channel = {
        "type": contact_payload.get("channel_type"),
        "label": contact_payload.get("channel_label"),
        "address": contact_payload.get("channel_address"),
    }
    system_contact = {
        "id": contact_payload.get("contact_list_member_id"),
        "identifier": contact_payload.get("identifier"),
        "status": None,
        "name": contact_payload.get("name"),
        "full_name": contact_payload.get("full_name"),
        "gender": contact_payload.get("gender"),
        "country": contact_payload.get("country"),
        "province": contact_payload.get("province"),
        "city": contact_payload.get("city"),
        "birth_date": contact_payload.get("birth_date"),
        "age": contact_payload.get("age"),
        "message": None,
        "draft_id": None,
        "created_at": None,
        "attempts": contact_attempts,
        "channel": contact_channel,
        "extra": contact_payload.get("extra") if isinstance(contact_payload.get("extra"), dict) else {},
    }
    if isinstance(contact_row, dict):
        system_contact["status"] = contact_row.get("status")

    whatsapp_payload = _build_system_whatsapp_payload(
        runtime_variables=runtime_variables,
        session_state=session_state,
        payload=payload,
    )
    external_id = payload.get("external_id")
    session_external_id = payload.get("session_external_id")
    external_session_id = payload.get("external_session_id")
    if external_session_id is None and session_external_id is not None:
        external_session_id = session_external_id
    if session_external_id is None and external_session_id is not None:
        session_external_id = external_session_id

    system_payload = {
        "contact": system_contact,
        "whatsapp": whatsapp_payload,
        "external_id": external_id,
        "external_session_id": external_session_id,
        "session_external_id": session_external_id,
        "whatsapp.read": whatsapp_payload["read"],
        "whatsapp.sent": whatsapp_payload["sent"],
        "whatsapp.failed": whatsapp_payload["failed"],
        "whatsapp.referral": whatsapp_payload["referral"],
        "whatsapp.delivered": whatsapp_payload["delivered"],
        "whatsapp.limit_reached": whatsapp_payload["limit_reached"],
        "whatsapp.referral.head_line": whatsapp_payload["referral"]["head_line"],
        "whatsapp.referral.source_id": whatsapp_payload["referral"]["source_id"],
        "whatsapp.referral.source_url": whatsapp_payload["referral"]["source_url"],
        "whatsapp.referral.source_type": whatsapp_payload["referral"]["source_type"],
        "payload": payload,
        "callback": callback_payload,
        "file": {"content": file_content},
    }

    variables["system"] = system_payload
    customs["system"] = system_payload
    variables["file"] = {"content": file_content}


def _resolve_component_exception_branch_label(
    *,
    definition: dict[str, Any],
    current_card_uuid: str,
) -> str | None:
    for candidate in outgoing_branch_labels(definition, current_card_uuid=current_card_uuid):
        token = str(candidate or "").strip().lower()
        if token.startswith("exception"):
            return token
    return None


def _resolve_condition_branch_label(
    *,
    definition: dict[str, Any],
    current_card_uuid: str,
    branch_label: str | None,
) -> str:
    normalized_branch = str(branch_label or "").strip().lower()
    outgoing_labels = outgoing_branch_labels(definition, current_card_uuid=current_card_uuid)
    if normalized_branch in outgoing_labels:
        return normalized_branch

    exception_branch = _resolve_component_exception_branch_label(
        definition=definition,
        current_card_uuid=current_card_uuid,
    )
    if exception_branch is not None:
        return exception_branch

    raise WorkflowExecutionError(
        "condition_branch_not_mapped",
        f"Branch '{normalized_branch or '<empty>'}' não encontrado e o condition não possui branch de exception.",
    )


def _is_terminal_failure_session_state(session_state: dict[str, Any]) -> bool:
    runtime_variables = session_state.get("runtime_variables")
    workflow_meta = runtime_variables.get("workflow_v2") if isinstance(runtime_variables, dict) else None
    terminal_failure = workflow_meta.get("terminal_failure") if isinstance(workflow_meta, dict) else None
    try:
        session_state_value = int(session_state.get("state"))
    except (TypeError, ValueError):
        return False
    return session_state_value in {3, 5} and isinstance(terminal_failure, dict)


def _catalog_parameter_scalar(value: Any, *, preferred_keys: tuple[str, ...] = ()) -> Any:
    if isinstance(value, list):
        if not value:
            return None
        return _catalog_parameter_scalar(value[0], preferred_keys=preferred_keys)
    if isinstance(value, dict):
        for key in (*preferred_keys, "id", "value"):
            candidate = value.get(key)
            if candidate is not None and str(candidate).strip():
                return candidate
        return None
    return value


def _catalog_parameter_bool(value: Any) -> bool:
    scalar = _catalog_parameter_scalar(value)
    if isinstance(scalar, bool):
        return scalar
    return str(scalar or "").strip().lower() in {"1", "true", "yes", "sim", "on", "enabled"}


def _split_random_parameters(component: dict[str, Any]) -> dict[str, Any]:
    raw = component.get("parameters")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        parameters: dict[str, Any] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("id") or entry.get("name") or "").strip()
            if key:
                parameters[key] = entry.get("value")
        return parameters
    return {}


def _split_random_percentage(value: Any, *, parameter_id: str) -> int:
    scalar = _catalog_parameter_scalar(value)
    if isinstance(scalar, bool):
        raise WorkflowExecutionError(
            "split_random_invalid_percentage",
            f"O campo {parameter_id} deve ser um inteiro entre 0 e 100.",
        )
    try:
        number = float(scalar)
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number) or not number.is_integer() or not 0 <= number <= 100:
        raise WorkflowExecutionError(
            "split_random_invalid_percentage",
            f"O campo {parameter_id} deve ser um inteiro entre 0 e 100.",
        )
    return int(number)


def _split_random_config(component: dict[str, Any]) -> tuple[int, int, str]:
    params = _split_random_parameters(component)
    variant_a_percentage = _split_random_percentage(
        params.get("variant_a_percentage"),
        parameter_id="variant_a_percentage",
    )
    variant_b_percentage = _split_random_percentage(
        params.get("variant_b_percentage"),
        parameter_id="variant_b_percentage",
    )
    if variant_a_percentage + variant_b_percentage != 100:
        raise WorkflowExecutionError(
            "split_random_invalid_total",
            "A soma dos percentuais das variantes A e B deve ser exatamente 100.",
        )

    output_var = str(_catalog_parameter_scalar(params.get("output_var")) or "").strip() or "split_random"
    if (
        not output_var
        or len(output_var) > 128
        or SPLIT_RANDOM_OUTPUT_VAR_RE.fullmatch(output_var) is None
    ):
        raise WorkflowExecutionError(
            "split_random_invalid_output_var",
            "O campo output_var deve conter um nome de variável válido.",
        )
    return variant_a_percentage, variant_b_percentage, output_var


def _split_random_exception_branch_label(
    *,
    definition: dict[str, Any],
    current_card_uuid: str,
) -> str | None:
    labels = [
        edge.label
        for edge in extract_edges(definition)
        if edge.source == current_card_uuid
        and edge.label is not None
        and edge.label.startswith("exception")
    ]
    if len(labels) == 1:
        return labels[0]
    return None


def _validate_split_random_branches(
    *,
    definition: dict[str, Any],
    current_card_uuid: str,
) -> None:
    allowed_labels = {"variant_a", "variant_b"}
    branch_counts = {"variant_a": 0, "variant_b": 0, "exception": 0}
    invalid_label = False
    for edge in extract_edges(definition):
        if edge.source != current_card_uuid:
            continue
        label = edge.label or ""
        if label in allowed_labels:
            branch_counts[label] += 1
        elif label.startswith("exception"):
            branch_counts["exception"] += 1
        else:
            invalid_label = True

    if (
        branch_counts["variant_a"] != 1
        or branch_counts["variant_b"] != 1
        or branch_counts["exception"] > 1
        or invalid_label
    ):
        raise WorkflowExecutionError(
            "split_random_invalid_branches",
            "O split_random deve possuir exatamente uma saída variant_a, uma variant_b e no máximo uma exception.",
        )


def _split_random_bucket(
    *,
    flow_uuid: str,
    session_identity: str,
    revision_id: str,
    current_card_uuid: str,
) -> int:
    seed = "\x1f".join(
        (
            SPLIT_RANDOM_HASH_STRATEGY,
            str(flow_uuid),
            str(session_identity),
            str(revision_id),
            str(current_card_uuid),
        )
    )
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False) % 100


def _run_split_random(
    *,
    component: dict[str, Any],
    definition: dict[str, Any],
    current_card_uuid: str,
    runtime_variables: dict[str, Any],
    flow_uuid: str,
    session_identity: str,
    revision_id: str,
    now: datetime | None = None,
) -> str:
    variant_a_percentage, variant_b_percentage, output_var = _split_random_config(component)
    _validate_split_random_branches(
        definition=definition,
        current_card_uuid=current_card_uuid,
    )
    bucket = _split_random_bucket(
        flow_uuid=flow_uuid,
        session_identity=session_identity,
        revision_id=revision_id,
        current_card_uuid=current_card_uuid,
    )
    branch_label = "variant_a" if bucket < variant_a_percentage else "variant_b"

    variables = _ensure_variables(runtime_variables)
    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs
    customs[output_var] = branch_label
    updated_at = now or datetime.now(timezone.utc)
    runtime_variables["split_random_last_result"] = {
        "component_ref_id": component.get("ref_id"),
        "card_cursor": current_card_uuid,
        "revision_id": revision_id,
        "output_var": output_var,
        "branch": branch_label,
        "bucket": bucket,
        "variant_a_percentage": variant_a_percentage,
        "variant_b_percentage": variant_b_percentage,
        "strategy": SPLIT_RANDOM_HASH_STRATEGY,
        "updated_at": updated_at.isoformat(),
    }
    runtime_variables.pop("split_random_last_error", None)
    return branch_label


def _select_contact_channel_parameters(component: dict[str, Any]) -> dict[str, Any]:
    raw = component.get("parameters")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        parameters: dict[str, Any] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("id") or entry.get("name") or "").strip()
            if key:
                parameters[key] = entry.get("value")
        return parameters
    return {}


def _select_contact_channel_config(
    component: dict[str, Any],
) -> tuple[str, str | None, str]:
    params = _select_contact_channel_parameters(component)
    raw_channel_type = params.get("channel_type")
    if isinstance(raw_channel_type, list) and len(raw_channel_type) != 1:
        raise WorkflowExecutionError(
            "select_contact_channel_invalid_channel_type",
            "O campo channel_type deve conter uma única opção.",
        )
    channel_type = str(_catalog_parameter_scalar(raw_channel_type) or "").strip().lower()
    if channel_type not in SELECT_CONTACT_CHANNEL_TYPES:
        raise WorkflowExecutionError(
            "select_contact_channel_invalid_channel_type",
            "O campo channel_type deve ser voice, whatsapp, sms ou email.",
        )

    raw_label = params.get("channel_label")
    if isinstance(raw_label, list) and len(raw_label) > 1:
        raise WorkflowExecutionError(
            "select_contact_channel_invalid_channel_label",
            "O campo channel_label deve conter uma única label literal.",
        )
    label_scalar = _catalog_parameter_scalar(
        raw_label,
        preferred_keys=("label", "name"),
    )
    if raw_label not in (None, "", [], {}) and label_scalar is None:
        raise WorkflowExecutionError(
            "select_contact_channel_invalid_channel_label",
            "O campo channel_label deve conter uma única label literal.",
        )
    channel_label = str(label_scalar or "").strip() or None
    if channel_label is not None and (
        len(channel_label) > SELECT_CONTACT_CHANNEL_MAX_LABEL_LENGTH
        or _TEMPLATE_PATTERN.search(channel_label)
        or any(ord(character) < 32 or ord(character) == 127 for character in channel_label)
    ):
        raise WorkflowExecutionError(
            "select_contact_channel_invalid_channel_label",
            "O campo channel_label deve conter uma label literal de até 128 caracteres.",
        )

    raw_output_var = params.get("output_var")
    if isinstance(raw_output_var, list) and len(raw_output_var) > 1:
        raise WorkflowExecutionError(
            "select_contact_channel_invalid_output_var",
            "O campo output_var deve conter um único nome de variável.",
        )
    output_var = str(_catalog_parameter_scalar(raw_output_var) or "").strip()
    output_var = output_var or "selected_channel"
    if (
        len(output_var) > 128
        or _TEMPLATE_PATTERN.search(output_var)
        or SELECT_CONTACT_CHANNEL_OUTPUT_VAR_RE.fullmatch(output_var) is None
    ):
        raise WorkflowExecutionError(
            "select_contact_channel_invalid_output_var",
            "O campo output_var deve conter um nome de variável válido.",
        )
    return channel_type, channel_label, output_var


def _select_contact_channel_anchor(
    contact_row: dict[str, Any] | None,
) -> tuple[int, str, int, str | None]:
    if not isinstance(contact_row, dict):
        raise WorkflowExecutionError(
            "select_contact_channel_missing_contact_context",
            "A sessão não possui um membro de contato ativo para iniciar a seleção.",
        )
    try:
        contact_list_member_id = int(contact_row.get("contact_list_member_id"))
        contact_list_id = str(UUID(str(contact_row.get("contact_list_id"))))
        mailing_id = int(contact_row.get("mailing_id"))
    except (TypeError, ValueError) as exc:
        raise WorkflowExecutionError(
            "select_contact_channel_missing_contact_context",
            "A sessão não possui lista, mailing e membro válidos para selecionar o canal.",
        ) from exc
    if contact_list_member_id <= 0 or mailing_id <= 0:
        raise WorkflowExecutionError(
            "select_contact_channel_missing_contact_context",
            "A sessão não possui lista, mailing e membro válidos para selecionar o canal.",
        )

    person_uuid = contact_row.get("person_uuid")
    if person_uuid is not None:
        try:
            person_uuid = str(UUID(str(person_uuid)))
        except (TypeError, ValueError) as exc:
            raise WorkflowExecutionError(
                "select_contact_channel_missing_contact_context",
                "O membro atual possui uma referência de pessoa inválida.",
            ) from exc
    return contact_list_member_id, contact_list_id, mailing_id, person_uuid


def _store_select_contact_channel_result(
    *,
    runtime_variables: dict[str, Any],
    output_var: str,
    result: dict[str, Any],
    selected: bool,
) -> None:
    variables = _ensure_variables(runtime_variables)
    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs
    customs[output_var] = dict(result) if selected else None
    runtime_variables["select_contact_channel_last_result"] = dict(result)
    runtime_variables.pop("select_contact_channel_last_error", None)
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    if selected:
        workflow_meta["selected_contact_channel"] = dict(result)
    else:
        workflow_meta.pop("selected_contact_channel", None)


async def _run_select_contact_channel(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    session_id: int,
    session_scope: str,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
    contact_row: dict[str, Any] | None,
    now: datetime | None = None,
) -> _SelectContactChannelExecution:
    channel_type, channel_label, output_var = _select_contact_channel_config(component)
    (
        contact_list_member_id,
        contact_list_id,
        mailing_id,
        person_uuid,
    ) = _select_contact_channel_anchor(contact_row)
    if session_scope == "person" and person_uuid is None:
        raise WorkflowExecutionError(
            "select_contact_channel_missing_contact_context",
            "A sessão por pessoa não possui person_uuid válido para selecionar o canal.",
        )

    try:
        async with db_session.begin_nested():
            candidate = await fetch_select_contact_channel_candidate(
                db_session,
                flow_uuid=flow_uuid,
                session_id=session_id,
                session_scope=session_scope,
                contact_list_member_id=contact_list_member_id,
                contact_list_id=contact_list_id,
                mailing_id=mailing_id,
                person_uuid=person_uuid,
                channel_type=channel_type,
                channel_label=channel_label,
            )
            if candidate is not None and session_scope == "person":
                rebound = await rebind_person_session_to_contact_channel(
                    db_session,
                    flow_uuid=flow_uuid,
                    session_id=session_id,
                    contact_list_member_id=int(candidate["contact_list_member_id"]),
                    contact_list_id=str(candidate["contact_list_id"]),
                    mailing_id=int(candidate["mailing_id"]),
                    person_uuid=(
                        str(candidate["person_uuid"])
                        if candidate.get("person_uuid") is not None
                        else None
                    ),
                )
                if not rebound:
                    raise WorkflowExecutionError(
                        "select_contact_channel_rebind_failed",
                        "O canal foi encontrado, mas a sessão não pôde ser vinculada a ele com segurança.",
                    )
    except WorkflowExecutionError:
        raise
    except Exception as exc:
        raise WorkflowExecutionError(
            "select_contact_channel_persistence_failed",
            "Falha ao selecionar o canal do contato.",
        ) from exc

    updated_at = now or datetime.now(timezone.utc)
    if candidate is None:
        result = {
            "component_ref_id": component.get("ref_id"),
            "selected": False,
            "branch": "not_found",
            "session_scope": session_scope,
            "requested_type": channel_type,
            "requested_label": channel_label,
            "output_var": output_var,
            "updated_at": updated_at.isoformat(),
        }
        _store_select_contact_channel_result(
            runtime_variables=runtime_variables,
            output_var=output_var,
            result=result,
            selected=False,
        )
        return _SelectContactChannelExecution("not_found", None)

    result = {
        "component_ref_id": component.get("ref_id"),
        "selected": True,
        "branch": "selected",
        "session_scope": session_scope,
        "contact_list_member_id": int(candidate["contact_list_member_id"]),
        "contact_list_id": str(candidate["contact_list_id"]),
        "mailing_id": int(candidate["mailing_id"]),
        "person_uuid": candidate.get("person_uuid"),
        "type": _normalize_channel_type(candidate.get("contact_channel_type")),
        "label": candidate.get("contact_channel_label"),
        "address": str(candidate.get("contact_channel_address") or "").strip(),
        "is_primary": bool(candidate.get("is_primary")),
        "output_var": output_var,
        "updated_at": updated_at.isoformat(),
    }
    _store_select_contact_channel_result(
        runtime_variables=runtime_variables,
        output_var=output_var,
        result=result,
        selected=True,
    )
    return _SelectContactChannelExecution("selected", dict(candidate))


def _wait_for_event_parameters(component: dict[str, Any]) -> dict[str, Any]:
    raw = component.get("parameters")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        parameters: dict[str, Any] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("id") or entry.get("name") or "").strip()
            if key:
                parameters[key] = entry.get("value")
        return parameters
    return {}


def _wait_for_event_config(component: dict[str, Any]) -> tuple[str, str, int, str]:
    params = _wait_for_event_parameters(component)

    event_source = str(_catalog_parameter_scalar(params.get("event_source")) or "").strip().lower()
    if event_source != "callback":
        raise WorkflowExecutionError(
            "wait_for_event_invalid_event_source",
            "O campo event_source deve ser callback.",
        )

    event_result = str(_catalog_parameter_scalar(params.get("event_result")) or "").strip().lower()
    if (
        not event_result
        or len(event_result) > 128
        or WAIT_FOR_EVENT_RESULT_RE.fullmatch(event_result) is None
    ):
        raise WorkflowExecutionError(
            "wait_for_event_invalid_event_result",
            "O campo event_result deve conter um resultado literal válido.",
        )

    raw_timeout = _catalog_parameter_scalar(params.get("timeout_seconds"))
    try:
        if isinstance(raw_timeout, bool):
            raise ValueError
        timeout_seconds = int(str(raw_timeout).strip())
    except (TypeError, ValueError):
        timeout_seconds = 0
    if not 1 <= timeout_seconds <= WAIT_FOR_EVENT_MAX_TIMEOUT_SECONDS:
        raise WorkflowExecutionError(
            "wait_for_event_invalid_timeout_seconds",
            "O campo timeout_seconds deve ser um inteiro entre 1 e 2592000.",
        )

    output_var = str(_catalog_parameter_scalar(params.get("output_var")) or "wait_event").strip()
    if (
        not output_var
        or len(output_var) > 128
        or WAIT_FOR_EVENT_OUTPUT_VAR_RE.fullmatch(output_var) is None
    ):
        raise WorkflowExecutionError(
            "wait_for_event_invalid_output_var",
            "O campo output_var deve conter um nome de variável válido.",
        )

    return event_source, event_result, timeout_seconds, output_var


def _clear_wait_for_event_state(runtime_variables: dict[str, Any]) -> None:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    workflow_meta.pop("wait_for_event", None)


def _wait_for_event_matching_callback_index(
    runtime_variables: dict[str, Any],
    *,
    state: dict[str, Any],
) -> int | None:
    callbacks_pending = runtime_variables.get("callbacks_pending")
    if not isinstance(callbacks_pending, list):
        return None

    try:
        pending_start_index = max(0, int(state.get("pending_start_index", 0)))
    except (TypeError, ValueError):
        return None

    timeout_at = _parse_iso_datetime(state.get("timeout_at"))
    if timeout_at is None:
        return None
    timeout_at_utc = timeout_at if timeout_at.tzinfo is not None else timeout_at.replace(tzinfo=timezone.utc)
    expected_source = str(state.get("event_source") or "").strip().lower()
    expected_result = str(state.get("event_result") or "").strip().lower()

    for index, callback in enumerate(callbacks_pending):
        if index < pending_start_index or not isinstance(callback, dict):
            continue
        if str(callback.get("event_name") or "").strip().lower() != expected_source:
            continue
        if str(callback.get("result") or "").strip().lower() != expected_result:
            continue
        received_at = _parse_iso_datetime(callback.get("received_at"))
        if received_at is None:
            continue
        received_at_utc = received_at if received_at.tzinfo is not None else received_at.replace(tzinfo=timezone.utc)
        if received_at_utc <= timeout_at_utc:
            return index
    return None


def _should_resume_wait_for_event_blocking_execution(
    runtime_variables: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    raw_state = workflow_meta.get("wait_for_event")
    if not isinstance(raw_state, dict):
        return True
    timeout_at = _parse_iso_datetime(raw_state.get("timeout_at"))
    if timeout_at is None:
        return True
    if _wait_for_event_matching_callback_index(runtime_variables, state=raw_state) is not None:
        return True
    current_time = now or datetime.now(timezone.utc)
    current_time_utc = current_time if current_time.tzinfo is not None else current_time.replace(tzinfo=timezone.utc)
    timeout_at_utc = timeout_at if timeout_at.tzinfo is not None else timeout_at.replace(tzinfo=timezone.utc)
    return current_time_utc >= timeout_at_utc


def _store_wait_for_event_output(
    *,
    runtime_variables: dict[str, Any],
    output_var: str,
    output: dict[str, Any],
    component_ref_id: str | None,
    updated_at: datetime,
) -> None:
    variables = _ensure_variables(runtime_variables)
    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs
    customs[output_var] = copy.deepcopy(output)
    runtime_variables["wait_for_event_last_result"] = {
        "component_ref_id": component_ref_id,
        "output_var": output_var,
        "result": copy.deepcopy(output),
        "updated_at": updated_at.isoformat(),
    }


def _run_wait_for_event(
    *,
    component: dict[str, Any],
    current_card_uuid: str,
    runtime_variables: dict[str, Any],
    now: datetime | None = None,
) -> _WaitForEventExecution:
    event_source, event_result, timeout_seconds, output_var = _wait_for_event_config(component)
    current_time = now or datetime.now(timezone.utc)
    current_time_utc = current_time if current_time.tzinfo is not None else current_time.replace(tzinfo=timezone.utc)
    workflow_meta = _ensure_workflow_meta(runtime_variables)
    raw_state = workflow_meta.get("wait_for_event")

    if raw_state is None:
        callbacks_pending = runtime_variables.get("callbacks_pending")
        pending_start_index = len(callbacks_pending) if isinstance(callbacks_pending, list) else 0
        timeout_at = current_time_utc + timedelta(seconds=timeout_seconds)
        workflow_meta["wait_for_event"] = {
            "component_ref_id": component.get("ref_id"),
            "card_cursor": current_card_uuid,
            "event_source": event_source,
            "event_result": event_result,
            "timeout_seconds": timeout_seconds,
            "output_var": output_var,
            "pending_start_index": pending_start_index,
            "blocked_at": current_time_utc.isoformat(),
            "timeout_at": timeout_at.isoformat(),
            "status": "waiting",
        }
        runtime_variables.pop("wait_for_event_last_error", None)
        return _WaitForEventExecution(branch_label=None, timeout_at=timeout_at)

    if not isinstance(raw_state, dict):
        raise WorkflowExecutionError(
            "wait_for_event_state_mismatch",
            "O estado persistido do wait_for_event é inválido.",
        )

    expected_state = {
        "card_cursor": current_card_uuid,
        "event_source": event_source,
        "event_result": event_result,
        "timeout_seconds": timeout_seconds,
        "output_var": output_var,
    }
    if any(raw_state.get(key) != value for key, value in expected_state.items()):
        raise WorkflowExecutionError(
            "wait_for_event_state_mismatch",
            "O estado persistido do wait_for_event não corresponde ao card atual.",
        )

    timeout_at = _parse_iso_datetime(raw_state.get("timeout_at"))
    if timeout_at is None:
        raise WorkflowExecutionError(
            "wait_for_event_state_mismatch",
            "O prazo persistido do wait_for_event é inválido.",
        )
    timeout_at_utc = timeout_at if timeout_at.tzinfo is not None else timeout_at.replace(tzinfo=timezone.utc)

    callback_index = _wait_for_event_matching_callback_index(runtime_variables, state=raw_state)
    callbacks_pending = runtime_variables.get("callbacks_pending")
    if callback_index is not None and isinstance(callbacks_pending, list):
        callback = callbacks_pending.pop(callback_index)
        if isinstance(callback, dict):
            runtime_variables["callback"] = copy.deepcopy(callback)
            output = {
                "status": "received",
                "event_source": event_source,
                "event_result": event_result,
                "received_at": callback.get("received_at"),
                "data": copy.deepcopy(callback.get("data")) if isinstance(callback.get("data"), dict) else {},
            }
            _store_wait_for_event_output(
                runtime_variables=runtime_variables,
                output_var=output_var,
                output=output,
                component_ref_id=(str(component.get("ref_id")) if component.get("ref_id") is not None else None),
                updated_at=current_time_utc,
            )
            _clear_wait_for_event_state(runtime_variables)
            runtime_variables.pop("wait_for_event_last_error", None)
            return _WaitForEventExecution(branch_label="received", timeout_at=timeout_at_utc)

    if current_time_utc >= timeout_at_utc:
        output = {
            "status": "timeout",
            "event_source": event_source,
            "event_result": event_result,
            "timeout_at": timeout_at_utc.isoformat(),
        }
        _store_wait_for_event_output(
            runtime_variables=runtime_variables,
            output_var=output_var,
            output=output,
            component_ref_id=(str(component.get("ref_id")) if component.get("ref_id") is not None else None),
            updated_at=current_time_utc,
        )
        _clear_wait_for_event_state(runtime_variables)
        runtime_variables.pop("wait_for_event_last_error", None)
        return _WaitForEventExecution(branch_label="timeout", timeout_at=timeout_at_utc)

    return _WaitForEventExecution(branch_label=None, timeout_at=timeout_at_utc)


def _identidade_enum_parameter(
    params: dict[str, Any],
    *,
    field: str,
    allowed: set[str],
    default: str,
) -> str:
    raw = _catalog_parameter_scalar(params.get(field))
    value = str(raw or default).strip().lower()
    if value not in allowed:
        raise WorkflowExecutionError(
            f"identidade_person_invalid_{field}",
            f"O campo {field} possui um valor inválido.",
        )
    return value


def _identidade_mailing_public_id(value: Any) -> str | None:
    scalar = _catalog_parameter_scalar(
        value,
        preferred_keys=("mailing_id", "public_id", "source_list_id", "uuid"),
    )
    normalized = str(scalar or "").strip()
    if not normalized:
        return None
    try:
        return str(UUID(normalized))
    except (TypeError, ValueError, AttributeError) as exc:
        raise WorkflowExecutionError(
            "identidade_person_invalid_mailing_id",
            "O campo mailing_id deve conter uma lista válida.",
        ) from exc


def _identidade_output_var(value: Any) -> str:
    normalized = str(_catalog_parameter_scalar(value) or "identidade").strip()
    if not normalized or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", normalized):
        raise WorkflowExecutionError(
            "identidade_person_invalid_output_var",
            "O campo output_var deve ser um caminho de variável válido.",
        )
    return normalized


def _store_identidade_output(
    *,
    runtime_variables: dict[str, Any],
    output_var: str,
    output: dict[str, Any],
    component_ref_id: str | None,
    document: str,
    status_code: int,
    attempts: int,
) -> None:
    variables = _ensure_variables(runtime_variables)
    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs
    _set_by_path(customs, output_var, output)
    runtime_variables["identidade_person_last_result"] = {
        "component_ref_id": component_ref_id,
        "document_masked": mask_document(document),
        "found": bool(output.get("found")),
        "status_code": status_code,
        "attempts": attempts,
        "output_var": output_var,
        "result": output,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _update_identidade_flow_link_output(
    runtime_variables: dict[str, Any],
    *,
    flow_link_state: dict[str, Any],
    status: str,
) -> None:
    last_result = runtime_variables.get("identidade_person_last_result")
    outputs: list[dict[str, Any]] = []
    if isinstance(last_result, dict):
        result = last_result.get("result")
        if isinstance(result, dict):
            outputs.append(result)
        output_var = str(last_result.get("output_var") or "").strip()
        variables = runtime_variables.get("variables")
        customs = variables.get("customs") if isinstance(variables, dict) else None
        current: Any = customs
        for part in [value for value in output_var.split(".") if value]:
            current = current.get(part) if isinstance(current, dict) else None
        if isinstance(current, dict) and all(current is not item for item in outputs):
            outputs.append(current)

    for output in outputs:
        mailing_action = output.get("mailing_action")
        if not isinstance(mailing_action, dict):
            continue
        mailing_action.update(
            {
                "flow_link": status,
                "flow_link_attempts": flow_link_state.get("attempts"),
                "flow_link_status_code": flow_link_state.get("status_code"),
            }
        )


async def _resolve_identidade_query(
    *,
    component_ref_id: str,
    workspace_id: str,
    access_token: str,
    document: str,
    require_phone: bool,
    require_email: bool,
    runtime_variables: dict[str, Any],
) -> IdentidadePersonQueryResult:
    cache_key = hashlib.sha256(
        f"{component_ref_id}:{workspace_id}:{document}:{int(require_phone)}:{int(require_email)}".encode("utf-8")
    ).hexdigest()
    pending = runtime_variables.get("identidade_person_pending_query")
    if isinstance(pending, dict) and pending.get("cache_key") == cache_key:
        cached_person = pending.get("person")
        return IdentidadePersonQueryResult(
            found=bool(pending.get("found")),
            person=dict(cached_person) if isinstance(cached_person, dict) else None,
            attempts=int(pending.get("attempts") or 1),
            status_code=int(pending.get("status_code") or 200),
        )

    result = await query_identidade_person(
        workspace_id=workspace_id,
        access_token=access_token,
        document=document,
        require_phone=require_phone,
        require_email=require_email,
    )
    runtime_variables["identidade_person_pending_query"] = {
        "cache_key": cache_key,
        "component_ref_id": component_ref_id,
        "document_masked": mask_document(document),
        "found": result.found,
        "person": result.person,
        "attempts": result.attempts,
        "status_code": result.status_code,
        "queried_at": datetime.now(timezone.utc).isoformat(),
    }
    return result


async def _persist_identidade_person_action(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    normalized_person: dict[str, Any],
    person_action: str,
    enrichment_policy: str,
    mailing_public_id: str | None,
    link_mailing_to_current_flow: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    local_action: dict[str, Any] = {
        "requested": person_action,
        "status": "not_written",
        "person_uuid": None,
    }
    mailing_action: dict[str, Any] = {
        "requested": mailing_public_id is not None,
        "mailing_id": mailing_public_id,
        "status": "not_requested" if mailing_public_id is None else "pending",
        "flow_link": "not_requested" if not link_mailing_to_current_flow else "pending",
    }
    if person_action == "lookup_only":
        if mailing_public_id is not None:
            mailing_action["status"] = "ignored_lookup_only"
        if link_mailing_to_current_flow:
            mailing_action["flow_link"] = "ignored_lookup_only"
        return local_action, mailing_action

    identifier = str(normalized_person["identifier"])
    try:
        async with db_session.begin_nested():
            existing = await fetch_person_by_identifier_for_update(
                db_session,
                identifier=identifier,
            )
            local_person: dict[str, Any] | None = None
            if person_action == "create_if_missing":
                if existing is not None:
                    local_person = existing
                    local_action["status"] = "already_exists_unchanged"
                else:
                    local_person = await insert_person_if_missing(db_session, payload=normalized_person)
                    if local_person is None:
                        local_person = await fetch_person_by_identifier_for_update(db_session, identifier=identifier)
                        local_action["status"] = "already_exists_unchanged"
                    else:
                        local_action["status"] = "created"
            elif person_action == "enrich_if_found":
                if existing is None:
                    local_action["status"] = "local_person_not_found"
                else:
                    merged = merge_person_payload(
                        existing,
                        normalized_person,
                        enrichment_policy=enrichment_policy,
                    )
                    local_person = await update_person_from_payload(
                        db_session,
                        person_uuid=str(existing["uuid"]),
                        payload=merged,
                    )
                    local_action["status"] = "enriched"
            elif person_action == "upsert":
                if existing is None:
                    local_person = await insert_person_if_missing(db_session, payload=normalized_person)
                    if local_person is None:
                        existing = await fetch_person_by_identifier_for_update(db_session, identifier=identifier)
                    if local_person is not None:
                        local_action["status"] = "created"
                if local_person is None and existing is not None:
                    merged = merge_person_payload(
                        existing,
                        normalized_person,
                        enrichment_policy=enrichment_policy,
                    )
                    local_person = await update_person_from_payload(
                        db_session,
                        person_uuid=str(existing["uuid"]),
                        payload=merged,
                    )
                    local_action["status"] = "enriched"

            if local_person is not None:
                local_action["person_uuid"] = local_person.get("uuid")

            if mailing_public_id is not None and local_person is not None:
                source_list = await resolve_source_list_by_public_id(
                    db_session,
                    public_id=mailing_public_id,
                )
                if source_list is None:
                    raise WorkflowExecutionError(
                        "identidade_person_mailing_not_found",
                        "A lista selecionada em mailing_id não foi encontrada.",
                    )
                source_status = str(source_list.get("status") or "").strip().upper()
                if source_status not in {"READY_TO_INGEST", "PROCESSED"}:
                    raise WorkflowExecutionError(
                        "identidade_person_mailing_not_ready",
                        "A lista selecionada precisa estar pronta ou processada.",
                    )
                membership = await ensure_person_in_source_list(
                    db_session,
                    source_list_id=int(source_list["id"]),
                    person=local_person,
                )
                mailing_action.update(
                    {
                        "status": "added" if membership.get("created") else "already_present",
                        "source_list_id": source_list.get("id"),
                        "contact_draft_id": membership.get("contact_draft_id"),
                        "channels": membership.get("channels"),
                    }
                )
                if link_mailing_to_current_flow:
                    active_link = await fetch_active_flow_mailing_link(
                        db_session,
                        flow_uuid=flow_uuid,
                        source_list_id=int(source_list["id"]),
                    )
                    if active_link is None:
                        mailing_action["flow_link"] = "pending"
                    else:
                        # O vínculo ativo também precisa ser atualizado no Target Core,
                        # pois o contact_draft acabou de ser criado/atualizado nesta transação.
                        mailing_action["flow_link"] = "pending"
                        mailing_action["flow_link_previous_state"] = "already_linked"
                        mailing_action["contact_list_id"] = active_link.get("contact_list_id")
            elif mailing_public_id is not None:
                mailing_action["status"] = "skipped_no_local_person"
                if link_mailing_to_current_flow:
                    mailing_action["flow_link"] = "skipped_no_local_person"
    except WorkflowExecutionError:
        raise
    except Exception as exc:
        raise WorkflowExecutionError(
            "identidade_person_persistence_failed",
            "Falha ao persistir os dados retornados pela Identidade.",
        ) from exc

    return local_action, mailing_action


async def _run_identidade_person(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str | None:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    component_ref_id = str(component.get("ref_id") or component.get("uuid") or "").strip()
    if not component_ref_id:
        raise WorkflowExecutionError(
            "identidade_person_missing_ref_id",
            "O componente identidade_person não possui ref_id.",
        )

    flow_link_state = _identidade_person_flow_link_state(runtime_variables)
    if isinstance(flow_link_state, dict) and str(flow_link_state.get("component_ref_id") or "") == component_ref_id:
        flow_link_status = str(flow_link_state.get("status") or "").strip().lower()
        if flow_link_status == "pending":
            return None
        if flow_link_status == "completed":
            _update_identidade_flow_link_output(
                runtime_variables,
                flow_link_state=flow_link_state,
                status="linked",
            )
            flow_link_state["status"] = "consumed"
            return "encontrado"
        if flow_link_status == "failed":
            _update_identidade_flow_link_output(
                runtime_variables,
                flow_link_state=flow_link_state,
                status="failed",
            )
            flow_link_state["status"] = "consumed"
            error = flow_link_state.get("last_error")
            error = error if isinstance(error, dict) else {}
            raise WorkflowExecutionError(
                str(error.get("code") or "identidade_person_flow_link_failed"),
                str(error.get("message") or "Falha ao vincular a lista ao fluxo atual."),
            )

    variables = _ensure_variables(runtime_variables)
    resolution_scope = _build_runtime_resolution_scope(
        runtime_variables=runtime_variables,
        variables=variables,
    )
    rendered_document = _render_value(params.get("document"), resolution_scope)
    try:
        document = normalize_document(rendered_document)
        workspace_id = normalize_workspace_id(_catalog_parameter_scalar(params.get("workspace_id")))
    except IdentidadePersonServiceError as exc:
        raise WorkflowExecutionError(exc.code, exc.message) from exc
    access_token = str(_catalog_parameter_scalar(params.get("access_token")) or "").strip()
    if not access_token:
        raise WorkflowExecutionError(
            "identidade_person_missing_access_token",
            "O campo access_token é obrigatório.",
        )

    require_phone = _catalog_parameter_bool(params.get("require_phone"))
    require_email = _catalog_parameter_bool(params.get("require_email"))
    person_action = _identidade_enum_parameter(
        params,
        field="person_action",
        allowed={"lookup_only", "create_if_missing", "enrich_if_found", "upsert"},
        default="lookup_only",
    )
    enrichment_policy = _identidade_enum_parameter(
        params,
        field="enrichment_policy",
        allowed={"fill_missing", "overwrite_non_null"},
        default="fill_missing",
    )
    phone_policy = _identidade_enum_parameter(
        params,
        field="phone_policy",
        allowed={"best_eligible", "all_eligible", "none"},
        default="best_eligible",
    )
    response_detail = _identidade_enum_parameter(
        params,
        field="response_detail",
        allowed={"normalized", "complete"},
        default="normalized",
    )
    mailing_public_id = _identidade_mailing_public_id(params.get("mailing_id"))
    link_mailing_to_current_flow = _catalog_parameter_bool(params.get("link_mailing_to_current_flow"))
    if link_mailing_to_current_flow and mailing_public_id is None:
        raise WorkflowExecutionError(
            "identidade_person_flow_link_without_mailing",
            "Selecione mailing_id para vincular uma lista ao fluxo atual.",
        )
    output_var = _identidade_output_var(params.get("output_var"))

    logger.info(
        "workflow m2 identidade person query started",
        extra={
            "event": "orch.workflow.m2.identidade_person.started",
            "flow_uuid": flow_uuid,
            "component_ref_id": component_ref_id,
            "document_masked": mask_document(document),
            "person_action": person_action,
        },
    )
    try:
        query_result = await _resolve_identidade_query(
            component_ref_id=component_ref_id,
            workspace_id=workspace_id,
            access_token=access_token,
            document=document,
            require_phone=require_phone,
            require_email=require_email,
            runtime_variables=runtime_variables,
        )
    except IdentidadePersonServiceError as exc:
        raise WorkflowExecutionError(exc.code, exc.message) from exc

    if not query_result.found or query_result.person is None:
        output = {
            "found": False,
            "person": None,
            "local_action": {"requested": person_action, "status": "not_executed"},
            "mailing_action": {"requested": mailing_public_id is not None, "status": "not_executed"},
        }
        _store_identidade_output(
            runtime_variables=runtime_variables,
            output_var=output_var,
            output=output,
            component_ref_id=component_ref_id,
            document=document,
            status_code=query_result.status_code,
            attempts=query_result.attempts,
        )
        runtime_variables.pop("identidade_person_pending_query", None)
        return "nao_encontrado"

    try:
        normalized_person = normalize_identidade_person(
            query_result.person,
            fallback_document=document,
            phone_policy=phone_policy,
        )
    except IdentidadePersonServiceError as exc:
        raise WorkflowExecutionError(exc.code, exc.message) from exc
    local_action, mailing_action = await _persist_identidade_person_action(
        db_session=db_session,
        flow_uuid=flow_uuid,
        normalized_person=normalized_person,
        person_action=person_action,
        enrichment_policy=enrichment_policy,
        mailing_public_id=mailing_public_id,
        link_mailing_to_current_flow=link_mailing_to_current_flow,
    )
    normalized_output = build_normalized_output(
        normalized_person=normalized_person,
        local_action=local_action,
        mailing_action=mailing_action,
    )
    output = normalized_output
    if response_detail == "complete":
        output = {
            **normalized_output,
            "provider_response": query_result.person,
        }
    _store_identidade_output(
        runtime_variables=runtime_variables,
        output_var=output_var,
        output=output,
        component_ref_id=component_ref_id,
        document=document,
        status_code=query_result.status_code,
        attempts=query_result.attempts,
    )
    runtime_variables.pop("identidade_person_pending_query", None)
    if mailing_action.get("flow_link") == "pending" and mailing_public_id is not None:
        workflow_meta = _ensure_workflow_meta(runtime_variables)
        workflow_meta["identidade_person_flow_link"] = {
            "component_ref_id": component_ref_id,
            "flow_uuid": flow_uuid,
            "mailing_uuid": mailing_public_id,
            "status": "pending",
            "attempts": 0,
            "status_code": None,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "last_error": None,
        }
        return None
    logger.info(
        "workflow m2 identidade person query completed",
        extra={
            "event": "orch.workflow.m2.identidade_person.completed",
            "flow_uuid": flow_uuid,
            "component_ref_id": component_ref_id,
            "document_masked": mask_document(document),
            "person_action_status": local_action.get("status"),
            "mailing_action_status": mailing_action.get("status"),
        },
    )
    return "encontrado"


CREATE_CONTACT_ACTIONS = {"update_current", "create_if_missing", "upsert"}
CREATE_CONTACT_ENRICHMENT_POLICIES = {"fill_missing", "overwrite_non_null"}
CREATE_CONTACT_PROFILE_FIELDS = (
    "full_name",
    "company",
    "gender",
    "role",
    "country",
    "state",
    "city",
    "birthdate",
)
CREATE_CONTACT_EXTRA_FIELD_RE = re.compile(
    r"extra\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
CREATE_CONTACT_OUTPUT_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")


def _create_contact_parameters(component: dict[str, Any]) -> dict[str, Any]:
    raw = component.get("parameters")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        parameters: dict[str, Any] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("id") or entry.get("name") or "").strip()
            if key:
                parameters[key] = entry.get("value")
        return parameters
    return {}


def _create_contact_enum_parameter(
    params: dict[str, Any],
    *,
    field: str,
    allowed: set[str],
) -> str:
    value = str(_catalog_parameter_scalar(params.get(field)) or "").strip().lower()
    if value not in allowed:
        raise WorkflowExecutionError(
            f"create_contact_invalid_{field}",
            f"O campo {field} possui um valor inválido.",
        )
    return value


def _create_contact_output_var(value: Any) -> str:
    normalized = str(_catalog_parameter_scalar(value) or "contact_action").strip()
    if (
        not normalized
        or len(normalized) > 128
        or CREATE_CONTACT_OUTPUT_VAR_RE.fullmatch(normalized) is None
    ):
        raise WorkflowExecutionError(
            "create_contact_invalid_output_var",
            "O campo output_var deve conter um nome de variável válido.",
        )
    return normalized


def _create_contact_mapping_entries(mapping: Any) -> list[tuple[str, Any]]:
    if isinstance(mapping, dict):
        entries = [(str(key).strip(), value) for key, value in mapping.items()]
    elif isinstance(mapping, list):
        entries = []
        for entry in mapping:
            if not isinstance(entry, dict):
                raise WorkflowExecutionError(
                    "create_contact_invalid_mapping",
                    "O campo mapping possui uma estrutura inválida.",
                )
            entries.append((str(entry.get("key") or "").strip(), entry.get("value")))
    else:
        entries = []

    if not entries:
        raise WorkflowExecutionError(
            "create_contact_missing_mapping",
            "O campo mapping deve declarar ao menos um dado do contato.",
        )

    seen: set[str] = set()
    extra_paths: list[tuple[str, ...]] = []
    for key, _ in entries:
        if (
            not key
            or key in seen
            or len(key) > 128
            or (
                key not in CREATE_CONTACT_PROFILE_FIELDS
                and CREATE_CONTACT_EXTRA_FIELD_RE.fullmatch(key) is None
            )
        ):
            raise WorkflowExecutionError(
                "create_contact_invalid_mapping_field",
                "O mapping contém campo vazio, duplicado ou não permitido.",
            )
        if key.startswith("extra."):
            path = tuple(key.removeprefix("extra.").split("."))
            if any(
                path[: len(existing_path)] == existing_path
                or existing_path[: len(path)] == path
                for existing_path in extra_paths
            ):
                raise WorkflowExecutionError(
                    "create_contact_conflicting_extra_mapping",
                    "O mapping contém chaves extra com caminhos conflitantes.",
                )
            extra_paths.append(path)
        seen.add(key)
    return entries


def _create_contact_is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == [] or value == {}


def _create_contact_profile_value(field: str, value: Any) -> Any:
    if _create_contact_is_blank(value):
        return None
    if isinstance(value, (dict, list, tuple, set)):
        raise WorkflowExecutionError(
            "create_contact_invalid_mapping_value",
            f"O campo {field} deve resultar em um valor simples.",
        )
    if field == "birthdate":
        try:
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            return date.fromisoformat(str(value).strip())
        except ValueError as exc:
            raise WorkflowExecutionError(
                "create_contact_invalid_birthdate",
                "O campo birthdate deve usar o formato AAAA-MM-DD.",
            ) from exc
    return str(value).strip()


def _create_contact_json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _create_contact_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_create_contact_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _build_create_contact_payload(
    *,
    mapping: Any,
    resolution_scope: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    payload: dict[str, Any] = {}
    extras: dict[str, Any] = {}
    configured_fields: list[str] = []
    for key, raw_value in _create_contact_mapping_entries(mapping):
        value = _render_value(raw_value, resolution_scope)
        if _create_contact_is_blank(value):
            continue
        if key in CREATE_CONTACT_PROFILE_FIELDS:
            payload[key] = _create_contact_profile_value(key, value)
        else:
            _set_by_path(extras, key.removeprefix("extra."), _create_contact_json_safe(value))
        configured_fields.append(key)

    if not configured_fields:
        raise WorkflowExecutionError(
            "create_contact_empty_mapping",
            "Nenhum valor do mapping pôde ser resolvido no runtime.",
        )
    payload["extras"] = extras
    return payload, configured_fields


def _create_contact_existing_extras(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return copy.deepcopy(parsed) if isinstance(parsed, dict) else {}
    return {}


def _merge_create_contact_extras(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    overwrite: bool,
    prefix: str = "extra",
) -> tuple[dict[str, Any], list[str]]:
    merged = copy.deepcopy(existing)
    changed_fields: list[str] = []
    for key, value in incoming.items():
        path = f"{prefix}.{key}"
        current = merged.get(key)
        if isinstance(value, dict):
            if isinstance(current, dict):
                nested_base = current
            elif overwrite or _create_contact_is_blank(current):
                nested_base = {}
            else:
                continue
            nested, nested_changes = _merge_create_contact_extras(
                nested_base, value, overwrite=overwrite, prefix=path
            )
            if nested_changes:
                merged[key] = nested
                changed_fields.extend(nested_changes)
        elif (overwrite or _create_contact_is_blank(current)) and current != value:
            merged[key] = copy.deepcopy(value)
            changed_fields.append(path)
    return merged, changed_fields


def _merge_create_contact_payload(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    enrichment_policy: str,
) -> tuple[dict[str, Any], list[str]]:
    overwrite = enrichment_policy == "overwrite_non_null"
    merged = {field: existing.get(field) for field in CREATE_CONTACT_PROFILE_FIELDS}
    changed_fields: list[str] = []
    for field in CREATE_CONTACT_PROFILE_FIELDS:
        if field not in incoming:
            continue
        current = existing.get(field)
        value = incoming[field]
        if (overwrite or _create_contact_is_blank(current)) and current != value:
            merged[field] = value
            changed_fields.append(field)

    existing_extras = _create_contact_existing_extras(existing.get("extras"))
    incoming_extras = incoming.get("extras") if isinstance(incoming.get("extras"), dict) else {}
    merged_extras, extra_changes = _merge_create_contact_extras(
        existing_extras,
        incoming_extras,
        overwrite=overwrite,
    )
    merged["extras"] = merged_extras
    changed_fields.extend(extra_changes)
    return merged, changed_fields


def _create_contact_identifier(value: Any, resolution_scope: dict[str, Any]) -> str:
    rendered = _render_value(value, resolution_scope)
    if isinstance(rendered, (dict, list, tuple, set, bool)):
        rendered = None
    normalized = str(rendered or "").strip()
    if not normalized:
        raise WorkflowExecutionError(
            "create_contact_missing_identifier",
            "O campo identifier deve resultar em um valor não vazio.",
        )
    return normalized


def _store_create_contact_output(
    *,
    runtime_variables: dict[str, Any],
    output_var: str,
    output: dict[str, Any],
    component_ref_id: str | None,
) -> None:
    variables = _ensure_variables(runtime_variables)
    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs
    _set_by_path(customs, output_var, output)
    runtime_variables["create_contact_last_result"] = {
        "component_ref_id": component_ref_id,
        "output_var": output_var,
        "result": copy.deepcopy(output),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _run_create_contact(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
    contact_row: dict[str, Any] | None,
) -> str:
    params = _create_contact_parameters(component)
    action = _create_contact_enum_parameter(
        params,
        field="person_action",
        allowed=CREATE_CONTACT_ACTIONS,
    )
    enrichment_policy = _create_contact_enum_parameter(
        params,
        field="enrichment_policy",
        allowed=CREATE_CONTACT_ENRICHMENT_POLICIES,
    )
    output_var = _create_contact_output_var(params.get("output_var"))
    variables = _ensure_variables(runtime_variables)
    resolution_scope = _build_runtime_resolution_scope(
        runtime_variables=runtime_variables,
        variables=variables,
    )
    component_ref_id = str(component.get("ref_id") or component.get("uuid") or "").strip() or None
    identifier: str | None = None
    person: dict[str, Any] | None = None
    branch = "unchanged"
    changed_fields: list[str] = []

    logger.info(
        "workflow m2 create contact started",
        extra={
            "event": "orch.workflow.m2.create_contact.started",
            "flow_uuid": flow_uuid,
            "component_ref_id": component_ref_id,
            "person_action": action,
            "enrichment_policy": enrichment_policy,
        },
    )

    try:
        async with db_session.begin_nested():
            existing: dict[str, Any] | None
            if action == "update_current":
                person_uuid = str(contact_row.get("person_uuid") or "").strip() if isinstance(contact_row, dict) else ""
                if not person_uuid:
                    branch = "not_found"
                else:
                    try:
                        person_uuid = str(UUID(person_uuid))
                    except (TypeError, ValueError, AttributeError) as exc:
                        raise WorkflowExecutionError(
                            "create_contact_invalid_current_person",
                            "A sessão atual possui uma referência de pessoa inválida.",
                        ) from exc
                    existing = await fetch_create_contact_person_by_uuid_for_update(
                        db_session,
                        person_uuid=person_uuid,
                    )
                    if existing is None:
                        branch = "not_found"
                    else:
                        incoming, _ = _build_create_contact_payload(
                            mapping=params.get("mapping"),
                            resolution_scope=resolution_scope,
                        )
                        identifier = str(existing.get("identifier") or "").strip() or None
                        merged, changed_fields = _merge_create_contact_payload(
                            existing,
                            incoming,
                            enrichment_policy=enrichment_policy,
                        )
                        if changed_fields:
                            person = await update_create_contact_person_profile(
                                db_session,
                                person_uuid=person_uuid,
                                payload=merged,
                            )
                            if person is None:
                                raise WorkflowExecutionError(
                                    "create_contact_current_person_not_found",
                                    "A pessoa atual deixou de estar disponível durante a atualização.",
                                )
                            branch = "updated"
                        else:
                            person = existing
            else:
                identifier = _create_contact_identifier(params.get("identifier"), resolution_scope)
                existing = await fetch_create_contact_person_by_identifier_for_update(
                    db_session,
                    identifier=identifier,
                )
                if existing is None:
                    incoming, configured_fields = _build_create_contact_payload(
                        mapping=params.get("mapping"),
                        resolution_scope=resolution_scope,
                    )
                    person = await insert_create_contact_person_if_missing(
                        db_session,
                        identifier=identifier,
                        payload=incoming,
                    )
                    if person is not None:
                        branch = "created"
                        changed_fields = list(configured_fields)
                    else:
                        existing = await fetch_create_contact_person_by_identifier_for_update(
                            db_session,
                            identifier=identifier,
                        )
                        if existing is None:
                            raise WorkflowExecutionError(
                                "create_contact_person_not_found_after_conflict",
                                "A pessoa não pôde ser recuperada após conflito de criação.",
                            )

                if person is None and existing is not None:
                    person = existing
                    if action == "upsert":
                        incoming, _ = _build_create_contact_payload(
                            mapping=params.get("mapping"),
                            resolution_scope=resolution_scope,
                        )
                        merged, changed_fields = _merge_create_contact_payload(
                            existing,
                            incoming,
                            enrichment_policy=enrichment_policy,
                        )
                        if changed_fields:
                            person = await update_create_contact_person_profile(
                                db_session,
                                person_uuid=str(existing["uuid"]),
                                payload=merged,
                            )
                            if person is None:
                                raise WorkflowExecutionError(
                                    "create_contact_person_not_found_during_update",
                                    "A pessoa deixou de estar disponível durante a atualização.",
                                )
                            branch = "updated"
    except WorkflowExecutionError:
        raise
    except Exception as exc:
        raise WorkflowExecutionError(
            "create_contact_persistence_failed",
            "Falha ao criar ou atualizar a pessoa.",
        ) from exc

    if person is not None:
        identifier = str(person.get("identifier") or identifier or "").strip() or None
    output = {
        "action": branch,
        "person_uuid": str(person.get("uuid")) if person is not None and person.get("uuid") else None,
        "identifier": identifier,
        "changed_fields": changed_fields,
    }
    _store_create_contact_output(
        runtime_variables=runtime_variables,
        output_var=output_var,
        output=output,
        component_ref_id=component_ref_id,
    )
    runtime_variables.pop("create_contact_last_error", None)
    logger.info(
        "workflow m2 create contact completed",
        extra={
            "event": "orch.workflow.m2.create_contact.completed",
            "flow_uuid": flow_uuid,
            "component_ref_id": component_ref_id,
            "person_action": action,
            "result_action": branch,
            "changed_fields": changed_fields,
        },
    )
    return branch


SOURCE_LIST_MEMBERSHIP_OUTPUT_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")


def _source_list_membership_parameters(component: dict[str, Any]) -> dict[str, Any]:
    raw = component.get("parameters")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        parameters: dict[str, Any] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("id") or entry.get("name") or "").strip()
            if key:
                parameters[key] = entry.get("value")
        return parameters
    return {}


def _source_list_membership_person_uuid(
    value: Any,
    *,
    resolution_scope: dict[str, Any],
) -> str | None:
    raw = _catalog_parameter_scalar(
        value,
        preferred_keys=("person_uuid", "uuid"),
    )
    rendered = _render_value(raw, resolution_scope)
    if rendered is None or (isinstance(rendered, str) and not rendered.strip()):
        return None
    if isinstance(rendered, (dict, list, tuple, set, bool)):
        raise WorkflowExecutionError(
            "source_list_membership_invalid_person_uuid",
            "O campo person_uuid deve resultar em um UUID de pessoa válido.",
        )
    try:
        return str(UUID(str(rendered).strip()))
    except (TypeError, ValueError, AttributeError) as exc:
        raise WorkflowExecutionError(
            "source_list_membership_invalid_person_uuid",
            "O campo person_uuid deve resultar em um UUID de pessoa válido.",
        ) from exc


def _source_list_membership_mailing_public_id(value: Any) -> str:
    scalar = _catalog_parameter_scalar(
        value,
        preferred_keys=("mailing_id", "public_id", "source_list_id", "uuid"),
    )
    normalized = str(scalar or "").strip()
    if not normalized:
        raise WorkflowExecutionError(
            "source_list_membership_missing_mailing_id",
            "O campo mailing_id deve selecionar uma lista.",
        )
    try:
        return str(UUID(normalized))
    except (TypeError, ValueError, AttributeError) as exc:
        raise WorkflowExecutionError(
            "source_list_membership_invalid_mailing_id",
            "O campo mailing_id deve conter uma lista válida.",
        ) from exc


def _source_list_membership_output_var(value: Any) -> str:
    normalized = str(_catalog_parameter_scalar(value) or "source_list_membership").strip()
    if (
        not normalized
        or len(normalized) > 128
        or SOURCE_LIST_MEMBERSHIP_OUTPUT_VAR_RE.fullmatch(normalized) is None
    ):
        raise WorkflowExecutionError(
            "source_list_membership_invalid_output_var",
            "O campo output_var deve conter um nome de variável válido.",
        )
    return normalized


def _store_source_list_membership_output(
    *,
    runtime_variables: dict[str, Any],
    output_var: str,
    output: dict[str, Any],
    component_ref_id: str | None,
) -> None:
    variables = _ensure_variables(runtime_variables)
    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs
    customs[output_var] = copy.deepcopy(output)
    runtime_variables["source_list_membership_last_result"] = {
        "component_ref_id": component_ref_id,
        "output_var": output_var,
        "result": copy.deepcopy(output),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _run_source_list_membership(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str:
    params = _source_list_membership_parameters(component)
    output_var = _source_list_membership_output_var(params.get("output_var"))
    mailing_public_id = _source_list_membership_mailing_public_id(params.get("mailing_id"))
    variables = _ensure_variables(runtime_variables)
    resolution_scope = _build_runtime_resolution_scope(
        runtime_variables=runtime_variables,
        variables=variables,
    )
    person_uuid = _source_list_membership_person_uuid(
        params.get("person_uuid"),
        resolution_scope=resolution_scope,
    )
    component_ref_id = str(component.get("ref_id") or component.get("uuid") or "").strip() or None
    branch = "not_found"
    missing: str | None = None
    source_list: dict[str, Any] | None = None
    membership: dict[str, Any] | None = None

    logger.info(
        "workflow m2 source list membership started",
        extra={
            "event": "orch.workflow.m2.source_list_membership.started",
            "flow_uuid": flow_uuid,
            "component_ref_id": component_ref_id,
            "person_uuid": person_uuid,
            "mailing_id": mailing_public_id,
        },
    )

    try:
        async with db_session.begin_nested():
            if person_uuid is None:
                missing = "person"
            else:
                person = await fetch_person_by_uuid_for_update(
                    db_session,
                    person_uuid=person_uuid,
                )
                if person is None:
                    missing = "person"
                else:
                    source_list = await resolve_source_list_by_public_id(
                        db_session,
                        public_id=mailing_public_id,
                    )
                    if source_list is None:
                        missing = "mailing"
                    else:
                        source_status = str(source_list.get("status") or "").strip().upper()
                        if source_status not in {"READY_TO_INGEST", "PROCESSED"}:
                            raise WorkflowExecutionError(
                                "source_list_membership_mailing_not_ready",
                                "A lista selecionada precisa estar pronta ou processada.",
                            )
                        if not str(person.get("identifier") or "").strip():
                            raise WorkflowExecutionError(
                                "source_list_membership_person_without_identifier",
                                "A pessoa selecionada não possui identificador para entrar na lista.",
                            )
                        membership = await ensure_person_in_source_list(
                            db_session,
                            source_list_id=int(source_list["id"]),
                            person=person,
                        )
                        branch = "linked" if membership.get("created") else "already_linked"
    except WorkflowExecutionError:
        raise
    except Exception as exc:
        raise WorkflowExecutionError(
            "source_list_membership_persistence_failed",
            "Falha ao vincular a pessoa à lista.",
        ) from exc

    output = {
        "action": branch,
        "person_uuid": person_uuid,
        "mailing_id": mailing_public_id,
        "source_list_id": source_list.get("id") if source_list is not None else None,
        "contact_draft_id": membership.get("contact_draft_id") if membership is not None else None,
        "channels": membership.get("channels") if membership is not None else None,
        "missing": missing,
    }
    _store_source_list_membership_output(
        runtime_variables=runtime_variables,
        output_var=output_var,
        output=output,
        component_ref_id=component_ref_id,
    )
    runtime_variables.pop("source_list_membership_last_error", None)
    logger.info(
        "workflow m2 source list membership completed",
        extra={
            "event": "orch.workflow.m2.source_list_membership.completed",
            "flow_uuid": flow_uuid,
            "component_ref_id": component_ref_id,
            "person_uuid": person_uuid,
            "mailing_id": mailing_public_id,
            "result_action": branch,
            "missing": missing,
        },
    )
    return branch


def _build_generate_file_resolution_scope(
    *,
    runtime_variables: dict[str, Any],
    variables: dict[str, Any],
) -> dict[str, Any]:
    return _build_runtime_resolution_scope(
        runtime_variables=runtime_variables,
        variables=variables,
    )


def _serialize_generate_file_rows(
    *,
    rows: list[dict[str, Any]],
    format_type: str,
    delimiter: str,
    include_header: bool,
    line_break: str,
) -> tuple[str, int]:
    if format_type == "csv":
        field_names = list(rows[0].keys()) if rows else []
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=field_names, delimiter=delimiter, lineterminator=line_break)
        if include_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if v is None else str(v) for k, v in row.items()})
        text_value = buffer.getvalue()
        return text_value, len(text_value.splitlines()) if text_value else 0

    if format_type == "json":
        text_value = json.dumps(rows, ensure_ascii=False)
        return text_value, len(text_value.splitlines()) if text_value else 0

    if format_type == "jsonl":
        lines = [json.dumps(row, ensure_ascii=False) for row in rows]
        text_value = line_break.join(lines)
        if text_value:
            text_value += line_break
        return text_value, len(text_value.splitlines()) if text_value else 0

    if format_type == "txt":
        lines: list[str] = []
        for row in rows:
            values = ["" if value is None else str(value) for value in row.values()]
            lines.append(delimiter.join(values))
        text_value = line_break.join(lines)
        if text_value:
            text_value += line_break
        return text_value, len(text_value.splitlines()) if text_value else 0

    raise WorkflowExecutionError("generate_file_invalid_format", "Formato de arquivo não suportado.")


def _gzip_if_needed(*, payload: bytes, compression: str, file_name: str) -> tuple[bytes, str]:
    if compression == "none":
        return payload, file_name
    if compression == "gzip":
        output_name = file_name if file_name.endswith(".gz") else f"{file_name}.gz"
        return gzip.compress(payload), output_name
    raise WorkflowExecutionError("generate_file_invalid_compression", "Compressão não suportada.")


def _sftp_upload_bytes(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    destination_path: str,
    file_name: str,
    write_mode: str,
    payload: bytes,
    encoding: str,
    line_break: str,
) -> dict[str, Any]:
    def _permission_like_error(exc: Exception) -> bool:
        message = str(exc or "").lower()
        return (
            "permission denied" in message
            or "permissionerror" in message
            or "errno 13" in message
            or "access denied" in message
        )

    def _suffix_name(original_name: str, sequence: int) -> str:
        suffix_token = f"_{int(sequence):04d}"
        if "." not in original_name:
            return f"{original_name}{suffix_token}"
        stem, ext = original_name.rsplit(".", 1)
        return f"{stem}{suffix_token}.{ext}"

    try:
        import paramiko
    except Exception as exc:
        raise WorkflowExecutionError(
            "generate_file_missing_dependency",
            "Dependência paramiko ausente para upload SFTP.",
        ) from exc

    transport = paramiko.Transport((host, int(port)))
    try:
        transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            remote_dir = _safe_relpath(destination_path)
            if remote_dir:
                current = ""
                for chunk in [part for part in remote_dir.split("/") if part]:
                    current = f"{current}/{chunk}" if current else f"/{chunk}"
                    try:
                        sftp.stat(current)
                    except Exception:
                        sftp.mkdir(current)
            base = f"/{remote_dir}" if remote_dir else ""
            remote_file = f"{base}/{file_name}" if base else f"/{file_name}"

            existing_names: set[str] = set()
            if base:
                try:
                    existing_names = set(sftp.listdir(base))
                except Exception:
                    existing_names = set()

            exists_before = file_name in existing_names

            target_name = file_name
            if write_mode in {"create", "create_per_session"} and exists_before and target_name == file_name:
                raise WorkflowExecutionError("generate_file_create_exists", "Arquivo já existe para write_mode=create.")

            try:
                if write_mode == "append":
                    if exists_before and target_name == file_name:
                        with sftp.open(remote_file, "rb") as remote_reader:
                            previous = remote_reader.read()
                        previous_text = previous.decode(encoding, errors="ignore")
                        append_text = payload.decode(encoding, errors="ignore")
                        if previous_text and append_text and not previous_text.endswith(line_break):
                            append_text = f"{line_break}{append_text}"
                        payload = append_text.encode(encoding)
                        with sftp.open(remote_file, "ab") as remote_writer:
                            remote_writer.write(payload)
                    else:
                        with sftp.open(remote_file, "wb") as remote_writer:
                            remote_writer.write(payload)
                else:
                    with sftp.open(remote_file, "wb") as remote_writer:
                        remote_writer.write(payload)
            except Exception as exc:
                if write_mode not in {"overwrite", "append"} or not _permission_like_error(exc):
                    raise

                refreshed_names = set(existing_names)
                for sequence in range(1, 10_000):
                    candidate_name = _suffix_name(file_name, sequence)
                    if candidate_name in refreshed_names:
                        continue
                    candidate_remote_file = f"{base}/{candidate_name}" if base else f"/{candidate_name}"
                    try:
                        with sftp.open(candidate_remote_file, "wb") as remote_writer:
                            remote_writer.write(payload)
                        target_name = candidate_name
                        remote_file = candidate_remote_file
                        break
                    except Exception as retry_exc:
                        if _permission_like_error(retry_exc):
                            continue
                        raise
                else:
                    raise WorkflowExecutionError(
                        "generate_file_permission_denied",
                        "Sem permissão para gravar arquivo no destino.",
                    ) from exc

            return {
                "file_name": target_name,
                "remote_path": remote_file,
            }
        finally:
            sftp.close()
    finally:
        transport.close()


async def _run_generate_file(
    *,
    db_session: AsyncSession,
    flow_uuid: str,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
    session_id: int,
) -> str:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    variables = _ensure_variables(runtime_variables)
    resolution_scope = _build_generate_file_resolution_scope(
        runtime_variables=runtime_variables,
        variables=variables,
    )

    raw_mapping = params.get("fields_mapping") or params.get("mapping") or []
    mapping_fields = _normalize_generate_file_mapping(raw_mapping)
    if not mapping_fields:
        raise WorkflowExecutionError("generate_file_missing_mapping", "Nenhum campo de mapeamento configurado.")

    destination_type = str(_unwrap_option(params.get("destination_type") or "sftp")).strip().lower()
    if destination_type not in {"local", "sftp"}:
        raise WorkflowExecutionError("generate_file_invalid_destination", "destination_type deve ser local ou sftp.")

    format_type = str(_unwrap_option(params.get("format_type") or params.get("file_type") or "csv")).strip().lower()
    if format_type not in {"csv", "json", "jsonl", "txt"}:
        raise WorkflowExecutionError("generate_file_invalid_format", "Formato de arquivo não suportado.")

    encoding = str(_unwrap_option(params.get("encoding") or "utf-8")).strip().lower()
    if encoding in {"utf8", "utf-8"}:
        encoding = "utf-8"
    elif encoding in {"latin1", "iso-8859-1"}:
        encoding = "iso-8859-1"
    elif encoding in {"windows-1252", "cp1252"}:
        encoding = "cp1252"
    elif encoding in {"utf-8 bom", "utf8 bom", "utf-8-sig"}:
        encoding = "utf-8-sig"
    else:
        raise WorkflowExecutionError("generate_file_invalid_encoding", "Codificação não suportada.")

    write_mode = str(_unwrap_option(params.get("write_mode") or "create")).strip().lower()
    if write_mode not in {"create", "overwrite", "append", "create_per_session"}:
        raise WorkflowExecutionError("generate_file_invalid_write_mode", "write_mode inválido.")

    scheduling_mode = str(_unwrap_option(params.get("scheduling_run_mode") or "imediato")).strip().lower()
    if scheduling_mode == "imediato":
        write_mode = "create_per_session"

    include_header = _coerce_bool(params.get("include_header"), default=True)
    delimiter = _normalize_delimiter(params.get("delimiter") or params.get("separator"))
    line_break = _normalize_line_break(params.get("line_break"))
    compression = str(_unwrap_option(params.get("compression") or "none")).strip().lower()
    if compression in {"", "none", "nenhuma"}:
        compression = "none"
    elif compression in {"gzip", "gz"}:
        compression = "gzip"
    else:
        raise WorkflowExecutionError("generate_file_invalid_compression", "Compressão não suportada.")

    file_name_raw = _render_value(params.get("file_name_template") or params.get("file_name") or "", resolution_scope)
    file_name = _safe_filename("" if file_name_raw is None else str(file_name_raw))
    file_name = _ensure_file_extension(file_name, format_type)
    if write_mode == "create_per_session":
        file_name = _append_session_suffix(file_name, session_id)

    destination_path = _safe_relpath(
        ""
        if _render_value(params.get("destination_path") or "", resolution_scope) is None
        else str(_render_value(params.get("destination_path") or "", resolution_scope))
    )

    row: dict[str, Any] = {}
    for field in mapping_fields:
        raw_value = _resolve_generate_file_source(field["source"], resolution_scope)
        row[field["column"]] = _coerce_generate_file_value(raw_value, field["data_type"])

    file_text, lines_written = _serialize_generate_file_rows(
        rows=[row],
        format_type=format_type,
        delimiter=delimiter,
        include_header=include_header,
        line_break=line_break,
    )
    host = str(_render_value(params.get("host"), resolution_scope) or "").strip() or None
    port = _coerce_int(_render_value(params.get("port"), resolution_scope), 22)
    username = str(_render_value(params.get("user") or params.get("username"), resolution_scope) or "").strip() or None
    password = _resolve_secret_reference(_render_value(params.get("password"), resolution_scope))
    if destination_type == "sftp" and (not host or not username or not password):
        raise WorkflowExecutionError("generate_file_missing_sftp_credentials", "Credenciais SFTP incompletas.")

    output_name = file_name if compression == "none" else (file_name if file_name.endswith(".gz") else f"{file_name}.gz")
    workspace_uuid = get_current_workspace_uuid()
    component_ref_id = str(component.get("ref_id") or component.get("uuid") or output_name)

    config = {
        "destination_type": destination_type,
        "destination_path": destination_path,
        "file_name": output_name,
        "sftp_host": host,
        "sftp_port": port,
        "sftp_user": username,
        "sftp_password": password,
        "format_type": format_type,
        "encoding": encoding,
        "write_mode": write_mode,
        "include_header": include_header,
        "delimiter": delimiter,
        "line_break": line_break,
        "compression": compression,
        "scheduling_run_mode": scheduling_mode,
        "scheduling_date": _unwrap_option(params.get("scheduling_date")),
        "scheduling_time_agendado": _unwrap_option(params.get("scheduling_time_agendado")),
        "scheduling_fuso_agandado": _unwrap_option(params.get("scheduling_fuso_agandado")) or "sp_utc_3",
        "recurrence": _unwrap_option(params.get("recurrence")),
        "scheduling_fuso_recorrente": _unwrap_option(params.get("scheduling_fuso_recorrente")) or "sp_utc_3",
        "scheduling_time": _unwrap_option(params.get("scheduling_time")),
    }
    enqueue_result = await upsert_job_and_buffer_row(
        db_session,
        workspace_uuid=workspace_uuid,
        flow_id=flow_uuid,
        component_ref_id=component_ref_id,
        session_id=session_id,
        config=config,
        row_payload=row,
    )

    if scheduling_mode == "imediato":
        try:
            from app.tasks.generate_file_tasks import generate_file_run_task

            generate_file_run_task.delay(workspace_uuid=workspace_uuid, job_id=str(enqueue_result["job_id"]))
        except Exception as exc:
            raise WorkflowExecutionError(
                "generate_file_dispatch_enqueue_failed",
                f"Falha ao enfileirar generate_file.run: {exc}",
            ) from exc

    result_payload: dict[str, Any] = {
        "status": "queued",
        "destination_type": destination_type,
        "format_type": format_type,
        "write_mode": write_mode,
        "lines_written": lines_written,
        "queued_row": bool(enqueue_result.get("queued_row")),
        "job_id": enqueue_result.get("job_id"),
        "next_run_at": enqueue_result.get("next_run_at"),
        "mode": enqueue_result.get("mode"),
        "remote_path": None,
        "file_name": output_name,
        "size_bytes": len(file_text.encode(encoding)),
        "md5": None,
    }
    runtime_variables["generate_file_last_result"] = result_payload

    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs
    output_var_prefix = str(params.get("output_var_prefix") or "arquivo").strip() or "arquivo"
    customs[output_var_prefix] = result_payload

    response_cfg = params.get("response") if isinstance(params.get("response"), dict) else {}
    status_var = response_cfg.get("status")
    path_var = response_cfg.get("path")
    file_var = response_cfg.get("file_name")
    md5_var = response_cfg.get("md5")
    error_var = response_cfg.get("error")
    if isinstance(status_var, str) and status_var.strip():
        customs[status_var.strip()] = "queued"
    if isinstance(path_var, str) and path_var.strip():
        customs[path_var.strip()] = result_payload.get("remote_path")
    if isinstance(file_var, str) and file_var.strip():
        customs[file_var.strip()] = result_payload.get("file_name")
    if isinstance(md5_var, str) and md5_var.strip():
        customs[md5_var.strip()] = None
    if isinstance(error_var, str) and error_var.strip():
        customs[error_var.strip()] = None

    return "success"


def _run_code_editor(
    *,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
    branch_labels: list[str],
) -> str | None:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    code = params.get("code")
    if not isinstance(code, str) or not code.strip():
        raise WorkflowExecutionError("code_editor_missing_code", "Componente code_editor sem código válido.")

    variables = _ensure_variables(runtime_variables)
    timeout_ms = _coerce_timeout_ms(params.get("timeout_ms"), default=400)

    normalized_labels = [str(label).strip().lower() for label in branch_labels if str(label).strip()]
    exception_label = next(
        (label for label in normalized_labels if label == "exception" or label.startswith("exception_")),
        None,
    )
    branch_payload = {label: label for label in normalized_labels}
    if exception_label:
        branch_payload.setdefault("exception", exception_label)
        branch_payload.setdefault("failure", exception_label)
        branch_payload.setdefault("error", exception_label)

    node_payload = {
        "code": code,
        "timeoutMs": timeout_ms,
        "variables": variables,
        "branches": branch_payload,
    }

    runner_js = textwrap.dedent(
        """
        const fs = require('node:fs');
        const vm = require('node:vm');

        function normalizeCode(src) {
          return String(src || '')
            .replace(/export\\s+default\\s+async\\s+function\\s+main/g, 'async function main')
            .replace(/export\\s+default\\s+function\\s+main/g, 'function main');
        }

        async function run() {
          const inputRaw = fs.readFileSync(0, 'utf8');
          const input = JSON.parse(inputRaw);
          const code = normalizeCode(input.code);
          const sandbox = {
            console: { log: () => {}, error: () => {}, warn: () => {} },
            ctx: {
              variables: input.variables || {},
              branches: input.branches || {}
            }
          };

          vm.createContext(sandbox);
          const wrapped = `${code}\\n;globalThis.__orch_main=(typeof main==='function'?main:null);`;
          vm.runInContext(wrapped, sandbox, { timeout: input.timeoutMs || 400 });
          if (typeof sandbox.__orch_main !== 'function') {
            throw new Error('main_not_found');
          }

          const result = await Promise.resolve(sandbox.__orch_main(sandbox.ctx));
          process.stdout.write(JSON.stringify({ result, ctx: sandbox.ctx }));
        }

        run().catch((err) => {
          process.stderr.write(String(err && err.stack ? err.stack : err));
          process.exit(1);
        });
        """
    )

    completed = subprocess.run(
        ["node", "-e", runner_js],
        input=json.dumps(node_payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=(timeout_ms / 1000.0) + 0.5,
        check=False,
    )

    if completed.returncode != 0:
        raise WorkflowExecutionError("code_editor_runtime_error", completed.stderr.strip() or "Falha na execução JS.")

    try:
        parsed = json.loads(completed.stdout or "{}")
    except Exception as exc:
        raise WorkflowExecutionError("code_editor_invalid_output", "Saída inválida do code_editor.") from exc

    ctx = parsed.get("ctx") if isinstance(parsed.get("ctx"), dict) else {}
    result = parsed.get("result")

    ctx_variables = ctx.get("variables")
    if isinstance(ctx_variables, dict):
        runtime_variables["variables"] = ctx_variables

    runtime_variables["code_editor_last_result"] = result

    if isinstance(result, dict) and result.get("payload") is not None:
        runtime_variables["code_editor_last_payload"] = result.get("payload")

    branch = None
    if isinstance(result, dict):
        branch = result.get("branch") or result.get("next_branch")
    elif isinstance(result, str):
        branch = result
    elif isinstance(result, bool):
        branch = "true" if result else "false"

    if branch is None:
        return None
    text_branch = str(branch).strip().lower()
    return text_branch or None


def _resolve_code_editor_branch(
    *,
    branch_label: str | None,
    branch_labels: list[str],
    runtime_variables: dict[str, Any],
    execution_error: WorkflowExecutionError | None = None,
) -> str | None:
    normalized_labels = [str(label).strip().lower() for label in branch_labels if str(label).strip()]
    exception_label = next(
        (label for label in normalized_labels if label == "exception" or label.startswith("exception_")),
        None,
    )

    def _set_last_error(code: str, message: str, details: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "code": code,
            "message": message,
        }
        if isinstance(details, dict) and details:
            payload["details"] = details
        runtime_variables["code_editor_last_error"] = payload

    if execution_error is not None:
        _set_last_error(
            execution_error.code,
            execution_error.message,
            {"available_branches": normalized_labels},
        )
        if exception_label:
            return exception_label
        raise execution_error

    if branch_label is None:
        return None

    normalized_branch = str(branch_label).strip().lower()
    if normalized_branch in {"exception", "failure", "error"} and exception_label:
        return exception_label
    if normalized_branch in normalized_labels:
        return normalized_branch

    message = f"Branch '{normalized_branch}' retornado pelo code_editor não está mapeado no fluxo."
    _set_last_error(
        "code_editor_branch_not_mapped",
        message,
        {"returned_branch": normalized_branch, "available_branches": normalized_labels},
    )
    if exception_label:
        return exception_label
    raise WorkflowExecutionError("code_editor_branch_not_mapped", message)


def _parse_headers(raw_headers: Any, variables: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    if isinstance(raw_headers, dict):
        for key, value in raw_headers.items():
            if key is None:
                continue
            rendered = _render_value(value, variables)
            headers[str(key)] = "" if rendered is None else str(rendered)
        return headers
    if isinstance(raw_headers, list):
        for item in raw_headers:
            if not isinstance(item, dict):
                continue
            if item.get("enabled") is False:
                continue
            key = item.get("key") or item.get("name")
            if key is None or not str(key).strip():
                continue
            value = _render_value(item.get("value"), variables)
            headers[str(key).strip()] = "" if value is None else str(value)
    return headers


def _parse_query(raw_query: Any, variables: dict[str, Any]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    if not isinstance(raw_query, list):
        return items
    for entry in raw_query:
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            continue
        key = entry.get("key") or entry.get("name")
        if key is None or not str(key).strip():
            continue
        value = _render_value(entry.get("value"), variables)
        items.append((str(key).strip(), "" if value is None else str(value)))
    return items


def _resolve_body(request_config: dict[str, Any], variables: dict[str, Any]) -> tuple[bytes | None, str | None]:
    body = request_config.get("body") if isinstance(request_config.get("body"), dict) else {}
    mode = str(body.get("mode") or "json").strip().lower()
    if mode == "json":
        raw_json = body.get("json")
        rendered = _render_value(raw_json, variables)
        if rendered is None:
            return b"null", "application/json"
        if isinstance(rendered, (dict, list, int, float, bool)):
            return json.dumps(rendered, ensure_ascii=False).encode("utf-8"), "application/json"
        text_value = str(rendered).strip()
        if not text_value:
            return b"{}", "application/json"
        try:
            parsed = json.loads(text_value)
            return json.dumps(parsed, ensure_ascii=False).encode("utf-8"), "application/json"
        except Exception:
            return json.dumps({"value": text_value}, ensure_ascii=False).encode("utf-8"), "application/json"

    if mode == "text":
        rendered = _render_value(body.get("text"), variables)
        return ("" if rendered is None else str(rendered)).encode("utf-8"), "text/plain"

    if mode in {"form", "x-www-form-urlencoded", "urlencoded"}:
        form_entries = body.get("form") if isinstance(body.get("form"), list) else []
        data: list[tuple[str, str]] = []
        for entry in form_entries:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key") or entry.get("name")
            if key is None or not str(key).strip():
                continue
            value = _render_value(entry.get("value"), variables)
            data.append((str(key).strip(), "" if value is None else str(value)))
        return parse.urlencode(data).encode("utf-8"), "application/x-www-form-urlencoded"

    if mode == "raw":
        raw_value = body.get("json")
        content_type = "application/json"
        if raw_value is None:
            raw_value = body.get("text")
            content_type = "text/plain"
        rendered = _render_value(raw_value, variables)
        if rendered is None:
            return b"", content_type
        if isinstance(rendered, (dict, list, int, float, bool)):
            return json.dumps(rendered, ensure_ascii=False).encode("utf-8"), "application/json"
        return str(rendered).encode("utf-8"), content_type

    return None, None


def _http_execute(req: request.Request, timeout_seconds: float) -> tuple[int, dict[str, str], str, str | None]:
    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), dict(resp.headers.items()), body, None
    except HTTPError as err:
        body = err.read().decode("utf-8", errors="replace") if hasattr(err, "read") else str(err)
        return int(getattr(err, "code", 500) or 500), dict(getattr(err, "headers", {}).items()), body, str(err)
    except URLError as err:
        return 599, {}, "", str(err)


def _run_api_call(
    *,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    request_cfg = params.get("request") if isinstance(params.get("request"), dict) else {}
    variables = _ensure_variables(runtime_variables)

    method = str(request_cfg.get("method") or "GET").strip().upper()
    raw_url = _render_value(request_cfg.get("url"), variables)
    url = "" if raw_url is None else str(raw_url).strip()
    if not url:
        raise WorkflowExecutionError("api_call_missing_url", "api_call sem URL válida.")

    query_items = _parse_query(request_cfg.get("query"), variables)
    if query_items:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{parse.urlencode(query_items)}"

    body_bytes, content_type = _resolve_body(request_cfg, variables)
    headers = _parse_headers(request_cfg.get("headers"), variables)
    if content_type and "Content-Type" not in {k.title(): v for k, v in headers.items()}:
        headers.setdefault("Content-Type", content_type)

    timeout_ms = _coerce_timeout_ms(request_cfg.get("timeout"), default=30_000)
    retry_cfg = request_cfg.get("retry") if isinstance(request_cfg.get("retry"), dict) else {}
    max_attempts = _read_int([retry_cfg.get("max_attempts"), retry_cfg.get("attempts"), retry_cfg.get("max")]) or 1
    max_attempts = max(1, min(max_attempts, 5))
    backoff_ms = _read_int([retry_cfg.get("backoff_ms"), retry_cfg.get("delay_ms"), retry_cfg.get("wait_ms")]) or 0
    backoff_ms = max(0, min(backoff_ms, 5_000))

    retry_on_statuses = retry_cfg.get("on_statuses")
    retry_status_set: set[int] = set()
    if isinstance(retry_on_statuses, list):
        for status_code in retry_on_statuses:
            try:
                retry_status_set.add(int(status_code))
            except Exception:
                continue
    if not retry_status_set:
        retry_status_set = {408, 425, 429, 500, 502, 503, 504, 599}

    attempt = 0
    status_code = 599
    resp_headers: dict[str, str] = {}
    resp_body = ""
    error_msg: str | None = None

    while attempt < max_attempts:
        attempt += 1
        req = request.Request(url=url, method=method, data=body_bytes, headers=headers)
        status_code, resp_headers, resp_body, error_msg = _http_execute(req, timeout_seconds=timeout_ms / 1000.0)
        if attempt >= max_attempts:
            break
        should_retry = (status_code in retry_status_set) or (status_code == 599) or (error_msg is not None)
        if not should_retry:
            break
        if backoff_ms > 0:
            time.sleep(backoff_ms / 1000.0)

    response_map = request_cfg.get("response") if isinstance(request_cfg.get("response"), dict) else {}
    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs

    status_var = response_map.get("status")
    body_var = response_map.get("body")
    headers_var = response_map.get("headers")
    error_var = response_map.get("error")
    parsed_body: Any = None
    if resp_body:
        try:
            parsed_body = json.loads(resp_body)
        except Exception:
            parsed_body = resp_body

    if isinstance(status_var, str) and status_var.strip():
        target = status_var.strip()
        customs[target] = status_code
        _set_by_path(variables, target, status_code)
    if isinstance(body_var, str) and body_var.strip():
        target = body_var.strip()
        customs[target] = parsed_body
        _set_by_path(variables, target, parsed_body)
    if isinstance(headers_var, str) and headers_var.strip():
        target = headers_var.strip()
        customs[target] = resp_headers
        _set_by_path(variables, target, resp_headers)
    if isinstance(error_var, str) and error_var.strip():
        target = error_var.strip()
        customs[target] = error_msg
        _set_by_path(variables, target, error_msg)

    runtime_variables["api_call_last_result"] = {
        "status_code": status_code,
        "url": url,
        "error": error_msg,
        "attempts": attempt,
        "max_attempts": max_attempts,
        "body": parsed_body,
        "headers": resp_headers,
    }

    return "success" if 200 <= status_code < 300 else "error"


def _finish_flow_json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _finish_flow_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_finish_flow_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _finish_flow_contact_payload(contact_state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(contact_state, dict):
        return None
    return {
        "id": contact_state.get("contact_list_member_id"),
        "contact_list_id": contact_state.get("contact_list_id"),
        "mailing_id": contact_state.get("mailing_id"),
        "identifier": contact_state.get("contact_identifier"),
        "name": contact_state.get("contact_name"),
        "full_name": contact_state.get("contact_full_name"),
        "gender": contact_state.get("contact_gender"),
        "country": contact_state.get("contact_country"),
        "province": contact_state.get("contact_province"),
        "city": contact_state.get("contact_city"),
        "birth_date": contact_state.get("contact_birth_date"),
        "age": contact_state.get("contact_age"),
        "person_uuid": contact_state.get("person_uuid"),
        "channel": {
            "type": contact_state.get("contact_channel_type"),
            "label": contact_state.get("contact_channel_label"),
            "address": contact_state.get("contact_channel_address"),
        },
        "extra": _normalize_contact_extra_data(contact_state.get("contact_channel_extra_data")),
    }


def _finish_flow_requires_dialer_cdr(
    *,
    runtime_variables: dict[str, Any],
    cdr_event: dict[str, Any] | None,
) -> bool:
    if isinstance(cdr_event, dict):
        return True
    if str(runtime_variables.get("source_app") or "").strip() == "DialerApp":
        return True
    routing = runtime_variables.get("send_with_dialer_routing")
    assignment = routing.get("assignment") if isinstance(routing, dict) else None
    if not isinstance(assignment, dict):
        return False
    return (
        str(assignment.get("mode") or "").strip().lower() == "dialer"
        or str(assignment.get("linked_actuator") or "").strip().lower() == "dialer"
    )


def _finish_flow_dialer_event_id(runtime_variables: dict[str, Any]) -> str | None:
    payload = runtime_variables.get("last_payload")
    if not isinstance(payload, dict):
        return None
    hangup = payload.get("hangup") if isinstance(payload.get("hangup"), dict) else {}
    makecall = payload.get("makecall") if isinstance(payload.get("makecall"), dict) else {}
    event_id = (
        str(payload.get("uniqueid") or "").strip()
        or str(hangup.get("Uniqueid") or "").strip()
        or str(hangup.get("Linkedid") or "").strip()
        or str(makecall.get("DestUniqueid") or "").strip()
    )
    return event_id or None


async def _dispatch_finish_flow_webhook(
    *,
    component: dict[str, Any],
    session_state: dict[str, Any],
    runtime_variables: dict[str, Any],
    finished_at: datetime,
    contact_state: dict[str, Any] | None = None,
    cdr: dict[str, Any] | None = None,
    cdr_required: bool = False,
    cdr_event_id: int | str | None = None,
    cdr_event_row_id: int | None = None,
    cdr_event_dispatched: bool = False,
) -> dict[str, Any] | None:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    webhook = params.get("webhook")
    if not isinstance(webhook, str) or not webhook.strip():
        return None

    if cdr_event_dispatched:
        runtime_variables.pop("cdr", None)
        return {
            "success": True,
            "skipped": True,
            "channel_event_identity": cdr_event_id,
            "channel_event_row_id": cdr_event_row_id,
            "reason": FINISH_FLOW_WEBHOOK_DISPATCHED_REASON,
        }

    previous_result = runtime_variables.get("finish_flow_webhook")
    previous_event_identity = (
        previous_result.get("channel_event_identity") if isinstance(previous_result, dict) else None
    )
    if (
        isinstance(previous_result, dict)
        and previous_result.get("success") is True
        and (
            cdr_event_id is None
            or previous_event_identity is None
            or str(previous_event_identity) == str(cdr_event_id)
        )
    ):
        runtime_variables.pop("cdr", None)
        return {**copy.deepcopy(previous_result), "skipped": True}

    resolved_cdr = copy.deepcopy(cdr) if isinstance(cdr, dict) else None
    if cdr_required and not isinstance(resolved_cdr, dict):
        result = {
            "success": False,
            "status_code": None,
            "error": "dialer_cdr_not_available",
            "url": webhook.strip(),
            "dispatched_at": None,
            "deferred": True,
        }
        runtime_variables["finish_flow_webhook"] = result
        return result

    session_payload = {
        key: copy.deepcopy(value)
        for key, value in session_state.items()
        if key != "runtime_variables"
    }
    session_payload.update(
        {
            "state": 3,
            "ended_at": finished_at,
            "last_card_uuid": component.get("uuid") or component.get("ref_id"),
            "next_card_uuid": None,
            "result": params.get("result"),
            "contact": _finish_flow_contact_payload(contact_state),
        }
    )
    payload = _finish_flow_json_safe(
        {
            "session": session_payload,
            "cdr": resolved_cdr,
        }
    )
    try:
        headers = {"Content-Type": "application/json"}
        session_uuid = str(session_state.get("uuid") or session_state.get("id") or "unknown")
        finish_identity = str(cdr_event_id or component.get("uuid") or component.get("ref_id") or "finish")
        headers["Idempotency-Key"] = f"orch-finish-flow:{session_uuid}:{finish_identity}"
        req = request.Request(
            url=webhook.strip(),
            method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        )
        status_code, _headers, _body, error = await asyncio.to_thread(
            _http_execute,
            req,
            timeout_seconds=5.0,
        )
    except Exception as exc:
        status_code, error = 599, str(exc)

    success = 200 <= status_code < 300
    if success:
        runtime_variables.pop("cdr", None)
    result = {
        "success": success,
        "status_code": status_code,
        "error": error,
        "url": webhook.strip(),
        "dispatched_at": datetime.now(timezone.utc).isoformat(),
        "channel_event_identity": cdr_event_id,
        "channel_event_row_id": cdr_event_row_id,
    }
    runtime_variables["finish_flow_webhook"] = result
    return result


def _resolve_send_whatsapp_interactive_branch_label(
    *,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str | None:
    candidates = _resolve_send_whatsapp_interactive_branch_labels(
        component=component,
        runtime_variables=runtime_variables,
    )
    return candidates[0] if candidates else None


def _resolve_send_whatsapp_interactive_branch_labels(
    *,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> list[str]:
    key = _extract_whatsapp_message_branch_key_from_runtime(runtime_variables)
    if key is None:
        key = _extract_whatsapp_status_from_runtime(runtime_variables)
    if key is None:
        return []

    key_token = str(key).strip()
    if not key_token:
        return []
    provider_number = _extract_whatsapp_provider_number_from_payload(runtime_variables.get("last_payload"))
    if provider_number is None:
        routing_meta = runtime_variables.get("send_whatsapp_interactive_routing")
        if isinstance(routing_meta, dict):
            assignment = routing_meta.get("assignment")
            if isinstance(assignment, dict):
                provider_number = str(normalize_phone_to_canonical_ani(assignment.get("ani")) or "").strip() or None
    if provider_number is None:
        provider_number = _read_send_whatsapp_interactive_selected_number(component)

    labels: list[str] = []
    if provider_number is not None:
        labels.append(f"wic:{provider_number}:{key_token}")
    labels.append(key_token)
    return list(dict.fromkeys(labels))


def _unwrap_option(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("id", "value", "name", "label"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None
    if isinstance(value, list) and value:
        return _unwrap_option(value[0])
    if isinstance(value, str):
        token = value.strip()
        return token or None
    return None


def _parse_intelligent_agent_exit_function(raw: Any) -> tuple[str | None, dict[str, Any] | None]:
    if isinstance(raw, dict):
        output_var_name = raw.get("output_var_name")
        output_var = str(output_var_name).strip() if output_var_name is not None else None
        schema = raw.get("json")
        if isinstance(schema, dict):
            return output_var or None, dict(schema)
        return output_var or None, None
    if isinstance(raw, str):
        candidate = raw.strip()
        if not candidate:
            return None, None
        try:
            parsed = json.loads(candidate)
        except Exception:
            return None, None
        if isinstance(parsed, dict):
            return None, parsed
    return None, None


def _map_ai_output_to_schema(*, parsed: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict) or not schema:
        return dict(parsed)

    mapped: dict[str, Any] = {}
    for key in schema.keys():
        if key in parsed:
            mapped[key] = parsed.get(key)

    if mapped:
        return mapped

    data = parsed.get("dados_extraidos")
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            key = str(item.get("chave") or "").strip()
            if key in schema:
                mapped[key] = item.get("valor")
    if mapped:
        return mapped

    if len(schema.keys()) == 1:
        only_key = next(iter(schema.keys()))
        text_value = (
            parsed.get("value")
            or parsed.get("resultado")
            or parsed.get("answer")
            or parsed.get("output")
            or parsed.get("text")
        )
        if text_value is not None:
            return {only_key: text_value}
    return {}


async def _run_intelligent_agent(
    *,
    db_session: AsyncSession,
    component: dict[str, Any],
    runtime_variables: dict[str, Any],
) -> str | None:
    params = component.get("parameters") if isinstance(component.get("parameters"), dict) else {}
    variables = _ensure_variables(runtime_variables)
    resolution_scope = _build_runtime_resolution_scope(
        runtime_variables=runtime_variables,
        variables=variables,
    )

    prompt_rendered = _render_value(params.get("user_prompt"), resolution_scope)
    user_prompt = "" if prompt_rendered is None else str(prompt_rendered).strip()
    if not user_prompt:
        raise WorkflowExecutionError("intelligent_agent_missing_prompt", "Componente intelligent_agent sem user_prompt.")

    output_var_name, output_schema = _parse_intelligent_agent_exit_function(params.get("exit_function"))
    selected_model = _unwrap_option(params.get("llm")) or "gpt-5"
    schema_text = json.dumps(output_schema, ensure_ascii=False) if isinstance(output_schema, dict) else None

    system_prompt_parts = [
        "Você é um agente inteligente do orquestrador.",
        "Responda APENAS com JSON válido, sem markdown, sem texto fora do JSON.",
    ]
    if schema_text:
        system_prompt_parts.append(
            "O JSON de saída deve respeitar EXATAMENTE este schema (mesmas chaves):\n"
            f"{schema_text}"
        )
    system_prompt = "\n\n".join(system_prompt_parts)

    workspace_uuid: str | None = None
    workspace_api_key: str | None = None
    try:
        workspace_uuid = get_current_workspace_uuid()
    except Exception:
        workspace_uuid = None
    if workspace_uuid:
        workspace_api_key = await fetch_workspace_otima_billing_api_key(
            db_session,
            workspace_uuid=workspace_uuid,
        )

    try:
        llm_result = execute_otima_llm_prompt(
            model=selected_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            workspace_uuid=workspace_uuid,
            workspace_api_key=workspace_api_key,
        )
    except Exception as exc:
        raise WorkflowExecutionError("intelligent_agent_provider_error", f"Falha na Otima LLM: {exc}") from exc

    parsed = llm_result.get("parsed_json")
    if not isinstance(parsed, dict):
        raise WorkflowExecutionError("intelligent_agent_invalid_response", "Resposta da LLM não retornou JSON válido.")

    mapped = _map_ai_output_to_schema(parsed=parsed, schema=output_schema)
    customs = variables.get("customs")
    if not isinstance(customs, dict):
        customs = {}
        variables["customs"] = customs

    intelligent_agent_ref = str(component.get("ref_id") or component.get("id") or "intelligent_agent").strip()
    ia_state = customs.setdefault("intelligent_agent", {})
    if isinstance(ia_state, dict):
        ia_state[intelligent_agent_ref] = {
            "model": selected_model,
            "output_var_name": output_var_name,
            "schema": output_schema,
            "result": mapped if mapped else parsed,
        }

    if output_var_name:
        customs[output_var_name] = mapped if mapped else parsed
        _set_by_path(variables, output_var_name, mapped if mapped else parsed)
    if isinstance(mapped, dict):
        for key, value in mapped.items():
            if isinstance(key, str) and key.strip():
                _set_by_path(variables, key.strip(), value)
                customs[key.strip()] = value

    runtime_variables["intelligent_agent_last_result"] = {
        "model": selected_model,
        "output_var_name": output_var_name,
        "result": mapped if mapped else parsed,
        "raw_text": llm_result.get("raw_text"),
        "endpoint": llm_result.get("endpoint"),
        "status_code": llm_result.get("status_code"),
    }
    return None


async def execute_workflow_m2_for_session(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    session_id: int,
) -> WorkflowExecutionResult:
    settings = get_settings()
    if not _read_enabled(settings):
        return WorkflowExecutionResult(
            enabled=False,
            executed_steps=0,
            stopped_reason="workflow_m2_disabled",
            last_card_uuid=None,
            next_card_uuid=None,
        )

    safe_schema = get_current_workspace_schema().replace('"', '""')
    max_steps = _read_max_steps(settings)
    loop_guard_repeat_threshold = _read_loop_guard_repeat_threshold(settings)
    contextual_member_routing_feature_enabled = _read_contextual_member_routing_enabled(settings)
    execution_started_at = datetime.now(timezone.utc)
    execution_started_perf = time.perf_counter()
    session_uuid_for_metrics: str | None = None
    revision_id_for_metrics: str | None = None
    metrics: list[dict[str, Any]] = []

    def _append_metric(
        *,
        metric_type: str,
        status: str,
        started_at: datetime,
        finished_at: datetime,
        latency_ms: float,
        stopped_reason: str | None,
        step_index: int | None = None,
        card_cursor: str | None = None,
        component_kind_value: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        metrics.append(
            {
                "session_id": session_id,
                "session_uuid": session_uuid_for_metrics,
                "flow_uuid": flow_uuid,
                "revision_id": revision_id_for_metrics,
                "metric_type": metric_type,
                "step_index": step_index,
                "card_uuid": _to_uuid_or_none(card_cursor),
                "card_cursor": card_cursor,
                "component_kind": component_kind_value,
                "status": status,
                "stopped_reason": stopped_reason,
                "latency_ms": round(latency_ms, 2),
                "started_at": started_at,
                "finished_at": finished_at,
                "details": details or {},
            }
        )

    async def _finalize(result: WorkflowExecutionResult) -> WorkflowExecutionResult:
        finished_at = datetime.now(timezone.utc)
        total_latency_ms = (time.perf_counter() - execution_started_perf) * 1000
        success_stop_reasons = {
            "blocked_wait_for_event",
            "finished_by_component",
            "scheduled_wait",
            "end_of_branch",
            "blocked_send_with_whatsapp",
            "blocked_send_whatsapp_interactive",
            "blocked_process_whatsapp_response",
            "blocked_send_with_dialer",
            "blocked_send_with_sms",
            "blocked_process_dialer_response",
            "blocked_switch_bot_flow",
            "blocked_identidade_person_flow_link",
        }
        workflow_status = "success"
        if result.stopped_reason == "session_execution_locked":
            workflow_status = "locked"
        elif result.stopped_reason not in success_stop_reasons:
            workflow_status = "stopped"

        _append_metric(
            metric_type="workflow",
            status=workflow_status,
            started_at=execution_started_at,
            finished_at=finished_at,
            latency_ms=total_latency_ms,
            stopped_reason=result.stopped_reason,
            details={
                "executed_steps": result.executed_steps,
                "last_card_uuid": result.last_card_uuid,
                "next_card_uuid": result.next_card_uuid,
            },
        )
        await persist_session_metrics(db_session, metrics=metrics)
        logger.info(
            "workflow m2 execution metrics",
            extra={
                "event": "orch.workflow.m2.metrics",
                "flow_uuid": flow_uuid,
                "session_id": session_id,
                "session_uuid": session_uuid_for_metrics,
                "metric_type": "workflow",
                "total_latency_ms": round(total_latency_ms, 2),
                "stopped_reason": result.stopped_reason,
            },
        )
        return result

    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        lock_result = await db_session.execute(
            text("SELECT pg_try_advisory_xact_lock(:class_id, :object_id) AS locked"),
            {
                "class_id": 92021,
                "object_id": int(session_id),
            },
        )
        locked = bool(lock_result.scalar_one())
        if not locked:
            return await _finalize(
                WorkflowExecutionResult(
                    enabled=True,
                    executed_steps=0,
                    stopped_reason="session_execution_locked",
                    last_card_uuid=None,
                    next_card_uuid=None,
                )
            )

        flow_row = await fetch_flow_row(db_session, flow_uuid=flow_uuid)
        if flow_row is None:
            return await _finalize(WorkflowExecutionResult(True, 0, "flow_not_found", None, None))

        session_state = await fetch_session_workflow_state(db_session, session_id=session_id)
        if session_state is None:
            return await _finalize(WorkflowExecutionResult(True, 0, "session_not_found", None, None))
        session_uuid_for_metrics = session_state.get("uuid")

        runtime_variables = session_state.get("runtime_variables")
        if not isinstance(runtime_variables, dict):
            runtime_variables = {}
        if _is_terminal_failure_session_state(session_state):
            return await _finalize(
                WorkflowExecutionResult(
                    True,
                    0,
                    "session_already_terminal",
                    session_state.get("last_card_uuid"),
                    None,
                )
            )

        async def _terminalize_revision_failure(
            *,
            failure_reason: str,
            requested_revision_id: str | None,
        ) -> WorkflowExecutionResult:
            failed_at = datetime.now(timezone.utc)
            workflow_meta = _ensure_workflow_meta(runtime_variables)
            workflow_meta["terminal_failure"] = {
                "code": failure_reason,
                "message": "A revisão fixada da sessão não está disponível para execução.",
                "requested_revision_id": requested_revision_id,
                "failed_at": failed_at.isoformat(),
            }
            last_card_uuid = session_state.get("last_card_uuid")
            _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=None)
            await replace_session_workflow_state(
                db_session,
                session_id=session_id,
                runtime_variables=runtime_variables,
                last_card_uuid=_to_uuid_or_none(last_card_uuid),
                next_card_uuid=None,
                ended_at=failed_at,
                state=3,
            )
            logger.error(
                "workflow m2 pinned revision unavailable",
                extra={
                    "event": "orch.workflow.m2.pinned_revision_unavailable",
                    "flow_uuid": flow_uuid,
                    "session_id": session_id,
                    "session_uuid": session_uuid_for_metrics,
                    "requested_revision_id": requested_revision_id,
                    "failure_reason": failure_reason,
                },
            )
            return await _finalize(
                WorkflowExecutionResult(
                    True,
                    0,
                    failure_reason,
                    last_card_uuid,
                    None,
                )
            )

        revision_resolution = await resolve_workflow_revision_for_session(
            db_session,
            flow_id=str(flow_row["id"]),
            runtime_variables=runtime_variables,
        )
        if revision_resolution.failure_reason in {
            "pinned_revision_invalid",
            "pinned_revision_not_found",
        }:
            return await _terminalize_revision_failure(
                failure_reason=revision_resolution.failure_reason,
                requested_revision_id=revision_resolution.requested_revision_id,
            )

        selected_revision = revision_resolution.revision
        if selected_revision is None:
            return await _finalize(WorkflowExecutionResult(True, 0, "revision_not_found", None, None))

        if revision_resolution.used_legacy_fallback:
            revision_patch: dict[str, Any] = {
                "revision_id": str(selected_revision["id"]),
                "revision_mode": str(selected_revision.get("selection_mode") or "selected"),
                "definition_loaded_at": datetime.now(timezone.utc).isoformat(),
                "legacy_revision_pinned_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                revision_patch["revision_version"] = int(selected_revision["version"])
            except (KeyError, TypeError, ValueError):
                pass

            pinned_runtime = await ensure_session_workflow_revision_pin(
                db_session,
                session_id=session_id,
                flow_uuid=flow_uuid,
                revision_patch=revision_patch,
            )
            if pinned_runtime is None:
                return await _finalize(WorkflowExecutionResult(True, 0, "session_not_found", None, None))

            runtime_variables = pinned_runtime
            session_state["runtime_variables"] = runtime_variables
            revision_resolution = await resolve_workflow_revision_for_session(
                db_session,
                flow_id=str(flow_row["id"]),
                runtime_variables=runtime_variables,
            )
            if revision_resolution.failure_reason in {
                "pinned_revision_invalid",
                "pinned_revision_not_found",
            }:
                return await _terminalize_revision_failure(
                    failure_reason=revision_resolution.failure_reason,
                    requested_revision_id=revision_resolution.requested_revision_id,
                )
            selected_revision = revision_resolution.revision
            if selected_revision is None:
                return await _finalize(WorkflowExecutionResult(True, 0, "revision_not_found", None, None))

        revision_id_for_metrics = str(selected_revision["id"])
        definition = selected_revision.get("definition")
        if not isinstance(definition, dict):
            raise WorkflowExecutionError("invalid_definition", "Definição do fluxo inválida para execução M2.")

        session_scope = _read_session_scope(runtime_variables)
        contextual_member_routing_enabled = _contextual_member_routing_enabled_for_scope(
            feature_enabled=contextual_member_routing_feature_enabled,
            session_scope=session_scope,
        )
        contact_member_scope = _extract_contact_member_routing_scope(runtime_variables)
        selected_contact_channel = (
            _active_selected_contact_channel(runtime_variables)
            if session_scope == "person"
            else None
        )
        effective_contact_member_scope = (
            _routing_scope_from_selected_contact_channel(selected_contact_channel)
            if selected_contact_channel is not None
            else contact_member_scope
        )
        person_scope_without_selectors = (
            session_scope == "person" and not contact_member_scope.explicit
        )
        if (
            contextual_member_routing_enabled
            and effective_contact_member_scope.valid
            and not person_scope_without_selectors
        ):
            contact_runtime_context = await fetch_contact_runtime_context_for_session(
                db_session,
                flow_uuid=flow_uuid,
                session_id=session_id,
                contact_list_member_id=effective_contact_member_scope.contact_list_member_id,
                contact_list_id=effective_contact_member_scope.contact_list_id,
                mailing_id=effective_contact_member_scope.mailing_id,
            )
        elif contextual_member_routing_enabled:
            contact_runtime_context = None
        else:
            contact_runtime_context = await fetch_contact_runtime_context_for_session(
                db_session,
                flow_uuid=flow_uuid,
                session_id=session_id,
            )

        resolved_contact_list_member_id: int | None = None
        if contextual_member_routing_enabled and isinstance(contact_runtime_context, dict):
            try:
                resolved_contact_list_member_id = int(contact_runtime_context["contact_list_member_id"])
            except (KeyError, TypeError, ValueError):
                contact_runtime_context = None

        channel_type_matches: bool | None = None
        if (
            contextual_member_routing_enabled
            and contact_member_scope.explicit
            and isinstance(contact_runtime_context, dict)
        ):
            channel_type_matches = _contact_member_channel_type_matches(
                runtime_variables,
                contact_runtime_context,
                expected_channel_type=(
                    str(selected_contact_channel["type"])
                    if selected_contact_channel is not None
                    else None
                ),
            )
            if not channel_type_matches:
                contact_runtime_context = None
                resolved_contact_list_member_id = None

        if contextual_member_routing_enabled:
            workflow_meta = _ensure_workflow_meta(runtime_variables)
            workflow_meta["contact_member_routing"] = {
                "selectors": contact_member_scope.selectors(),
                "effective_selectors": effective_contact_member_scope.selectors(),
                "explicit": contact_member_scope.explicit,
                "valid": contact_member_scope.valid,
                "resolved_contact_list_member_id": resolved_contact_list_member_id,
                "channel_type_matches": channel_type_matches,
                "selected_contact_channel_active": selected_contact_channel is not None,
            }

        routing_contact_list_member_id = _resolved_contact_member_id_for_routing(
            effective_contact_member_scope,
            resolved_contact_list_member_id,
        )

        if (
            contextual_member_routing_enabled
            and (contact_member_scope.explicit or session_scope == "person")
            and contact_runtime_context is None
        ):
            failed_at = datetime.now(timezone.utc)
            if person_scope_without_selectors:
                failure_message = (
                    "Sessão por pessoa recebida sem identificadores de membro, lista ou mailing."
                )
            elif not contact_member_scope.valid:
                failure_message = "Identificadores de membro/lista inválidos no payload de origem."
            elif channel_type_matches is False:
                failure_message = (
                    "O membro explícito não corresponde ao tipo de canal informado pela sessão."
                )
            else:
                failure_message = "Nenhum membro ativo corresponde ao escopo explícito da sessão."
            workflow_meta = _ensure_workflow_meta(runtime_variables)
            workflow_meta["terminal_failure"] = {
                "code": "contact_member_scope_not_found",
                "message": failure_message,
                "selectors": contact_member_scope.selectors(),
                "failed_at": failed_at.isoformat(),
            }
            last_card_uuid = session_state.get("last_card_uuid")
            _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=None)
            await replace_session_workflow_state(
                db_session,
                session_id=session_id,
                runtime_variables=runtime_variables,
                last_card_uuid=_to_uuid_or_none(last_card_uuid),
                next_card_uuid=None,
                ended_at=failed_at,
                state=3,
            )
            logger.warning(
                "workflow m2 contact member scope rejected",
                extra={
                    "event": "orch.workflow.m2.contact_member_scope_rejected",
                    "flow_uuid": flow_uuid,
                    "session_id": session_id,
                    "session_uuid": session_uuid_for_metrics,
                    "selectors": contact_member_scope.selectors(),
                    "scope_valid": contact_member_scope.valid,
                },
            )
            return await _finalize(
                WorkflowExecutionResult(
                    True,
                    0,
                    "contact_member_scope_not_found",
                    last_card_uuid,
                    None,
                )
            )

        _inject_contact_runtime_scope(
            runtime_variables=runtime_variables,
            contact_row=contact_runtime_context,
        )
        _inject_callback_runtime_scope(
            runtime_variables=runtime_variables,
        )
        _inject_system_runtime_scope(
            runtime_variables=runtime_variables,
            session_state=session_state,
            contact_row=contact_runtime_context,
        )

        has_pending_whatsapp_events = False
        has_pending_dialer_events = False
        resume_cursor = _read_whatsapp_resume_cursor(runtime_variables)
        if resume_cursor is not None:
            has_pending_whatsapp_events = await has_pending_channel_events(
                db_session,
                session_id=session_id,
                channel="whatsapp",
            )
        dialer_resume_cursor = _read_dialer_resume_cursor(runtime_variables)
        if dialer_resume_cursor is not None:
            has_pending_dialer_events = await has_pending_channel_events(
                db_session,
                session_id=session_id,
                channel="dialer",
            )

        current_next_card_uuid = session_state.get("next_card_uuid")
        if current_next_card_uuid is None:
            current_next_card_uuid = _read_next_cursor(runtime_variables)
        blocking_stop_reason = _read_blocking_stop_reason(runtime_variables)

        frozen_until = session_state.get("frozen_until")
        should_preempt_to_whatsapp_resume_cursor = _should_preempt_to_whatsapp_resume_cursor(
            runtime_variables,
            has_pending_whatsapp_events=has_pending_whatsapp_events,
            current_next_card_uuid=current_next_card_uuid,
            blocking_stop_reason=blocking_stop_reason,
        )
        should_preempt_to_dialer_resume_cursor = (
            has_pending_dialer_events
            and dialer_resume_cursor is not None
            and (
                current_next_card_uuid is None
                or str(current_next_card_uuid) == str(dialer_resume_cursor)
                or blocking_stop_reason in DIALER_BLOCKING_STOP_REASONS
            )
        )
        should_preempt_wait_for_event = (
            blocking_stop_reason == WAIT_FOR_EVENT_BLOCKING_STOP_REASON
            and _should_resume_wait_for_event_blocking_execution(runtime_variables)
        )
        if isinstance(frozen_until, datetime):
            frozen_until_utc = frozen_until if frozen_until.tzinfo is not None else frozen_until.replace(tzinfo=timezone.utc)
            if frozen_until_utc > datetime.now(timezone.utc) and not (
                should_preempt_to_whatsapp_resume_cursor
                or should_preempt_to_dialer_resume_cursor
                or should_preempt_wait_for_event
            ):
                return await _finalize(
                    WorkflowExecutionResult(
                        True,
                        0,
                        "frozen_wait_active",
                        session_state.get("last_card_uuid"),
                        session_state.get("next_card_uuid"),
                    )
                )

        if should_preempt_to_whatsapp_resume_cursor:
            preempt_signature = _extract_whatsapp_status_signature_from_runtime(runtime_variables)
            if preempt_signature is not None:
                _set_whatsapp_last_preempt_signature(runtime_variables, preempt_signature)
            current_card_uuid = resume_cursor
        elif should_preempt_to_dialer_resume_cursor:
            current_card_uuid = dialer_resume_cursor
        else:
            current_card_uuid = current_next_card_uuid
            if current_card_uuid is None and _extract_whatsapp_status_from_runtime(runtime_variables) is not None:
                current_card_uuid = _read_whatsapp_resume_cursor(runtime_variables)
            if current_card_uuid is None and _extract_dialer_status_from_runtime(runtime_variables) is not None:
                current_card_uuid = _read_dialer_resume_cursor(runtime_variables)
        if blocking_stop_reason is not None:
            if _should_resume_whatsapp_blocking_execution(
                runtime_variables,
                has_pending_whatsapp_events=has_pending_whatsapp_events,
            ):
                _clear_blocking_execution(runtime_variables)
                await replace_session_workflow_state(
                    db_session,
                    session_id=session_id,
                    runtime_variables=runtime_variables,
                    last_card_uuid=_to_uuid_or_none(session_state.get("last_card_uuid")),
                    next_card_uuid=_to_uuid_or_none(current_card_uuid),
                )
            elif _should_resume_dialer_blocking_execution(runtime_variables):
                if blocking_stop_reason == "blocked_send_with_dialer":
                    resumed_from_card = str(session_state.get("last_card_uuid") or "").strip()
                    if resumed_from_card:
                        current_card_uuid = resumed_from_card
                _clear_blocking_execution(runtime_variables)
                await replace_session_workflow_state(
                    db_session,
                    session_id=session_id,
                    runtime_variables=runtime_variables,
                    last_card_uuid=_to_uuid_or_none(session_state.get("last_card_uuid")),
                    next_card_uuid=_to_uuid_or_none(current_card_uuid),
                )
            elif (
                blocking_stop_reason in RUN_FLOW_BLOCKING_STOP_REASONS
                and _should_resume_run_flow_blocking_execution(runtime_variables)
            ):
                _clear_blocking_execution(runtime_variables)
                await replace_session_workflow_state(
                    db_session,
                    session_id=session_id,
                    runtime_variables=runtime_variables,
                    last_card_uuid=_to_uuid_or_none(session_state.get("last_card_uuid")),
                    next_card_uuid=_to_uuid_or_none(current_card_uuid),
                )
            elif (
                blocking_stop_reason == WAIT_FOR_EVENT_BLOCKING_STOP_REASON
                and _should_resume_wait_for_event_blocking_execution(runtime_variables)
            ):
                _clear_blocking_execution(runtime_variables)
            elif _should_resume_switch_bot_flow_blocking_execution(runtime_variables):
                _clear_blocking_execution(runtime_variables)
                await replace_session_workflow_state(
                    db_session,
                    session_id=session_id,
                    runtime_variables=runtime_variables,
                    last_card_uuid=_to_uuid_or_none(session_state.get("last_card_uuid")),
                    next_card_uuid=_to_uuid_or_none(current_card_uuid),
                )
            elif _should_resume_identidade_person_blocking_execution(runtime_variables):
                resumed_from_card = str(session_state.get("last_card_uuid") or "").strip()
                if resumed_from_card:
                    current_card_uuid = resumed_from_card
                _clear_blocking_execution(runtime_variables)
                await replace_session_workflow_state(
                    db_session,
                    session_id=session_id,
                    runtime_variables=runtime_variables,
                    last_card_uuid=_to_uuid_or_none(session_state.get("last_card_uuid")),
                    next_card_uuid=_to_uuid_or_none(current_card_uuid),
                )
            else:
                return await _finalize(
                    WorkflowExecutionResult(
                        True,
                        0,
                        blocking_stop_reason,
                        session_state.get("last_card_uuid"),
                        current_card_uuid,
                    )
                )
        if current_card_uuid is None:
            return await _finalize(WorkflowExecutionResult(True, 0, "no_next_card", session_state.get("last_card_uuid"), None))

        components = index_components(definition)
        executed_steps = 0
        last_card_uuid = session_state.get("last_card_uuid")
        next_card_uuid = current_card_uuid

        for _ in range(max_steps):
            if not next_card_uuid:
                break

            step_started_at = datetime.now(timezone.utc)
            step_started_perf = time.perf_counter()
            component = components.get(next_card_uuid)
            if component is None:
                step_finished_at = datetime.now(timezone.utc)
                _append_metric(
                    metric_type="card",
                    status="error",
                    started_at=step_started_at,
                    finished_at=step_finished_at,
                    latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                    stopped_reason="component_not_found",
                    step_index=executed_steps + 1,
                    card_cursor=next_card_uuid,
                    component_kind_value=None,
                )
                return await _finalize(
                    WorkflowExecutionResult(True, executed_steps, "component_not_found", last_card_uuid, next_card_uuid)
                )

            kind = component_kind(component)
            branch_label: str | None = None

            try:
                _ensure_person_scope_component_supported(
                    session_scope=session_scope,
                    component_kind_value=kind,
                    selected_contact_channel=selected_contact_channel,
                )
                if kind == "set_variables":
                    _run_set_variables(component, runtime_variables)
                elif kind == "create_contact":
                    try:
                        branch_label = await _run_create_contact(
                            db_session=db_session,
                            flow_uuid=flow_uuid,
                            component=component,
                            runtime_variables=runtime_variables,
                            contact_row=contact_runtime_context,
                        )
                    except WorkflowExecutionError as exc:
                        exception_branch = _resolve_component_exception_branch_label(
                            definition=definition,
                            current_card_uuid=next_card_uuid,
                        )
                        if exception_branch is None:
                            raise
                        runtime_variables["create_contact_last_error"] = {
                            "component_ref_id": component.get("ref_id"),
                            "code": exc.code,
                            "message": exc.message,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        logger.warning(
                            "workflow m2 create contact failed",
                            extra={
                                "event": "orch.workflow.m2.create_contact.failed",
                                "flow_uuid": flow_uuid,
                                "session_id": session_id,
                                "component_ref_id": component.get("ref_id"),
                                "error_code": exc.code,
                            },
                        )
                        branch_label = exception_branch
                elif kind == "source_list_membership":
                    try:
                        branch_label = await _run_source_list_membership(
                            db_session=db_session,
                            flow_uuid=flow_uuid,
                            component=component,
                            runtime_variables=runtime_variables,
                        )
                    except WorkflowExecutionError as exc:
                        exception_branch = _resolve_component_exception_branch_label(
                            definition=definition,
                            current_card_uuid=next_card_uuid,
                        )
                        if exception_branch is None:
                            raise
                        runtime_variables["source_list_membership_last_error"] = {
                            "component_ref_id": component.get("ref_id"),
                            "code": exc.code,
                            "message": exc.message,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        logger.warning(
                            "workflow m2 source list membership failed",
                            extra={
                                "event": "orch.workflow.m2.source_list_membership.failed",
                                "flow_uuid": flow_uuid,
                                "session_id": session_id,
                                "component_ref_id": component.get("ref_id"),
                                "error_code": exc.code,
                            },
                        )
                        branch_label = exception_branch
                elif kind == "split_random":
                    try:
                        branch_label = _run_split_random(
                            component=component,
                            definition=definition,
                            current_card_uuid=next_card_uuid,
                            runtime_variables=runtime_variables,
                            flow_uuid=flow_uuid,
                            session_identity=str(session_uuid_for_metrics or session_id),
                            revision_id=str(revision_id_for_metrics),
                        )
                    except WorkflowExecutionError as exc:
                        exception_branch = _split_random_exception_branch_label(
                            definition=definition,
                            current_card_uuid=next_card_uuid,
                        )
                        runtime_variables.pop("split_random_last_result", None)
                        runtime_variables["split_random_last_error"] = {
                            "component_ref_id": component.get("ref_id"),
                            "code": exc.code,
                            "message": exc.message,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        logger.warning(
                            "workflow m2 split random failed",
                            extra={
                                "event": "orch.workflow.m2.split_random.failed",
                                "flow_uuid": flow_uuid,
                                "session_id": session_id,
                                "session_uuid": session_uuid_for_metrics,
                                "revision_id": revision_id_for_metrics,
                                "component_ref_id": component.get("ref_id"),
                                "error_code": exc.code,
                                "has_exception_branch": exception_branch is not None,
                            },
                        )
                        if exception_branch is None:
                            raise
                        branch_label = exception_branch
                    else:
                        split_result = runtime_variables.get("split_random_last_result")
                        logger.info(
                            "workflow m2 split random completed",
                            extra={
                                "event": "orch.workflow.m2.split_random.completed",
                                "flow_uuid": flow_uuid,
                                "session_id": session_id,
                                "session_uuid": session_uuid_for_metrics,
                                "revision_id": revision_id_for_metrics,
                                "component_ref_id": component.get("ref_id"),
                                "outcome": branch_label,
                                "bucket": (
                                    split_result.get("bucket")
                                    if isinstance(split_result, dict)
                                    else None
                                ),
                            },
                        )
                elif kind == "select_contact_channel":
                    try:
                        channel_execution = await _run_select_contact_channel(
                            db_session=db_session,
                            flow_uuid=flow_uuid,
                            session_id=session_id,
                            session_scope=session_scope,
                            component=component,
                            runtime_variables=runtime_variables,
                            contact_row=contact_runtime_context,
                        )
                    except WorkflowExecutionError as exc:
                        exception_branch = _resolve_component_exception_branch_label(
                            definition=definition,
                            current_card_uuid=next_card_uuid,
                        )
                        _ensure_workflow_meta(runtime_variables).pop(
                            "selected_contact_channel", None
                        )
                        selected_contact_channel = None
                        runtime_variables.pop("select_contact_channel_last_result", None)
                        runtime_variables["select_contact_channel_last_error"] = {
                            "component_ref_id": component.get("ref_id"),
                            "code": exc.code,
                            "message": exc.message,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        logger.warning(
                            "workflow m2 select contact channel failed",
                            extra={
                                "event": "orch.workflow.m2.select_contact_channel.failed",
                                "flow_uuid": flow_uuid,
                                "session_id": session_id,
                                "session_uuid": session_uuid_for_metrics,
                                "revision_id": revision_id_for_metrics,
                                "component_ref_id": component.get("ref_id"),
                                "session_scope": session_scope,
                                "error_code": exc.code,
                                "has_exception_branch": exception_branch is not None,
                            },
                        )
                        if exception_branch is None:
                            raise
                        branch_label = exception_branch
                    else:
                        branch_label = channel_execution.branch_label
                        selected_contact_channel = None
                        if channel_execution.contact_row is not None:
                            contact_runtime_context = channel_execution.contact_row
                            selected_contact_channel = _active_selected_contact_channel(
                                runtime_variables
                            )
                            try:
                                routing_contact_list_member_id = int(
                                    contact_runtime_context["contact_list_member_id"]
                                )
                            except (KeyError, TypeError, ValueError) as exc:
                                raise WorkflowExecutionError(
                                    "select_contact_channel_missing_contact_context",
                                    "O canal selecionado não possui um membro válido.",
                                ) from exc
                            selected_address = str(
                                contact_runtime_context.get("contact_channel_address") or ""
                            ).strip()
                            if selected_address:
                                session_state["entity_address"] = selected_address
                            _inject_contact_runtime_scope(
                                runtime_variables=runtime_variables,
                                contact_row=contact_runtime_context,
                            )
                            _inject_system_runtime_scope(
                                runtime_variables=runtime_variables,
                                session_state=session_state,
                                contact_row=contact_runtime_context,
                            )
                        logger.info(
                            "workflow m2 select contact channel completed",
                            extra={
                                "event": "orch.workflow.m2.select_contact_channel.completed",
                                "flow_uuid": flow_uuid,
                                "session_id": session_id,
                                "session_uuid": session_uuid_for_metrics,
                                "revision_id": revision_id_for_metrics,
                                "component_ref_id": component.get("ref_id"),
                                "session_scope": session_scope,
                                "outcome": branch_label,
                                "contact_list_member_id": (
                                    contact_runtime_context.get("contact_list_member_id")
                                    if channel_execution.contact_row is not None
                                    else None
                                ),
                            },
                        )
                elif kind == "wait_for_event":
                    try:
                        wait_execution = _run_wait_for_event(
                            component=component,
                            current_card_uuid=next_card_uuid,
                            runtime_variables=runtime_variables,
                        )
                    except WorkflowExecutionError as exc:
                        _clear_wait_for_event_state(runtime_variables)
                        _clear_blocking_execution(runtime_variables)
                        await clear_session_frozen_until(
                            db_session,
                            session_id=session_id,
                        )
                        exception_branch = _resolve_component_exception_branch_label(
                            definition=definition,
                            current_card_uuid=next_card_uuid,
                        )
                        runtime_variables["wait_for_event_last_error"] = {
                            "component_ref_id": component.get("ref_id"),
                            "code": exc.code,
                            "message": exc.message,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        logger.warning(
                            "workflow m2 wait for event failed",
                            extra={
                                "event": "orch.workflow.m2.wait_for_event.failed",
                                "flow_uuid": flow_uuid,
                                "session_id": session_id,
                                "component_ref_id": component.get("ref_id"),
                                "error_code": exc.code,
                                "has_exception_branch": exception_branch is not None,
                            },
                        )
                        if exception_branch is None:
                            raise
                        branch_label = exception_branch
                    else:
                        branch_label = wait_execution.branch_label
                        if branch_label is None:
                            last_card_uuid = next_card_uuid
                            next_card_uuid = last_card_uuid
                            executed_steps += 1
                            _set_cursors(
                                runtime_variables,
                                last_cursor=last_card_uuid,
                                next_cursor=next_card_uuid,
                            )
                            _mark_blocking_execution(
                                runtime_variables,
                                stopped_reason=WAIT_FOR_EVENT_BLOCKING_STOP_REASON,
                            )
                            _reset_loop_guard_counter(runtime_variables)
                            await replace_session_workflow_state(
                                db_session,
                                session_id=session_id,
                                runtime_variables=runtime_variables,
                                last_card_uuid=_to_uuid_or_none(last_card_uuid),
                                next_card_uuid=_to_uuid_or_none(next_card_uuid),
                                frozen_until=wait_execution.timeout_at,
                            )
                            step_latency_ms = (time.perf_counter() - step_started_perf) * 1000
                            step_finished_at = datetime.now(timezone.utc)
                            _append_metric(
                                metric_type="card",
                                status="success",
                                started_at=step_started_at,
                                finished_at=step_finished_at,
                                latency_ms=step_latency_ms,
                                stopped_reason=WAIT_FOR_EVENT_BLOCKING_STOP_REASON,
                                step_index=executed_steps,
                                card_cursor=last_card_uuid,
                                component_kind_value=kind,
                                details={
                                    "next_card_uuid": next_card_uuid,
                                    "timeout_at": wait_execution.timeout_at.isoformat(),
                                },
                            )
                            logger.info(
                                "workflow m2 wait for event armed",
                                extra={
                                    "event": "orch.workflow.m2.wait_for_event.armed",
                                    "flow_uuid": flow_uuid,
                                    "session_id": session_id,
                                    "session_uuid": session_uuid_for_metrics,
                                    "card_uuid": _to_uuid_or_none(last_card_uuid) or last_card_uuid,
                                    "component_ref_id": component.get("ref_id"),
                                    "timeout_at": wait_execution.timeout_at.isoformat(),
                                    "stopped_reason": WAIT_FOR_EVENT_BLOCKING_STOP_REASON,
                                },
                            )
                            return await _finalize(
                                WorkflowExecutionResult(
                                    True,
                                    executed_steps,
                                    WAIT_FOR_EVENT_BLOCKING_STOP_REASON,
                                    last_card_uuid,
                                    next_card_uuid,
                                )
                            )

                        _clear_wait_for_event_state(runtime_variables)
                        _clear_blocking_execution(runtime_variables)
                        await clear_session_frozen_until(
                            db_session,
                            session_id=session_id,
                        )
                        logger.info(
                            "workflow m2 wait for event completed",
                            extra={
                                "event": "orch.workflow.m2.wait_for_event.completed",
                                "flow_uuid": flow_uuid,
                                "session_id": session_id,
                                "session_uuid": session_uuid_for_metrics,
                                "component_ref_id": component.get("ref_id"),
                                "outcome": branch_label,
                            },
                        )
                elif kind == "condition":
                    branch_label = _resolve_condition_branch_label(
                        definition=definition,
                        current_card_uuid=next_card_uuid,
                        branch_label=_run_condition(component, runtime_variables),
                    )
                elif kind == "code_editor":
                    branch_candidates = outgoing_branch_labels(definition, current_card_uuid=next_card_uuid)
                    try:
                        branch_label = _run_code_editor(
                            component=component,
                            runtime_variables=runtime_variables,
                            branch_labels=branch_candidates,
                        )
                        branch_label = _resolve_code_editor_branch(
                            branch_label=branch_label,
                            branch_labels=branch_candidates,
                            runtime_variables=runtime_variables,
                        )
                    except WorkflowExecutionError as exc:
                        branch_label = _resolve_code_editor_branch(
                            branch_label=None,
                            branch_labels=branch_candidates,
                            runtime_variables=runtime_variables,
                            execution_error=exc,
                        )
                elif kind == "api_call":
                    try:
                        branch_label = _run_api_call(
                            component=component,
                            runtime_variables=runtime_variables,
                        )
                    except WorkflowExecutionError as exc:
                        exception_branch = _resolve_component_exception_branch_label(
                            definition=definition,
                            current_card_uuid=next_card_uuid,
                        )
                        if exception_branch is None:
                            raise
                        runtime_variables["api_call_last_error"] = {
                            "component_ref_id": component.get("ref_id"),
                            "code": exc.code,
                            "message": exc.message,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        logger.warning(
                            "workflow m2 api_call preparation failed",
                            extra={
                                "event": "orch.workflow.m2.api_call_preparation_failed",
                                "flow_uuid": flow_uuid,
                                "session_id": session_id,
                                "session_uuid": session_uuid_for_metrics,
                                "component_ref_id": component.get("ref_id"),
                                "error_code": exc.code,
                                "has_exception_branch": True,
                            },
                        )
                        branch_label = exception_branch
                elif kind == "cache_post":
                    try:
                        branch_label = await _run_cache_post(
                            db_session=db_session,
                            flow_uuid=flow_uuid,
                            component=component,
                            runtime_variables=runtime_variables,
                        )
                    except WorkflowExecutionError as exc:
                        exception_branch = _resolve_component_exception_branch_label(
                            definition=definition,
                            current_card_uuid=next_card_uuid,
                        )
                        if exception_branch is None:
                            raise
                        runtime_variables["cache_post_last_error"] = {
                            "component_ref_id": component.get("ref_id"),
                            "code": exc.code,
                            "message": exc.message,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        branch_label = exception_branch
                elif kind == "cache_get":
                    try:
                        branch_label = await _run_cache_get(
                            db_session=db_session,
                            definition=definition,
                            flow_uuid=flow_uuid,
                            component=component,
                            runtime_variables=runtime_variables,
                            branch_labels=outgoing_branch_labels(
                                definition,
                                current_card_uuid=next_card_uuid,
                            ),
                        )
                    except WorkflowExecutionError as exc:
                        exception_branch = _resolve_component_exception_branch_label(
                            definition=definition,
                            current_card_uuid=next_card_uuid,
                        )
                        if exception_branch is None:
                            raise
                        runtime_variables["cache_get_last_error"] = {
                            "component_ref_id": component.get("ref_id"),
                            "code": exc.code,
                            "message": exc.message,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        branch_label = exception_branch
                elif kind == "generate_file":
                    branch_label = await _run_generate_file(
                        db_session=db_session,
                        flow_uuid=flow_uuid,
                        component=component,
                        runtime_variables=runtime_variables,
                        session_id=session_id,
                    )
                elif kind == "intelligent_agent":
                    branch_label = await _run_intelligent_agent(
                        db_session=db_session,
                        component=component,
                        runtime_variables=runtime_variables,
                    )
                elif kind in {"send_whatsapp_interactive", "send_whatsapp_template"}:
                    _set_whatsapp_resume_cursor(
                        runtime_variables,
                        process_card_cursor=next_card_uuid,
                    )
                    pending_whatsapp_event = await claim_next_pending_channel_event(
                        db_session,
                        session_id=session_id,
                        channel="whatsapp",
                    )
                    if isinstance(pending_whatsapp_event, dict):
                        payload = pending_whatsapp_event.get("payload")
                        if isinstance(payload, dict):
                            runtime_variables["last_payload"] = payload
                            _inject_system_runtime_scope(
                                runtime_variables=runtime_variables,
                                session_state=session_state,
                                contact_row=contact_runtime_context,
                            )

                    branch_candidates = _resolve_send_whatsapp_interactive_branch_labels(
                        component=component,
                        runtime_variables=runtime_variables,
                    )
                    branch_label = None
                    if branch_candidates:
                        available_labels = outgoing_branch_labels(definition, current_card_uuid=next_card_uuid)
                        for candidate in branch_candidates:
                            normalized_candidate = str(candidate).strip().lower()
                            if normalized_candidate in available_labels:
                                branch_label = candidate
                                break
                        if branch_label is None:
                            runtime_variables["send_whatsapp_interactive_last_error"] = {
                                "component_ref_id": component.get("ref_id"),
                                "code": "send_whatsapp_interactive_branch_not_found",
                                "message": (
                                    f"Branches '{', '.join(branch_candidates)}' não encontrados para componente "
                                    f"send_whatsapp_interactive. Sessão permanece em escuta."
                                ),
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }
                            last_card_uuid = next_card_uuid
                            executed_steps += 1
                            _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=next_card_uuid)
                            _mark_blocking_execution(runtime_variables, stopped_reason="blocked_send_whatsapp_interactive")
                            _reset_loop_guard_counter(runtime_variables)
                            await replace_session_workflow_state(
                                db_session,
                                session_id=session_id,
                                runtime_variables=runtime_variables,
                                last_card_uuid=_to_uuid_or_none(last_card_uuid),
                                next_card_uuid=_to_uuid_or_none(next_card_uuid),
                            )
                            step_latency_ms = (time.perf_counter() - step_started_perf) * 1000
                            step_finished_at = datetime.now(timezone.utc)
                            _append_metric(
                                metric_type="card",
                                status="success",
                                started_at=step_started_at,
                                finished_at=step_finished_at,
                                latency_ms=step_latency_ms,
                                stopped_reason="blocked_send_whatsapp_interactive",
                                step_index=executed_steps,
                                card_cursor=last_card_uuid,
                                component_kind_value=kind,
                                details={"branch_candidates": branch_candidates, "next_card_uuid": next_card_uuid},
                            )
                            return await _finalize(
                                WorkflowExecutionResult(
                                    True,
                                    executed_steps,
                                    "blocked_send_whatsapp_interactive",
                                    last_card_uuid,
                                    next_card_uuid,
                                )
                            )
                        if (defer_until := _compute_whatsapp_status_order_delay(
                            runtime_variables=runtime_variables,
                            session_state=session_state,
                        )) is not None:
                            await replace_session_workflow_state(
                                db_session,
                                session_id=session_id,
                                runtime_variables=runtime_variables,
                                last_card_uuid=_to_uuid_or_none(last_card_uuid),
                                next_card_uuid=_to_uuid_or_none(next_card_uuid),
                                frozen_until=defer_until,
                            )
                            step_latency_ms = (time.perf_counter() - step_started_perf) * 1000
                            step_finished_at = datetime.now(timezone.utc)
                            _append_metric(
                                metric_type="card",
                                status="success",
                                started_at=step_started_at,
                                finished_at=step_finished_at,
                                latency_ms=step_latency_ms,
                                stopped_reason="frozen_wait_active",
                                step_index=executed_steps,
                                card_cursor=next_card_uuid,
                                component_kind_value=kind,
                                details={
                                    "defer_until": defer_until.isoformat(),
                                    "reason": "waiting_previous_whatsapp_status",
                                },
                            )
                            return await _finalize(
                                WorkflowExecutionResult(
                                    True,
                                    executed_steps,
                                    "frozen_wait_active",
                                    last_card_uuid,
                                    next_card_uuid,
                                )
                            )

                        runtime_variables.pop("send_whatsapp_interactive_last_error", None)
                    else:
                        assignment = await _prepare_send_whatsapp_template_contact_member(
                            db_session=db_session,
                            flow_uuid=flow_uuid,
                            session_id=session_id,
                            session_uuid=str(session_uuid_for_metrics),
                            revision_id=str(revision_id_for_metrics),
                            component=component,
                            runtime_variables=runtime_variables,
                            contact_row=contact_runtime_context,
                            contact_list_member_id=routing_contact_list_member_id,
                        )
                        if _is_send_with_whatsapp_limit_exhausted(assignment):
                            _set_synthetic_whatsapp_status_payload(
                                runtime_variables,
                                status="limit_reached",
                                reason="send_whatsapp_interactive_limit_exhausted",
                            )
                            _inject_system_runtime_scope(
                                runtime_variables=runtime_variables,
                                session_state=session_state,
                                contact_row=contact_runtime_context,
                            )
                            limit_candidates = _resolve_send_whatsapp_interactive_branch_labels(
                                component=component,
                                runtime_variables=runtime_variables,
                            )
                            if limit_candidates:
                                available_labels = outgoing_branch_labels(definition, current_card_uuid=next_card_uuid)
                                for candidate in limit_candidates:
                                    normalized_candidate = str(candidate).strip().lower()
                                    if normalized_candidate in available_labels:
                                        branch_label = candidate
                                        break
                        else:
                            runtime_variables.pop("send_whatsapp_interactive_last_error", None)

                    if branch_label is None:
                        last_card_uuid = next_card_uuid
                        executed_steps += 1
                        _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=next_card_uuid)
                        _mark_blocking_execution(runtime_variables, stopped_reason="blocked_send_whatsapp_interactive")
                        _reset_loop_guard_counter(runtime_variables)
                        await replace_session_workflow_state(
                            db_session,
                            session_id=session_id,
                            runtime_variables=runtime_variables,
                            last_card_uuid=_to_uuid_or_none(last_card_uuid),
                            next_card_uuid=_to_uuid_or_none(next_card_uuid),
                        )
                        step_latency_ms = (time.perf_counter() - step_started_perf) * 1000
                        step_finished_at = datetime.now(timezone.utc)
                        _append_metric(
                            metric_type="card",
                            status="success",
                            started_at=step_started_at,
                            finished_at=step_finished_at,
                            latency_ms=step_latency_ms,
                            stopped_reason="blocked_send_whatsapp_interactive",
                            step_index=executed_steps,
                            card_cursor=last_card_uuid,
                            component_kind_value=kind,
                            details={"next_card_uuid": next_card_uuid},
                        )
                        logger.info(
                            "workflow m2 card blocking",
                            extra={
                                "event": "orch.workflow.m2.card.blocking",
                                "flow_uuid": flow_uuid,
                                "session_id": session_id,
                                "session_uuid": session_uuid_for_metrics,
                                "card_uuid": _to_uuid_or_none(last_card_uuid) or last_card_uuid,
                                "component_kind": kind,
                                "step_index": executed_steps,
                                "latency_ms": round(step_latency_ms, 2),
                                "stopped_reason": "blocked_send_whatsapp_interactive",
                                "metric_type": "card",
                            },
                        )
                        return await _finalize(
                            WorkflowExecutionResult(
                                True,
                                executed_steps,
                                "blocked_send_whatsapp_interactive",
                                last_card_uuid,
                                next_card_uuid,
                            )
                        )
                elif kind == "process_whatsapp_response":
                    _set_whatsapp_resume_cursor(
                        runtime_variables,
                        process_card_cursor=next_card_uuid,
                    )
                    pending_whatsapp_event = await claim_next_pending_channel_event(
                        db_session,
                        session_id=session_id,
                        channel="whatsapp",
                    )
                    if isinstance(pending_whatsapp_event, dict):
                        payload = pending_whatsapp_event.get("payload")
                        if isinstance(payload, dict):
                            runtime_variables["last_payload"] = payload
                            _inject_system_runtime_scope(
                                runtime_variables=runtime_variables,
                                session_state=session_state,
                                contact_row=contact_runtime_context,
                            )
                    if (defer_until := _compute_whatsapp_status_order_delay(
                        runtime_variables=runtime_variables,
                        session_state=session_state,
                    )) is not None:
                        await replace_session_workflow_state(
                            db_session,
                            session_id=session_id,
                            runtime_variables=runtime_variables,
                            last_card_uuid=_to_uuid_or_none(last_card_uuid),
                            next_card_uuid=_to_uuid_or_none(next_card_uuid),
                            frozen_until=defer_until,
                        )
                        step_latency_ms = (time.perf_counter() - step_started_perf) * 1000
                        step_finished_at = datetime.now(timezone.utc)
                        _append_metric(
                            metric_type="card",
                            status="success",
                            started_at=step_started_at,
                            finished_at=step_finished_at,
                            latency_ms=step_latency_ms,
                            stopped_reason="frozen_wait_active",
                            step_index=executed_steps,
                            card_cursor=next_card_uuid,
                            component_kind_value=kind,
                            details={
                                "defer_until": defer_until.isoformat(),
                                "reason": "waiting_previous_whatsapp_status",
                            },
                        )
                        return await _finalize(
                            WorkflowExecutionResult(
                                True,
                                executed_steps,
                                "frozen_wait_active",
                                last_card_uuid,
                                next_card_uuid,
                            )
                        )
                    branch_label = _run_process_whatsapp_response(
                        component=component,
                        runtime_variables=runtime_variables,
                    )
                elif kind == "process_dialer_response":
                    _set_dialer_resume_cursor(
                        runtime_variables,
                        process_card_cursor=next_card_uuid,
                    )
                    pending_dialer_event = await claim_next_pending_channel_event(
                        db_session,
                        session_id=session_id,
                        channel="dialer",
                    )
                    if isinstance(pending_dialer_event, dict):
                        payload = pending_dialer_event.get("payload")
                        if isinstance(payload, dict):
                            runtime_variables["last_payload"] = payload
                            _inject_system_runtime_scope(
                                runtime_variables=runtime_variables,
                                session_state=session_state,
                                contact_row=contact_runtime_context,
                            )
                    branch_label = _run_process_dialer_response(
                        component=component,
                        runtime_variables=runtime_variables,
                    )
                elif kind == "run_flow":
                    branch_label = _run_run_flow(
                        component=component,
                        runtime_variables=runtime_variables,
                    )
                    if branch_label is not None:
                        _clear_run_flow_waiting(runtime_variables)
                    else:
                        _set_run_flow_waiting(runtime_variables, card_cursor=next_card_uuid)
                        last_card_uuid = next_card_uuid
                        next_card_uuid = last_card_uuid
                        executed_steps += 1
                        _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=next_card_uuid)
                        _mark_blocking_execution(runtime_variables, stopped_reason="blocked_run_flow")
                        _reset_loop_guard_counter(runtime_variables)
                        await replace_session_workflow_state(
                            db_session,
                            session_id=session_id,
                            runtime_variables=runtime_variables,
                            last_card_uuid=_to_uuid_or_none(last_card_uuid),
                            next_card_uuid=_to_uuid_or_none(next_card_uuid),
                        )
                        step_latency_ms = (time.perf_counter() - step_started_perf) * 1000
                        step_finished_at = datetime.now(timezone.utc)
                        _append_metric(
                            metric_type="card",
                            status="success",
                            started_at=step_started_at,
                            finished_at=step_finished_at,
                            latency_ms=step_latency_ms,
                            stopped_reason="blocked_run_flow",
                            step_index=executed_steps,
                            card_cursor=last_card_uuid,
                            component_kind_value=kind,
                            details={"next_card_uuid": next_card_uuid},
                        )
                        logger.info(
                            "workflow m2 card blocking",
                            extra={
                                "event": "orch.workflow.m2.card.blocking",
                                "flow_uuid": flow_uuid,
                                "session_id": session_id,
                                "session_uuid": session_uuid_for_metrics,
                                "card_uuid": _to_uuid_or_none(last_card_uuid) or last_card_uuid,
                                "component_kind": kind,
                                "step_index": executed_steps,
                                "latency_ms": round(step_latency_ms, 2),
                                "stopped_reason": "blocked_run_flow",
                                "metric_type": "card",
                            },
                        )
                        return await _finalize(
                            WorkflowExecutionResult(
                                True,
                                executed_steps,
                                "blocked_run_flow",
                                last_card_uuid,
                                next_card_uuid,
                            )
                        )
                elif (blocking_stop_reason := _blocking_stop_reason_for_component(kind)) is not None:
                    should_block_execution = True
                    if kind == "send_with_whatsapp":
                        assignment = await _prepare_send_whatsapp_template_contact_member(
                            db_session=db_session,
                            flow_uuid=flow_uuid,
                            session_id=session_id,
                            session_uuid=str(session_uuid_for_metrics),
                            revision_id=str(revision_id_for_metrics),
                            component=component,
                            runtime_variables=runtime_variables,
                            contact_row=contact_runtime_context,
                            contact_list_member_id=routing_contact_list_member_id,
                        )
                        if _is_send_with_whatsapp_limit_exhausted(assignment):
                            _clear_blocking_execution(runtime_variables)
                            _set_synthetic_whatsapp_status_payload(
                                runtime_variables,
                                status="limit_reached",
                                reason="send_with_whatsapp_limit_exhausted",
                            )
                            _inject_system_runtime_scope(
                                runtime_variables=runtime_variables,
                                session_state=session_state,
                                contact_row=contact_runtime_context,
                            )
                            should_block_execution = False
                    elif kind == "send_with_dialer":
                        branch_label = _resolve_send_with_dialer_branch_label(
                            component=component,
                            runtime_variables=runtime_variables,
                        )
                        if branch_label is None:
                            await _prepare_send_with_dialer_contact_member(
                                db_session=db_session,
                                flow_uuid=flow_uuid,
                                session_id=session_id,
                                runtime_variables=runtime_variables,
                                contact_list_member_id=routing_contact_list_member_id,
                            )
                        else:
                            should_block_execution = False
                    elif kind == "send_with_sms":
                        assignment = await _prepare_send_with_sms_contact_member(
                            db_session=db_session,
                            flow_uuid=flow_uuid,
                            session_id=session_id,
                            component=component,
                            runtime_variables=runtime_variables,
                            contact_row=contact_runtime_context,
                        )
                        logger.info(
                            "workflow m2 send with sms prepared",
                            extra={
                                "event": "orch.workflow.m2.send_with_sms.prepared",
                                "flow_uuid": flow_uuid,
                                "session_id": session_id,
                                "session_uuid": session_uuid_for_metrics,
                                "revision_id": revision_id_for_metrics,
                                "component_ref_id": component.get("ref_id"),
                                "contact_list_member_id": assignment.get(
                                    "contact_list_member_id"
                                ),
                                "mode": assignment.get("mode"),
                            },
                        )
                    elif kind == "run_flow":
                        _set_run_flow_waiting(runtime_variables, card_cursor=next_card_uuid)
                    elif kind == "switch_bot_flow":
                        branch_label = _run_switch_bot_flow(
                            definition=definition,
                            current_card_uuid=next_card_uuid,
                            component=component,
                            runtime_variables=runtime_variables,
                        )
                        if branch_label is not None:
                            should_block_execution = False
                    elif kind == "identidade_person":
                        try:
                            branch_label = await _run_identidade_person(
                                db_session=db_session,
                                flow_uuid=flow_uuid,
                                component=component,
                                runtime_variables=runtime_variables,
                            )
                        except WorkflowExecutionError as exc:
                            exception_branch = _resolve_component_exception_branch_label(
                                definition=definition,
                                current_card_uuid=next_card_uuid,
                            )
                            if exception_branch is None:
                                raise
                            runtime_variables["identidade_person_last_error"] = {
                                "component_ref_id": component.get("ref_id"),
                                "code": exc.code,
                                "message": exc.message,
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }
                            logger.warning(
                                "workflow m2 identidade person failed",
                                extra={
                                    "event": "orch.workflow.m2.identidade_person.failed",
                                    "flow_uuid": flow_uuid,
                                    "session_id": session_id,
                                    "component_ref_id": component.get("ref_id"),
                                    "error_code": exc.code,
                                },
                            )
                            branch_label = exception_branch
                        if branch_label is not None:
                            should_block_execution = False
                    if should_block_execution:
                        resolved_next = (
                            next_card_uuid
                            if kind in {"run_flow", "switch_bot_flow", "identidade_person"}
                            else resolve_next_card_uuid(definition, next_card_uuid)
                        )
                        last_card_uuid = next_card_uuid
                        next_card_uuid = resolved_next
                        executed_steps += 1
                        _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=next_card_uuid)
                        _mark_blocking_execution(runtime_variables, stopped_reason=blocking_stop_reason)
                        _reset_loop_guard_counter(runtime_variables)

                        await replace_session_workflow_state(
                            db_session,
                            session_id=session_id,
                            runtime_variables=runtime_variables,
                            last_card_uuid=_to_uuid_or_none(last_card_uuid),
                            next_card_uuid=_to_uuid_or_none(next_card_uuid),
                        )
                        step_latency_ms = (time.perf_counter() - step_started_perf) * 1000
                        step_finished_at = datetime.now(timezone.utc)
                        _append_metric(
                            metric_type="card",
                            status="success",
                            started_at=step_started_at,
                            finished_at=step_finished_at,
                            latency_ms=step_latency_ms,
                            stopped_reason=blocking_stop_reason,
                            step_index=executed_steps,
                            card_cursor=last_card_uuid,
                            component_kind_value=kind,
                            details={"next_card_uuid": next_card_uuid},
                        )
                        logger.info(
                            "workflow m2 card blocking",
                            extra={
                                "event": "orch.workflow.m2.card.blocking",
                                "flow_uuid": flow_uuid,
                                "session_id": session_id,
                                "session_uuid": session_uuid_for_metrics,
                                "card_uuid": _to_uuid_or_none(last_card_uuid) or last_card_uuid,
                                "component_kind": kind,
                                "step_index": executed_steps,
                                "latency_ms": round(step_latency_ms, 2),
                                "stopped_reason": blocking_stop_reason,
                                "metric_type": "card",
                            },
                        )
                        return await _finalize(
                            WorkflowExecutionResult(
                                True,
                                executed_steps,
                                blocking_stop_reason,
                                last_card_uuid,
                                next_card_uuid,
                            )
                        )
                elif kind in {"wait", "scheduling_moment", "scheduling-moment"}:
                    resolved_next = resolve_next_card_uuid(definition, next_card_uuid)
                    frozen_until = _compute_frozen_until(component, runtime_variables)
                    last_card_uuid = next_card_uuid
                    next_card_uuid = resolved_next
                    executed_steps += 1
                    _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=next_card_uuid)
                    _reset_loop_guard_counter(runtime_variables)

                    await replace_session_workflow_state(
                        db_session,
                        session_id=session_id,
                        runtime_variables=runtime_variables,
                        last_card_uuid=_to_uuid_or_none(last_card_uuid),
                        next_card_uuid=_to_uuid_or_none(next_card_uuid),
                        frozen_until=frozen_until,
                    )
                    step_finished_at = datetime.now(timezone.utc)
                    _append_metric(
                        metric_type="card",
                        status="success",
                        started_at=step_started_at,
                        finished_at=step_finished_at,
                        latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                        stopped_reason="scheduled_wait",
                        step_index=executed_steps,
                        card_cursor=last_card_uuid,
                        component_kind_value=kind,
                        details={"next_card_uuid": next_card_uuid},
                    )
                    logger.info(
                        "workflow m2 card latency",
                        extra={
                            "event": "orch.workflow.m2.card",
                            "flow_uuid": flow_uuid,
                            "session_id": session_id,
                            "session_uuid": session_uuid_for_metrics,
                            "card_uuid": _to_uuid_or_none(last_card_uuid) or last_card_uuid,
                            "component_kind": kind,
                            "step_index": executed_steps,
                            "latency_ms": round((time.perf_counter() - step_started_perf) * 1000, 2),
                            "stopped_reason": "scheduled_wait",
                            "metric_type": "card",
                        },
                    )
                    return await _finalize(
                        WorkflowExecutionResult(True, executed_steps, "scheduled_wait", last_card_uuid, next_card_uuid)
                    )
                elif kind in {"finish_flow", "finish-flow"}:
                    resume_cursor_for_channel = _read_whatsapp_resume_cursor(runtime_variables)
                    has_more_whatsapp_events = False
                    if resume_cursor_for_channel is not None:
                        has_more_whatsapp_events = await has_pending_channel_events(
                            db_session,
                            session_id=session_id,
                            channel="whatsapp",
                        )
                    if has_more_whatsapp_events and resume_cursor_for_channel is not None:
                        last_card_uuid = next_card_uuid
                        next_card_uuid = resume_cursor_for_channel
                        executed_steps += 1
                        _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=next_card_uuid)
                        transition_signature = f"{last_card_uuid}->{next_card_uuid}"
                        loop_guard_counter = _register_loop_guard_step(
                            runtime_variables,
                            transition_signature=transition_signature,
                        )
                        if loop_guard_counter >= loop_guard_repeat_threshold:
                            await replace_session_workflow_state(
                                db_session,
                                session_id=session_id,
                                runtime_variables=runtime_variables,
                                last_card_uuid=_to_uuid_or_none(last_card_uuid),
                                next_card_uuid=_to_uuid_or_none(next_card_uuid),
                                ended_at=datetime.now(timezone.utc),
                                state=3,
                            )
                            step_finished_at = datetime.now(timezone.utc)
                            _append_metric(
                                metric_type="card",
                                status="stopped",
                                started_at=step_started_at,
                                finished_at=step_finished_at,
                                latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                                stopped_reason="loop_guard_repeat_limit",
                                step_index=executed_steps,
                                card_cursor=last_card_uuid,
                                component_kind_value=kind,
                                details={
                                    "next_card_uuid": next_card_uuid,
                                    "loop_guard_counter": loop_guard_counter,
                                    "loop_guard_threshold": loop_guard_repeat_threshold,
                                },
                            )
                            return await _finalize(
                                WorkflowExecutionResult(
                                    True,
                                    executed_steps,
                                    "loop_guard_repeat_limit",
                                    last_card_uuid,
                                    next_card_uuid,
                                )
                            )
                        await replace_session_workflow_state(
                            db_session,
                            session_id=session_id,
                            runtime_variables=runtime_variables,
                            last_card_uuid=_to_uuid_or_none(last_card_uuid),
                            next_card_uuid=_to_uuid_or_none(next_card_uuid),
                            ended_at=None,
                            state=2,
                        )
                        step_finished_at = datetime.now(timezone.utc)
                        _append_metric(
                            metric_type="card",
                            status="success",
                            started_at=step_started_at,
                            finished_at=step_finished_at,
                            latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                            stopped_reason="continue_with_pending_channel_event",
                            step_index=executed_steps,
                            card_cursor=last_card_uuid,
                            component_kind_value=kind,
                            details={"next_card_uuid": next_card_uuid},
                        )
                        continue

                    finished_at = datetime.now(timezone.utc)
                    last_card_uuid = next_card_uuid
                    next_card_uuid = None
                    executed_steps += 1
                    _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=next_card_uuid)
                    _reset_loop_guard_counter(runtime_variables)

                    await replace_session_workflow_state(
                        db_session,
                        session_id=session_id,
                        runtime_variables=runtime_variables,
                        last_card_uuid=_to_uuid_or_none(last_card_uuid),
                        next_card_uuid=_to_uuid_or_none(next_card_uuid),
                        ended_at=finished_at,
                        state=3,
                    )

                    finish_flow_webhook_result = None
                    finish_flow_params = (
                        component.get("parameters")
                        if isinstance(component.get("parameters"), dict)
                        else {}
                    )
                    finish_flow_webhook = finish_flow_params.get("webhook")
                    if isinstance(finish_flow_webhook, str) and finish_flow_webhook.strip():
                        finish_flow_cdr_event = await fetch_next_pending_channel_event(
                            db_session,
                            session_id=session_id,
                            channel="dialer",
                        )
                        if finish_flow_cdr_event is None:
                            current_dialer_event_id = _finish_flow_dialer_event_id(runtime_variables)
                            if current_dialer_event_id is not None:
                                finish_flow_cdr_event = await fetch_channel_event_by_identity(
                                    db_session,
                                    session_id=session_id,
                                    channel="dialer",
                                    event_id=current_dialer_event_id,
                                )
                        pending_cdr = (
                            finish_flow_cdr_event.get("payload")
                            if isinstance(finish_flow_cdr_event, dict)
                            else None
                        )
                        if isinstance(pending_cdr, dict):
                            runtime_variables["cdr"] = copy.deepcopy(pending_cdr)
                        finish_flow_requires_cdr = _finish_flow_requires_dialer_cdr(
                            runtime_variables=runtime_variables,
                            cdr_event=finish_flow_cdr_event,
                        )
                        finish_flow_session_state = (
                            await fetch_session_webhook_snapshot(
                                db_session,
                                session_id=session_id,
                            )
                            or session_state
                        )
                        finish_flow_webhook_result = await _dispatch_finish_flow_webhook(
                            component=component,
                            session_state=finish_flow_session_state,
                            runtime_variables=runtime_variables,
                            finished_at=finished_at,
                            contact_state=contact_runtime_context,
                            cdr=(pending_cdr if isinstance(pending_cdr, dict) else None),
                            cdr_required=finish_flow_requires_cdr,
                            cdr_event_id=(
                                finish_flow_cdr_event.get("event_id")
                                if isinstance(finish_flow_cdr_event, dict)
                                else None
                            ),
                            cdr_event_row_id=(
                                int(finish_flow_cdr_event["id"])
                                if isinstance(finish_flow_cdr_event, dict)
                                and finish_flow_cdr_event.get("id") is not None
                                else None
                            ),
                            cdr_event_dispatched=(
                                isinstance(finish_flow_cdr_event, dict)
                                and finish_flow_cdr_event.get("discard_reason")
                                == FINISH_FLOW_WEBHOOK_DISPATCHED_REASON
                            ),
                        )
                        if (
                            finish_flow_webhook_result
                            and finish_flow_webhook_result.get("success") is True
                            and isinstance(finish_flow_cdr_event, dict)
                            and finish_flow_cdr_event.get("id") is not None
                        ):
                            await mark_channel_event_processed(
                                db_session,
                                event_row_id=int(finish_flow_cdr_event["id"]),
                                session_id=session_id,
                                channel="dialer",
                                discard_reason=FINISH_FLOW_WEBHOOK_DISPATCHED_REASON,
                            )
                        await replace_session_workflow_state(
                            db_session,
                            session_id=session_id,
                            runtime_variables=runtime_variables,
                            last_card_uuid=_to_uuid_or_none(last_card_uuid),
                            next_card_uuid=_to_uuid_or_none(next_card_uuid),
                            ended_at=finished_at,
                            state=3,
                        )
                    step_finished_at = datetime.now(timezone.utc)
                    _append_metric(
                        metric_type="card",
                        status="success",
                        started_at=step_started_at,
                        finished_at=step_finished_at,
                        latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                        stopped_reason="finished_by_component",
                        step_index=executed_steps,
                        card_cursor=last_card_uuid,
                        component_kind_value=kind,
                        details={"webhook": finish_flow_webhook_result} if finish_flow_webhook_result else None,
                    )
                    logger.info(
                        "workflow m2 card latency",
                        extra={
                            "event": "orch.workflow.m2.card",
                            "flow_uuid": flow_uuid,
                            "session_id": session_id,
                            "session_uuid": session_uuid_for_metrics,
                            "card_uuid": _to_uuid_or_none(last_card_uuid) or last_card_uuid,
                            "component_kind": kind,
                            "step_index": executed_steps,
                            "latency_ms": round((time.perf_counter() - step_started_perf) * 1000, 2),
                            "stopped_reason": "finished_by_component",
                            "metric_type": "card",
                        },
                    )
                    return await _finalize(
                        WorkflowExecutionResult(True, executed_steps, "finished_by_component", last_card_uuid, None)
                    )
                else:
                    step_finished_at = datetime.now(timezone.utc)
                    _append_metric(
                        metric_type="card",
                        status="stopped",
                        started_at=step_started_at,
                        finished_at=step_finished_at,
                        latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                        stopped_reason=f"component_not_supported:{kind}",
                        step_index=executed_steps + 1,
                        card_cursor=next_card_uuid,
                        component_kind_value=kind,
                    )
                    return await _finalize(
                        WorkflowExecutionResult(True, executed_steps, f"component_not_supported:{kind}", last_card_uuid, next_card_uuid)
                    )
            except Exception as exc:
                if (
                    kind in WHATSAPP_HSM_COMPONENT_KINDS
                    and isinstance(exc, WorkflowExecutionError)
                    and exc.code in WHATSAPP_HSM_ERROR_CODES
                ):
                    exception_branch = _resolve_component_exception_branch_label(
                        definition=definition,
                        current_card_uuid=next_card_uuid,
                    )
                    runtime_variables[f"{kind}_last_error"] = {
                        "component_ref_id": component.get("ref_id"),
                        "code": exc.code,
                        "message": exc.message,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    logger.warning(
                        "workflow m2 whatsapp hsm preparation failed",
                        extra={
                            "event": "orch.workflow.m2.whatsapp_hsm_preparation_failed",
                            "flow_uuid": flow_uuid,
                            "session_id": session_id,
                            "session_uuid": session_uuid_for_metrics,
                            "component_kind": kind,
                            "component_ref_id": component.get("ref_id"),
                            "error_code": exc.code,
                            "has_exception_branch": exception_branch is not None,
                        },
                    )
                    if exception_branch is not None:
                        branch_label = exception_branch

                if (
                    branch_label is None
                    and isinstance(exc, WorkflowExecutionError)
                    and exc.code in TERMINAL_WORKFLOW_ERROR_CODES
                ):
                    failed_at = datetime.now(timezone.utc)
                    failed_card_uuid = next_card_uuid
                    workflow_meta = _ensure_workflow_meta(runtime_variables)
                    workflow_meta["terminal_failure"] = {
                        "code": exc.code,
                        "message": exc.message,
                        "component_kind": kind,
                        "component_ref_id": component.get("ref_id"),
                        "failed_at": failed_at.isoformat(),
                    }
                    last_card_uuid = failed_card_uuid
                    next_card_uuid = None
                    executed_steps += 1
                    _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=None)
                    await replace_session_workflow_state(
                        db_session,
                        session_id=session_id,
                        runtime_variables=runtime_variables,
                        last_card_uuid=_to_uuid_or_none(last_card_uuid),
                        next_card_uuid=None,
                        ended_at=failed_at,
                        state=3,
                    )
                    step_finished_at = datetime.now(timezone.utc)
                    _append_metric(
                        metric_type="card",
                        status="error",
                        started_at=step_started_at,
                        finished_at=step_finished_at,
                        latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                        stopped_reason=exc.code,
                        step_index=executed_steps,
                        card_cursor=last_card_uuid,
                        component_kind_value=kind,
                        details={"message": exc.message, "terminal": True},
                    )
                    return await _finalize(
                        WorkflowExecutionResult(
                            True,
                            executed_steps,
                            exc.code,
                            last_card_uuid,
                            None,
                        )
                    )
                if branch_label is not None:
                    pass
                elif kind == "condition":
                    exception_branch = _resolve_component_exception_branch_label(
                        definition=definition,
                        current_card_uuid=next_card_uuid,
                    )
                    if exception_branch is not None:
                        runtime_variables["condition_last_error"] = {
                            "component_ref_id": component.get("ref_id"),
                            "code": getattr(exc, "code", type(exc).__name__),
                            "message": str(exc),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        branch_label = exception_branch
                    else:
                        step_finished_at = datetime.now(timezone.utc)
                        _append_metric(
                            metric_type="card",
                            status="error",
                            started_at=step_started_at,
                            finished_at=step_finished_at,
                            latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                            stopped_reason=type(exc).__name__,
                            step_index=executed_steps + 1,
                            card_cursor=next_card_uuid,
                            component_kind_value=kind,
                            details={"message": str(exc)},
                        )
                        raise
                else:
                    step_finished_at = datetime.now(timezone.utc)
                    _append_metric(
                        metric_type="card",
                        status="error",
                        started_at=step_started_at,
                        finished_at=step_finished_at,
                        latency_ms=(time.perf_counter() - step_started_perf) * 1000,
                        stopped_reason=type(exc).__name__,
                        step_index=executed_steps + 1,
                        card_cursor=next_card_uuid,
                        component_kind_value=kind,
                        details={"message": str(exc)},
                    )
                    raise

            current = next_card_uuid
            if branch_label is not None:
                resolved_next = resolve_next_card_uuid_by_branch(
                    definition,
                    current_card_uuid=current,
                    branch_label=branch_label,
                )
            else:
                resolved_next = resolve_next_card_uuid(definition, current)

            last_card_uuid = current
            next_card_uuid = resolved_next
            executed_steps += 1
            _set_cursors(runtime_variables, last_cursor=last_card_uuid, next_cursor=next_card_uuid)
            transition_signature = f"{current}->{resolved_next or 'END'}"
            loop_guard_counter = _register_loop_guard_step(
                runtime_variables,
                transition_signature=transition_signature,
            )
            if loop_guard_counter >= loop_guard_repeat_threshold:
                await replace_session_workflow_state(
                    db_session,
                    session_id=session_id,
                    runtime_variables=runtime_variables,
                    last_card_uuid=_to_uuid_or_none(last_card_uuid),
                    next_card_uuid=_to_uuid_or_none(next_card_uuid),
                    ended_at=datetime.now(timezone.utc),
                    state=3,
                )
                step_finished_at = datetime.now(timezone.utc)
                step_latency_ms = (time.perf_counter() - step_started_perf) * 1000
                _append_metric(
                    metric_type="card",
                    status="stopped",
                    started_at=step_started_at,
                    finished_at=step_finished_at,
                    latency_ms=step_latency_ms,
                    stopped_reason="loop_guard_repeat_limit",
                    step_index=executed_steps,
                    card_cursor=last_card_uuid,
                    component_kind_value=kind,
                    details={
                        "branch_label": branch_label,
                        "next_card_uuid": next_card_uuid,
                        "loop_guard_counter": loop_guard_counter,
                        "loop_guard_threshold": loop_guard_repeat_threshold,
                    },
                )
                return await _finalize(
                    WorkflowExecutionResult(
                        True,
                        executed_steps,
                        "loop_guard_repeat_limit",
                        last_card_uuid,
                        next_card_uuid,
                    )
                )

            await replace_session_workflow_state(
                db_session,
                session_id=session_id,
                runtime_variables=runtime_variables,
                last_card_uuid=_to_uuid_or_none(last_card_uuid),
                next_card_uuid=_to_uuid_or_none(next_card_uuid),
            )
            step_latency_ms = (time.perf_counter() - step_started_perf) * 1000
            _append_metric(
                metric_type="card",
                status="success",
                started_at=step_started_at,
                finished_at=datetime.now(timezone.utc),
                latency_ms=step_latency_ms,
                stopped_reason=None,
                step_index=executed_steps,
                card_cursor=last_card_uuid,
                component_kind_value=kind,
                details={"branch_label": branch_label, "next_card_uuid": next_card_uuid},
            )
            logger.info(
                "workflow m2 card latency",
                extra={
                    "event": "orch.workflow.m2.card",
                    "flow_uuid": flow_uuid,
                    "session_id": session_id,
                    "session_uuid": session_uuid_for_metrics,
                    "card_uuid": _to_uuid_or_none(last_card_uuid) or last_card_uuid,
                    "component_kind": kind,
                    "step_index": executed_steps,
                    "latency_ms": round(step_latency_ms, 2),
                    "metric_type": "card",
                },
            )

            if resolved_next is None:
                return await _finalize(
                    WorkflowExecutionResult(True, executed_steps, "end_of_branch", last_card_uuid, None)
                )

        return await _finalize(
            WorkflowExecutionResult(True, executed_steps, "max_steps_reached", last_card_uuid, next_card_uuid)
        )
