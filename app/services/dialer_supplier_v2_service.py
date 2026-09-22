from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib import request
from urllib.error import HTTPError, URLError
from uuid import UUID

from app.core.config import Settings, get_settings


_MAX_RESPONSE_BYTES = 1024 * 1024
_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_INTENT_FIELDS = (
    "session_uuid",
    "flow_uuid",
    "flow_revision_id",
    "component_ref_id",
    "contact_list_id",
    "contact_list_member_id",
    "dial_profile_id",
)


class DialerSupplierV2RegistrationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


class DialerSupplierV2EligibilityError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class DialerCycleRegistrationResult:
    cycle_id: str
    state: str
    replayed: bool
    dial_profile_revision_id: str
    attempt_policy_id: str
    attempt_limit_id: str
    profile_snapshot_checksum: str
    ready_at: str

    def runtime_payload(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "state": self.state,
            "replayed": self.replayed,
            "dial_profile_revision_id": self.dial_profile_revision_id,
            "attempt_policy_id": self.attempt_policy_id,
            "attempt_limit_id": self.attempt_limit_id,
            "profile_snapshot_checksum": self.profile_snapshot_checksum,
            "ready_at": self.ready_at,
        }


@dataclass(frozen=True)
class DialerNextChannelResult:
    decision: str
    reason: str
    next_eligible_at: str | None
    candidate: dict[str, Any] | None
    source: dict[str, Any]
    authorization: dict[str, Any] | None
    selector_component_ref_id: str
    evaluated_at: str

    def runtime_payload(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "next_eligible_at": self.next_eligible_at,
            "candidate": dict(self.candidate) if self.candidate else None,
            "source": dict(self.source),
            "authorization": (
                dict(self.authorization) if self.authorization else None
            ),
            "selector_component_ref_id": self.selector_component_ref_id,
            "evaluated_at": self.evaluated_at,
        }


@dataclass(frozen=True)
class DialerPostAnswerRetryResult:
    decision: str
    reason: str
    candidate: dict[str, Any]
    source: dict[str, Any]
    selector_component_ref_id: str
    audit_event_id: str
    replayed: bool
    evaluated_at: str

    def runtime_payload(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "candidate": dict(self.candidate),
            "source": dict(self.source),
            "selector_component_ref_id": self.selector_component_ref_id,
            "audit_event_id": self.audit_event_id,
            "replayed": self.replayed,
            "evaluated_at": self.evaluated_at,
        }


def dialer_supplier_v2_enabled_for_context(
    *, settings: Settings, workspace_uuid: str, flow_uuid: str
) -> bool:
    if not settings.dialer_supplier_v2_enabled:
        return False
    try:
        normalized_workspace = str(UUID(str(workspace_uuid)))
        normalized = str(UUID(str(flow_uuid)))
    except (TypeError, ValueError, AttributeError):
        return False
    allowed_workspaces = {
        str(UUID(str(item)))
        for item in settings.dialer_supplier_v2_workspace_allowlist
    }
    allowed_flows = {
        str(UUID(str(item))) for item in settings.dialer_supplier_v2_flow_allowlist
    }
    return normalized_workspace in allowed_workspaces and normalized in allowed_flows


def dialer_supplier_v2_multilane_enabled_for_context(
    *, settings: Settings, workspace_uuid: str, flow_uuid: str
) -> bool:
    """Require the existing Supplier V2 gate plus the dedicated multilane gate."""

    if not dialer_supplier_v2_enabled_for_context(
        settings=settings,
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
    ):
        return False
    if not bool(getattr(settings, "orch_dialer_multilane_v2_enabled", False)):
        return False
    try:
        normalized = str(UUID(str(flow_uuid)))
        allowed_flows = {
            str(UUID(str(item)))
            for item in getattr(
                settings,
                "orch_dialer_multilane_v2_flow_uuids",
                (),
            )
        }
    except (TypeError, ValueError, AttributeError):
        return False
    return normalized in allowed_flows


def build_dialer_cycle_intent(
    *,
    session_uuid: str,
    flow_uuid: str,
    flow_revision_id: str,
    component_ref_id: str,
    contact_list_id: str,
    contact_list_member_id: int,
    dial_profile_id: str,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    identity = {
        "session_uuid": _required_uuid(session_uuid, "session_uuid"),
        "flow_uuid": _required_uuid(flow_uuid, "flow_uuid"),
        "flow_revision_id": _required_uuid(flow_revision_id, "flow_revision_id"),
        "component_ref_id": str(component_ref_id or "").strip(),
        "contact_list_id": _required_uuid(contact_list_id, "contact_list_id"),
        "contact_list_member_id": _required_member_id(contact_list_member_id),
        "dial_profile_id": _required_uuid(dial_profile_id, "dial_profile_id"),
    }
    if not identity["component_ref_id"] or len(identity["component_ref_id"]) > 255:
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_component_ref_missing",
            "O novo card de discagem não possui ref_id válido.",
            retryable=False,
        )

    fingerprint = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    idempotency_key = f"orch:v2:dial-cycle:{fingerprint}"
    if isinstance(existing, Mapping) and all(
        str(existing.get(field)) == str(value) for field, value in identity.items()
    ):
        if str(existing.get("idempotency_key") or "") == idempotency_key:
            preserved = dict(existing)
            preserved.pop("callback_token", None)
            preserved.pop("callback_token_hash", None)
            return preserved

    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        **identity,
        "idempotency_key": idempotency_key,
        "status": "pending",
        "attempts": 0,
        "requested_at": now_iso,
        "updated_at": now_iso,
        "last_error": None,
    }


def parse_dialer_cycle_intent(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_intent_missing",
            "A sessão não possui intenção persistida para o Supplier V2.",
            retryable=False,
        )
    intent = build_dialer_cycle_intent(
        session_uuid=str(value.get("session_uuid") or ""),
        flow_uuid=str(value.get("flow_uuid") or ""),
        flow_revision_id=str(value.get("flow_revision_id") or ""),
        component_ref_id=str(value.get("component_ref_id") or ""),
        contact_list_id=str(value.get("contact_list_id") or ""),
        contact_list_member_id=value.get("contact_list_member_id"),
        dial_profile_id=str(value.get("dial_profile_id") or ""),
        existing=value,
    )
    if str(intent.get("idempotency_key") or "") != str(
        value.get("idempotency_key") or ""
    ):
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_idempotency_key_invalid",
            "A intenção persistida possui chave idempotente incompatível.",
            retryable=False,
        )
    return intent


def register_dialer_cycle(
    *,
    workspace_uuid: str,
    intent: Mapping[str, Any],
    settings: Settings | None = None,
) -> DialerCycleRegistrationResult:
    resolved_settings = settings or get_settings()
    normalized_workspace = _required_uuid(workspace_uuid, "workspace_uuid")
    parsed_intent = parse_dialer_cycle_intent(intent)

    base_url = str(
        resolved_settings.target_core_supplier_api_base_url or ""
    ).strip().rstrip("/")
    bearer = str(resolved_settings.target_core_api_bearer_token or "").strip()
    if not base_url:
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_base_url_missing",
            "TARGET_CORE_SUPPLIER_API_BASE_URL não está configurada.",
            retryable=False,
        )
    if not bearer:
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_bearer_missing",
            "TARGET_CORE_API_BEARER_TOKEN não está configurado.",
            retryable=False,
        )

    body = json.dumps(
        {field: parsed_intent[field] for field in _INTENT_FIELDS},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    req = request.Request(
        url=f"{base_url}/v2/contact-supplier/dialer-cycles",
        method="POST",
        data=body,
    )
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {bearer}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Idempotency-Key", str(parsed_intent["idempotency_key"]))
    req.add_header("X-WORKSPACE-UUID", normalized_workspace)

    status_code = 599
    response_body = b""
    try:
        with request.urlopen(
            req,
            timeout=float(resolved_settings.dialer_supplier_v2_http_timeout_seconds),
        ) as response:  # noqa: S310
            status_code = int(response.status)
            response_body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        status_code = int(exc.code)
        response_body = exc.read(_MAX_RESPONSE_BYTES + 1)
    except (URLError, TimeoutError, OSError) as exc:
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_unavailable",
            "O Supplier V2 está indisponível para registrar o ciclo.",
            retryable=True,
        ) from exc

    if len(response_body) > _MAX_RESPONSE_BYTES:
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_response_too_large",
            "A resposta do Supplier V2 excedeu o limite seguro.",
            retryable=True,
            status_code=status_code,
        )
    if status_code not in {200, 201}:
        error_code = _extract_error_code(response_body)
        raise DialerSupplierV2RegistrationError(
            error_code or "dialer_supplier_v2_http_error",
            "O Supplier V2 recusou o registro do ciclo.",
            retryable=status_code in _RETRYABLE_STATUS_CODES,
            status_code=status_code,
        )
    return _parse_cycle_response(response_body, parsed_intent=parsed_intent)


def _parse_cycle_response(
    response_body: bytes, *, parsed_intent: Mapping[str, Any]
) -> DialerCycleRegistrationResult:
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_invalid_response",
            "O Supplier V2 devolveu JSON inválido.",
            retryable=True,
        ) from exc
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_invalid_response",
            "O Supplier V2 devolveu um envelope incompatível.",
            retryable=True,
        )
    try:
        for field in _INTENT_FIELDS:
            if str(data[field]) != str(parsed_intent[field]):
                raise ValueError(f"mismatch:{field}")
        cycle_id = _required_uuid(data["id"], "cycle_id")
        profile_revision_id = _required_uuid(
            data["dial_profile_revision_id"], "dial_profile_revision_id"
        )
        attempt_policy_id = _required_uuid(
            data["attempt_policy_id"], "attempt_policy_id"
        )
        attempt_limit_id = _required_uuid(
            data["attempt_limit_id"], "attempt_limit_id"
        )
        state = str(data["state"] or "").strip().lower()
        replayed = data["replayed"]
        checksum = str(data["profile_snapshot_checksum"] or "").strip()
        ready_at = _required_iso_datetime(data["ready_at"], "ready_at")
        callback_token = str(data["callback_token"] or "").strip()
        accepted_states = (
            {"ready", "deferred", "terminal"}
            if replayed is True
            else {"ready"}
        )
        if (
            state not in accepted_states
            or not isinstance(replayed, bool)
            or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
            or not ready_at
            or not callback_token
        ):
            raise ValueError("invalid cycle response")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_invalid_response",
            "O Supplier V2 devolveu dados incompatíveis com a intenção.",
            retryable=True,
        ) from exc
    return DialerCycleRegistrationResult(
        cycle_id=cycle_id,
        state=state,
        replayed=replayed,
        dial_profile_revision_id=profile_revision_id,
        attempt_policy_id=attempt_policy_id,
        attempt_limit_id=attempt_limit_id,
        profile_snapshot_checksum=checksum,
        ready_at=ready_at,
    )


def resolve_next_dialer_channel(
    *,
    workspace_uuid: str,
    session_uuid: str,
    flow_uuid: str,
    flow_revision_id: str,
    source_component_ref_id: str,
    selector_component_ref_id: str,
    cycle_id: str,
    event_id: str,
    current_contact_list_member_id: int,
    mode: str,
    channel_label: str | None = None,
    settings: Settings | None = None,
) -> DialerNextChannelResult:
    resolved_settings = settings or get_settings()
    request_payload = {
        "session_uuid": _eligibility_uuid(session_uuid, "session_uuid"),
        "flow_uuid": _eligibility_uuid(flow_uuid, "flow_uuid"),
        "flow_revision_id": _eligibility_uuid(
            flow_revision_id, "flow_revision_id"
        ),
        "source_component_ref_id": _eligibility_component_ref(
            source_component_ref_id, "source_component_ref_id"
        ),
        "selector_component_ref_id": _eligibility_component_ref(
            selector_component_ref_id, "selector_component_ref_id"
        ),
        "cycle_id": _eligibility_uuid(cycle_id, "cycle_id"),
        "event_id": _eligibility_uuid(event_id, "event_id"),
        "current_contact_list_member_id": _eligibility_member_id(
            current_contact_list_member_id
        ),
        "mode": str(mode or "").strip().lower(),
        "channel_label": str(channel_label).strip() if channel_label else None,
    }
    if request_payload["mode"] not in {"respect_dial_rule", "flow_override"}:
        raise DialerSupplierV2EligibilityError(
            "dialer_supplier_v2_next_channel_mode_invalid",
            "O modo da seleção de próximo telefone é inválido.",
            retryable=False,
        )
    if request_payload["channel_label"] and len(
        str(request_payload["channel_label"])
    ) > 128:
        raise DialerSupplierV2EligibilityError(
            "dialer_supplier_v2_next_channel_label_invalid",
            "A label da seleção de próximo telefone é inválida.",
            retryable=False,
        )

    base_url = str(
        resolved_settings.target_core_supplier_api_base_url or ""
    ).strip().rstrip("/")
    bearer = str(resolved_settings.target_core_api_bearer_token or "").strip()
    if not base_url or not bearer:
        raise DialerSupplierV2EligibilityError(
            "dialer_supplier_v2_next_channel_configuration_missing",
            "A integração da Supplier V2 não está configurada.",
            retryable=False,
        )

    req = request.Request(
        url=f"{base_url}/v2/contact-supplier/dialer-next-channel/resolve",
        method="POST",
        data=json.dumps(
            request_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {bearer}")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-WORKSPACE-UUID", workspace_uuid)

    status_code = 599
    response_body = b""
    try:
        with request.urlopen(
            req,
            timeout=float(resolved_settings.dialer_supplier_v2_http_timeout_seconds),
        ) as response:  # noqa: S310
            status_code = int(response.status)
            response_body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        status_code = int(exc.code)
        response_body = exc.read(_MAX_RESPONSE_BYTES + 1)
    except (URLError, TimeoutError, OSError) as exc:
        raise DialerSupplierV2EligibilityError(
            "dialer_supplier_v2_next_channel_unavailable",
            "A Supplier V2 está indisponível para avaliar o próximo telefone.",
            retryable=True,
        ) from exc
    if len(response_body) > _MAX_RESPONSE_BYTES:
        raise DialerSupplierV2EligibilityError(
            "dialer_supplier_v2_next_channel_response_too_large",
            "A resposta da Supplier V2 excedeu o limite seguro.",
            retryable=True,
            status_code=status_code,
        )
    if status_code != 200:
        raise DialerSupplierV2EligibilityError(
            _extract_error_code(response_body)
            or "dialer_supplier_v2_next_channel_http_error",
            "A Supplier V2 recusou a avaliação do próximo telefone.",
            retryable=status_code in _RETRYABLE_STATUS_CODES,
            status_code=status_code,
        )
    return _parse_next_channel_response(
        response_body,
        requested=request_payload,
    )


def _parse_next_channel_response(
    response_body: bytes,
    *,
    requested: Mapping[str, Any],
) -> DialerNextChannelResult:
    try:
        payload = json.loads(response_body.decode("utf-8"))
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            raise ValueError("missing data")
        if data.get("supplier_contract") != "v2":
            raise ValueError("supplier contract mismatch")
        decision = str(data.get("decision") or "").strip().lower()
        if decision not in {
            "selected",
            "not_found",
            "blocked_by_policy",
            "deferred",
        }:
            raise ValueError("invalid decision")
        reason = str(data.get("reason") or "").strip()
        selector_ref = str(data.get("selector_component_ref_id") or "").strip()
        if selector_ref != requested["selector_component_ref_id"] or not reason:
            raise ValueError("identity mismatch")
        source = data.get("source")
        if not isinstance(source, Mapping) or any(
            str(source.get(field) or "").strip() != str(expected).strip()
            for field, expected in (
                ("cycle_id", requested["cycle_id"]),
                ("event_id", requested["event_id"]),
                ("component_ref_id", requested["source_component_ref_id"]),
            )
        ):
            raise ValueError("source mismatch")
        evaluated_at = _required_iso_datetime(
            data.get("evaluated_at"), "evaluated_at"
        )
        next_eligible_at: str | None = None
        if decision == "deferred":
            if reason != "calendar_closed":
                raise ValueError("unsupported deferred reason")
            next_eligible_at = _required_iso_datetime(
                data.get("next_eligible_at"), "next_eligible_at"
            )
            if datetime.fromisoformat(
                next_eligible_at.replace("Z", "+00:00")
            ) <= datetime.fromisoformat(evaluated_at.replace("Z", "+00:00")):
                raise ValueError("deferred deadline invalid")
        elif data.get("next_eligible_at") not in (None, ""):
            raise ValueError("unexpected deferred deadline")
        candidate_raw = data.get("candidate")
        authorization_raw = data.get("authorization")
        candidate: dict[str, Any] | None = None
        authorization: dict[str, Any] | None = None
        if decision == "selected":
            if not isinstance(candidate_raw, Mapping):
                raise ValueError("candidate missing")
            candidate = {
                "contact_list_member_id": _eligibility_member_id(
                    candidate_raw.get("contact_list_member_id")
                ),
                "contact_list_id": _eligibility_uuid(
                    candidate_raw.get("contact_list_id"), "contact_list_id"
                ),
                "mailing_id": _eligibility_member_id(
                    candidate_raw.get("mailing_id")
                ),
                "person_uuid": _eligibility_uuid(
                    candidate_raw.get("person_uuid"), "person_uuid"
                ),
                "channel_type": str(
                    candidate_raw.get("channel_type") or ""
                ).strip().lower(),
                "channel_label": candidate_raw.get("channel_label"),
                "channel_address": str(
                    candidate_raw.get("channel_address") or ""
                ).strip(),
                "is_primary": candidate_raw.get("is_primary") is True,
            }
            if (
                candidate["channel_type"] != "voice"
                or not candidate["channel_address"]
                or candidate["contact_list_member_id"]
                == requested["current_contact_list_member_id"]
            ):
                raise ValueError("candidate invalid")
            if not isinstance(authorization_raw, Mapping):
                raise ValueError("authorization missing")
            authorization = dict(authorization_raw)
            if str(authorization.get("mode") or "") != requested["mode"]:
                raise ValueError("authorization mismatch")
        elif candidate_raw is not None:
            raise ValueError("unexpected candidate")
    except (
        AttributeError,
        DialerSupplierV2EligibilityError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        if isinstance(exc, DialerSupplierV2EligibilityError):
            raise
        raise DialerSupplierV2EligibilityError(
            "dialer_supplier_v2_next_channel_invalid_response",
            "A Supplier V2 devolveu uma decisão de próximo telefone inválida.",
            retryable=True,
        ) from exc
    return DialerNextChannelResult(
        decision=decision,
        reason=reason,
        next_eligible_at=next_eligible_at,
        candidate=candidate,
        source=dict(source),
        authorization=authorization,
        selector_component_ref_id=selector_ref,
        evaluated_at=evaluated_at,
    )


def retry_dialer_after_answered_tabulation(
    *,
    workspace_uuid: str,
    session_uuid: str,
    flow_uuid: str,
    flow_revision_id: str,
    source_component_ref_id: str,
    selector_component_ref_id: str,
    wait_component_ref_id: str,
    cycle_id: str,
    event_id: str,
    current_contact_list_member_id: int,
    tabulation: str,
    tabulation_received_at: str,
    settings: Settings | None = None,
) -> DialerPostAnswerRetryResult:
    resolved_settings = settings or get_settings()
    request_payload = {
        "session_uuid": _eligibility_uuid(session_uuid, "session_uuid"),
        "flow_uuid": _eligibility_uuid(flow_uuid, "flow_uuid"),
        "flow_revision_id": _eligibility_uuid(
            flow_revision_id, "flow_revision_id"
        ),
        "source_component_ref_id": _eligibility_component_ref(
            source_component_ref_id, "source_component_ref_id"
        ),
        "selector_component_ref_id": _eligibility_component_ref(
            selector_component_ref_id, "selector_component_ref_id"
        ),
        "wait_component_ref_id": _eligibility_component_ref(
            wait_component_ref_id, "wait_component_ref_id"
        ),
        "cycle_id": _eligibility_uuid(cycle_id, "cycle_id"),
        "event_id": _eligibility_uuid(event_id, "event_id"),
        "current_contact_list_member_id": _eligibility_member_id(
            current_contact_list_member_id
        ),
        "tabulation": str(tabulation or "").strip(),
        "tabulation_received_at": _required_iso_datetime(
            tabulation_received_at,
            "tabulation_received_at",
        ),
    }
    if (
        not request_payload["tabulation"]
        or len(str(request_payload["tabulation"])) > 128
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in str(request_payload["tabulation"])
        )
    ):
        raise DialerSupplierV2EligibilityError(
            "dialer_supplier_v2_post_answer_tabulation_invalid",
            "A tabulação pós-atendimento é inválida.",
            retryable=False,
        )

    base_url = str(
        resolved_settings.target_core_supplier_api_base_url or ""
    ).strip().rstrip("/")
    bearer = str(resolved_settings.target_core_api_bearer_token or "").strip()
    if not base_url or not bearer:
        raise DialerSupplierV2EligibilityError(
            "dialer_supplier_v2_post_answer_configuration_missing",
            "A integração da Supplier V2 não está configurada.",
            retryable=False,
        )

    req = request.Request(
        url=f"{base_url}/v2/contact-supplier/dialer-post-answer/retry",
        method="POST",
        data=json.dumps(
            request_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {bearer}")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-WORKSPACE-UUID", workspace_uuid)

    status_code = 599
    response_body = b""
    try:
        with request.urlopen(
            req,
            timeout=float(resolved_settings.dialer_supplier_v2_http_timeout_seconds),
        ) as response:  # noqa: S310
            status_code = int(response.status)
            response_body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        status_code = int(exc.code)
        response_body = exc.read(_MAX_RESPONSE_BYTES + 1)
    except (URLError, TimeoutError, OSError) as exc:
        raise DialerSupplierV2EligibilityError(
            "dialer_supplier_v2_post_answer_unavailable",
            "A Supplier V2 está indisponível para retomar o atendimento.",
            retryable=True,
        ) from exc
    if len(response_body) > _MAX_RESPONSE_BYTES:
        raise DialerSupplierV2EligibilityError(
            "dialer_supplier_v2_post_answer_response_too_large",
            "A resposta da Supplier V2 excedeu o limite seguro.",
            retryable=True,
            status_code=status_code,
        )
    if status_code != 200:
        raise DialerSupplierV2EligibilityError(
            _extract_error_code(response_body)
            or "dialer_supplier_v2_post_answer_http_error",
            "A Supplier V2 recusou a retomada pós-atendimento.",
            retryable=status_code in _RETRYABLE_STATUS_CODES,
            status_code=status_code,
        )
    return _parse_post_answer_retry_response(
        response_body,
        requested=request_payload,
    )


def _parse_post_answer_retry_response(
    response_body: bytes,
    *,
    requested: Mapping[str, Any],
) -> DialerPostAnswerRetryResult:
    try:
        payload = json.loads(response_body.decode("utf-8"))
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping) or data.get("supplier_contract") != "v2":
            raise ValueError("supplier contract mismatch")
        if data.get("decision") != "retry_same_phone":
            raise ValueError("invalid decision")
        reason = str(data.get("reason") or "").strip()
        if not reason:
            raise ValueError("missing reason")
        selector_ref = str(data.get("selector_component_ref_id") or "").strip()
        if selector_ref != requested["selector_component_ref_id"]:
            raise ValueError("selector mismatch")
        source = data.get("source")
        if not isinstance(source, Mapping) or any(
            str(source.get(field) or "").strip() != str(expected).strip()
            for field, expected in (
                ("cycle_id", requested["cycle_id"]),
                ("event_id", requested["event_id"]),
                ("component_ref_id", requested["source_component_ref_id"]),
            )
        ):
            raise ValueError("source mismatch")
        candidate = data.get("candidate")
        if not isinstance(candidate, Mapping) or int(
            candidate.get("contact_list_member_id") or 0
        ) != int(requested["current_contact_list_member_id"]):
            raise ValueError("candidate mismatch")
        audit_event_id = _eligibility_uuid(
            data.get("audit_event_id"),
            "audit_event_id",
        )
        replayed = data.get("replayed")
        if not isinstance(replayed, bool):
            raise ValueError("invalid replay flag")
        evaluated_at = _required_iso_datetime(
            data.get("evaluated_at"),
            "evaluated_at",
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DialerSupplierV2EligibilityError(
            "dialer_supplier_v2_post_answer_invalid_response",
            "A Supplier V2 devolveu uma retomada pós-atendimento inválida.",
            retryable=True,
        ) from exc
    return DialerPostAnswerRetryResult(
        decision="retry_same_phone",
        reason=reason,
        candidate=dict(candidate),
        source=dict(source),
        selector_component_ref_id=selector_ref,
        audit_event_id=audit_event_id,
        replayed=replayed,
        evaluated_at=evaluated_at,
    )


def _eligibility_uuid(value: Any, field: str) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DialerSupplierV2EligibilityError(
            f"dialer_supplier_v2_next_channel_{field}_invalid",
            f"A seleção possui {field} inválido.",
            retryable=False,
        ) from exc
    if parsed.int == 0:
        raise DialerSupplierV2EligibilityError(
            f"dialer_supplier_v2_next_channel_{field}_invalid",
            f"A seleção possui {field} inválido.",
            retryable=False,
        )
    return str(parsed)


def _eligibility_component_ref(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 255:
        raise DialerSupplierV2EligibilityError(
            f"dialer_supplier_v2_next_channel_{field}_invalid",
            f"A seleção possui {field} inválido.",
            retryable=False,
        )
    return normalized


def _eligibility_member_id(value: Any) -> int:
    if isinstance(value, bool):
        value = None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DialerSupplierV2EligibilityError(
            "dialer_supplier_v2_next_channel_member_invalid",
            "A seleção possui referência de membro inválida.",
            retryable=False,
        ) from exc
    if parsed < 1:
        raise DialerSupplierV2EligibilityError(
            "dialer_supplier_v2_next_channel_member_invalid",
            "A seleção possui referência de membro inválida.",
            retryable=False,
        )
    return parsed


def _extract_error_code(response_body: bytes) -> str | None:
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    candidates = (payload, payload.get("detail"))
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        code = str(candidate.get("error_code") or "").strip()
        if code:
            return code[:160]
    return None


def _required_uuid(value: Any, field: str) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DialerSupplierV2RegistrationError(
            f"dialer_supplier_v2_{field}_invalid",
            f"A intenção possui {field} inválido.",
            retryable=False,
        ) from exc
    if parsed.int == 0:
        raise DialerSupplierV2RegistrationError(
            f"dialer_supplier_v2_{field}_invalid",
            f"A intenção possui {field} inválido.",
            retryable=False,
        )
    return str(parsed)


def _required_member_id(value: Any) -> int:
    if isinstance(value, bool):
        value = None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_contact_list_member_id_invalid",
            "A intenção possui contact_list_member_id inválido.",
            retryable=False,
        ) from exc
    if parsed < 1:
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_contact_list_member_id_invalid",
            "A intenção possui contact_list_member_id inválido.",
            retryable=False,
        )
    return parsed


def _required_iso_datetime(value: Any, field: str) -> str:
    raw = str(value or "").strip()
    normalized = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid {field}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"invalid {field}")
    return raw


__all__ = [
    "DialerCycleRegistrationResult",
    "DialerNextChannelResult",
    "DialerPostAnswerRetryResult",
    "DialerSupplierV2EligibilityError",
    "DialerSupplierV2RegistrationError",
    "build_dialer_cycle_intent",
    "dialer_supplier_v2_enabled_for_context",
    "dialer_supplier_v2_multilane_enabled_for_context",
    "parse_dialer_cycle_intent",
    "register_dialer_cycle",
    "resolve_next_dialer_channel",
    "retry_dialer_after_answered_tabulation",
]
