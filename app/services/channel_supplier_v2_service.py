from __future__ import annotations

import hashlib
import hmac
import json
import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet

from app.core.config import Settings, get_settings


_MAX_RESPONSE_BYTES = 1024 * 1024
_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_CALLBACK_TOKEN_PREFIX = "cdv2.1"
_CALLBACK_TOKEN_SIGNATURE_BYTES = 32
_CALLBACK_EVENT_KINDS = {
    "sms": ("dlr", "mo", "status"),
    "rcs": ("mo", "status"),
}
_INTENT_FIELDS = (
    "session_uuid",
    "flow_uuid",
    "flow_revision_id",
    "component_ref_id",
    "contact_list_id",
    "contact_list_member_id",
    "channel",
    "dispatch_sequence",
    "destination_fingerprint",
    "envelope_ciphertext",
    "envelope_checksum",
    "envelope_key_id",
)


def build_channel_dispatch_correlation_key(
    *,
    session_uuid: str,
    flow_uuid: str,
    flow_revision_id: str,
    component_ref_id: str,
    channel: str,
    dispatch_sequence: int,
) -> str:
    identity = {
        "session_uuid": _required_uuid(session_uuid, "session_uuid"),
        "flow_uuid": _required_uuid(flow_uuid, "flow_uuid"),
        "flow_revision_id": _required_uuid(flow_revision_id, "flow_revision_id"),
        "component_ref_id": str(component_ref_id or "").strip(),
        "channel": str(channel or "").strip().lower(),
        "dispatch_sequence": int(dispatch_sequence),
    }
    if (
        not identity["component_ref_id"]
        or identity["channel"] not in {"sms", "rcs"}
        or identity["dispatch_sequence"] <= 0
    ):
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_identity_invalid",
            "A identidade do channel dispatch é inválida.",
            retryable=False,
        )
    digest = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"cdv2:{identity['channel']}:{digest}"


class ChannelSupplierV2RegistrationError(RuntimeError):
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
class ChannelDispatchRegistrationResult:
    dispatch_id: str
    state: str
    replayed: bool

    def runtime_payload(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "state": self.state,
            "replayed": self.replayed,
        }


def channel_supplier_v2_enabled_for_context(
    *, settings: Settings, workspace_uuid: str, flow_uuid: str
) -> bool:
    if not settings.channel_supplier_v2_enabled:
        return False
    try:
        workspace = str(UUID(str(workspace_uuid)))
        flow = str(UUID(str(flow_uuid)))
        workspaces = {
            str(UUID(str(item)))
            for item in settings.channel_supplier_v2_workspace_allowlist
        }
        flows = {
            str(UUID(str(item))) for item in settings.channel_supplier_v2_flow_allowlist
        }
    except (TypeError, ValueError, AttributeError):
        return False
    return workspace in workspaces and flow in flows


def channel_supplier_v2_callbacks_enabled_for_context(
    *, settings: Settings, workspace_uuid: str, flow_uuid: str
) -> bool:
    return bool(
        getattr(settings, "channel_supplier_v2_callbacks_enabled", False)
    ) and (
        channel_supplier_v2_enabled_for_context(
            settings=settings,
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
        )
    )


def _required_uuid(value: Any, field: str) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ChannelSupplierV2RegistrationError(
            f"channel_supplier_v2_{field}_invalid",
            f"A intenção não possui {field} válido.",
            retryable=False,
        ) from exc
    if parsed.int == 0:
        raise ChannelSupplierV2RegistrationError(
            f"channel_supplier_v2_{field}_invalid",
            f"A intenção não possui {field} válido.",
            retryable=False,
        )
    return str(parsed)


def _normalize_destination(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if not 8 <= len(digits) <= 15:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_destination_invalid",
            "O canal selecionado não possui destinatário válido.",
            retryable=False,
        )
    return digits


def _fernet(settings: Settings) -> tuple[Fernet, bytes]:
    raw_text = str(settings.channel_supplier_v2_encryption_key or "").strip()
    if not raw_text:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_encryption_key_missing",
            "A chave de envelope do channel dispatch V2 não está configurada.",
            retryable=False,
        )
    raw = raw_text.encode("ascii")
    try:
        fingerprint_key = hashlib.sha256(
            b"channel-dispatch-v2:destination-fingerprint:" + raw
        ).digest()
        return Fernet(raw), fingerprint_key
    except (ValueError, UnicodeError) as exc:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_encryption_key_invalid",
            "A chave de envelope do channel dispatch V2 é inválida.",
            retryable=False,
        ) from exc


def _callback_signing_key(settings: Settings) -> bytes:
    raw_text = str(settings.channel_supplier_v2_encryption_key or "").strip()
    if not raw_text:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_encryption_key_missing",
            "A chave de callback do channel dispatch V2 não está configurada.",
            retryable=False,
        )
    try:
        Fernet(raw_text.encode("ascii"))
        return hashlib.sha256(
            b"channel-dispatch-v2:callback-token:" + raw_text.encode("ascii")
        ).digest()
    except (ValueError, UnicodeError) as exc:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_encryption_key_invalid",
            "A chave de callback do channel dispatch V2 é inválida.",
            retryable=False,
        ) from exc


def _b64url_encode(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    return urlsafe_b64decode((value + padding).encode("ascii"))


def build_channel_callback_token(
    *,
    workspace_uuid: str,
    session_uuid: str,
    flow_uuid: str,
    flow_revision_id: str,
    component_ref_id: str,
    channel: str,
    dispatch_sequence: int,
    settings: Settings | None = None,
) -> str:
    resolved = settings or get_settings()
    component = str(component_ref_id or "").strip()
    normalized_channel = str(channel or "").strip().lower()
    try:
        sequence = int(dispatch_sequence)
    except (TypeError, ValueError) as exc:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_callback_identity_invalid",
            "A identidade do callback SMS/RCS V2 é inválida.",
            retryable=False,
        ) from exc
    if (
        not component
        or len(component) > 255
        or normalized_channel not in _CALLBACK_EVENT_KINDS
        or sequence <= 0
    ):
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_callback_identity_invalid",
            "A identidade do callback SMS/RCS V2 é inválida.",
            retryable=False,
        )
    claims = {
        "v": 1,
        "w": _required_uuid(workspace_uuid, "workspace_uuid"),
        "s": _required_uuid(session_uuid, "session_uuid"),
        "f": _required_uuid(flow_uuid, "flow_uuid"),
        "r": _required_uuid(flow_revision_id, "flow_revision_id"),
        "c": component,
        "h": normalized_channel,
        "q": sequence,
    }
    encoded_claims = _b64url_encode(
        json.dumps(
            claims,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    )
    signed_value = f"{_CALLBACK_TOKEN_PREFIX}.{encoded_claims}".encode("ascii")
    signature = hmac.new(
        _callback_signing_key(resolved), signed_value, hashlib.sha256
    ).digest()
    return f"{_CALLBACK_TOKEN_PREFIX}.{encoded_claims}.{_b64url_encode(signature)}"


def parse_channel_callback_token(
    token: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    parts = str(token or "").strip().split(".")
    if len(parts) != 4 or ".".join(parts[:2]) != _CALLBACK_TOKEN_PREFIX:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_callback_token_invalid",
            "O token de callback SMS/RCS V2 é inválido.",
            retryable=False,
        )
    encoded_claims, encoded_signature = parts[2], parts[3]
    signed_value = f"{_CALLBACK_TOKEN_PREFIX}.{encoded_claims}".encode("ascii")
    try:
        supplied_signature = _b64url_decode(encoded_signature)
        if len(supplied_signature) != _CALLBACK_TOKEN_SIGNATURE_BYTES:
            raise ValueError("invalid signature length")
        expected_signature = hmac.new(
            _callback_signing_key(resolved), signed_value, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise ValueError("invalid signature")
        claims = json.loads(_b64url_decode(encoded_claims).decode("ascii"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_callback_token_invalid",
            "O token de callback SMS/RCS V2 é inválido.",
            retryable=False,
        ) from exc
    if not isinstance(claims, Mapping) or claims.get("v") != 1:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_callback_token_invalid",
            "O token de callback SMS/RCS V2 é inválido.",
            retryable=False,
        )
    try:
        parsed = {
            "workspace_uuid": _required_uuid(claims.get("w"), "workspace_uuid"),
            "session_uuid": _required_uuid(claims.get("s"), "session_uuid"),
            "flow_uuid": _required_uuid(claims.get("f"), "flow_uuid"),
            "flow_revision_id": _required_uuid(
                claims.get("r"), "flow_revision_id"
            ),
            "component_ref_id": str(claims.get("c") or "").strip(),
            "channel": str(claims.get("h") or "").strip().lower(),
            "dispatch_sequence": int(claims.get("q")),
        }
    except (TypeError, ValueError) as exc:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_callback_token_invalid",
            "O token de callback SMS/RCS V2 é inválido.",
            retryable=False,
        ) from exc
    if (
        not parsed["component_ref_id"]
        or len(parsed["component_ref_id"]) > 255
        or parsed["channel"] not in _CALLBACK_EVENT_KINDS
        or parsed["dispatch_sequence"] <= 0
    ):
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_callback_token_invalid",
            "O token de callback SMS/RCS V2 é inválido.",
            retryable=False,
        )
    return parsed


def build_channel_callback_urls(
    *,
    workspace_uuid: str,
    session_uuid: str,
    flow_uuid: str,
    flow_revision_id: str,
    component_ref_id: str,
    channel: str,
    dispatch_sequence: int,
    settings: Settings | None = None,
) -> dict[str, str]:
    resolved = settings or get_settings()
    normalized_channel = str(channel or "").strip().lower()
    event_kinds = _CALLBACK_EVENT_KINDS.get(normalized_channel)
    base_url = str(resolved.channel_supplier_v2_callback_base_url or "").strip().rstrip(
        "/"
    )
    parsed_base = urlsplit(base_url)
    if (
        event_kinds is None
        or parsed_base.scheme not in {"http", "https"}
        or not parsed_base.netloc
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_callback_base_url_invalid",
            "A base pública do callback SMS/RCS V2 é inválida.",
            retryable=False,
        )
    token = build_channel_callback_token(
        workspace_uuid=workspace_uuid,
        session_uuid=session_uuid,
        flow_uuid=flow_uuid,
        flow_revision_id=flow_revision_id,
        component_ref_id=component_ref_id,
        channel=normalized_channel,
        dispatch_sequence=dispatch_sequence,
        settings=resolved,
    )
    orch_base = base_url if base_url.endswith("/v1/orch") else f"{base_url}/v1/orch"
    return {
        event_kind: (
            f"{orch_base}/channel-supplier-v2/callbacks/{token}/"
            f"{normalized_channel}/{event_kind}"
        )
        for event_kind in event_kinds
    }


def build_channel_dispatch_intent(
    *,
    session_uuid: str,
    flow_uuid: str,
    flow_revision_id: str,
    component_ref_id: str,
    contact_list_id: str,
    contact_list_member_id: int,
    channel: str,
    dispatch_sequence: int,
    destination: str,
    provider: Mapping[str, Any],
    settings: Settings | None = None,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    normalized_channel = str(channel or "").strip().lower()
    if normalized_channel not in {"sms", "rcs"}:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_channel_invalid",
            "O channel dispatch aceita somente SMS ou RCS.",
            retryable=False,
        )
    try:
        member_id = int(contact_list_member_id)
        sequence = int(dispatch_sequence)
    except (TypeError, ValueError) as exc:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_identity_invalid",
            "A identidade do channel dispatch é inválida.",
            retryable=False,
        ) from exc
    component = str(component_ref_id or "").strip()
    if member_id <= 0 or sequence <= 0 or not component or len(component) > 255:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_identity_invalid",
            "A identidade do channel dispatch é inválida.",
            retryable=False,
        )
    identity = {
        "session_uuid": _required_uuid(session_uuid, "session_uuid"),
        "flow_uuid": _required_uuid(flow_uuid, "flow_uuid"),
        "flow_revision_id": _required_uuid(flow_revision_id, "flow_revision_id"),
        "component_ref_id": component,
        "contact_list_id": _required_uuid(contact_list_id, "contact_list_id"),
        "contact_list_member_id": member_id,
        "channel": normalized_channel,
        "dispatch_sequence": sequence,
    }
    if isinstance(existing, Mapping) and all(
        str(existing.get(field)) == str(value) for field, value in identity.items()
    ):
        try:
            return parse_channel_dispatch_intent(existing)
        except ChannelSupplierV2RegistrationError:
            pass

    normalized_destination = _normalize_destination(destination)
    fernet, key_material = _fernet(resolved)
    envelope = {
        "version": 1,
        "identity": identity,
        "destination": normalized_destination,
        "provider": dict(provider),
    }
    plaintext = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    content_checksum = hashlib.sha256(plaintext).hexdigest()
    ciphertext = fernet.encrypt(plaintext).decode("ascii")
    ciphertext_checksum = hashlib.sha256(ciphertext.encode("ascii")).hexdigest()
    destination_fingerprint = hmac.new(
        key_material, normalized_destination.encode("ascii"), hashlib.sha256
    ).hexdigest()
    idempotency_fingerprint = hashlib.sha256(
        json.dumps(
            {**identity, "content_checksum": content_checksum},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    return {
        **identity,
        "correlation_key": build_channel_dispatch_correlation_key(
            session_uuid=identity["session_uuid"],
            flow_uuid=identity["flow_uuid"],
            flow_revision_id=identity["flow_revision_id"],
            component_ref_id=identity["component_ref_id"],
            channel=identity["channel"],
            dispatch_sequence=identity["dispatch_sequence"],
        ),
        "destination_fingerprint": destination_fingerprint,
        "envelope_ciphertext": ciphertext,
        "envelope_checksum": ciphertext_checksum,
        "envelope_key_id": str(resolved.channel_supplier_v2_encryption_key_id).strip(),
        "idempotency_key": f"orch:v2:channel-dispatch:{idempotency_fingerprint}",
        "status": "pending",
        "attempts": 0,
        "requested_at": now,
        "updated_at": now,
        "last_error": None,
    }


def parse_channel_dispatch_intent(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_intent_missing",
            "A sessão não possui intenção SMS/RCS V2 persistida.",
            retryable=False,
        )
    parsed = dict(value)
    for field in ("session_uuid", "flow_uuid", "flow_revision_id", "contact_list_id"):
        parsed[field] = _required_uuid(parsed.get(field), field)
    component = str(parsed.get("component_ref_id") or "").strip()
    channel = str(parsed.get("channel") or "").strip().lower()
    try:
        member_id = int(parsed.get("contact_list_member_id"))
        sequence = int(parsed.get("dispatch_sequence"))
    except (TypeError, ValueError) as exc:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_intent_invalid",
            "A intenção SMS/RCS V2 persistida é inválida.",
            retryable=False,
        ) from exc
    if (
        not component
        or channel not in {"sms", "rcs"}
        or member_id <= 0
        or sequence <= 0
        or re.fullmatch(r"[0-9a-f]{64}", str(parsed.get("destination_fingerprint") or "")) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(parsed.get("envelope_checksum") or "")) is None
        or not str(parsed.get("envelope_ciphertext") or "").strip()
        or not str(parsed.get("envelope_key_id") or "").strip()
        or not str(parsed.get("idempotency_key") or "").startswith("orch:v2:channel-dispatch:")
    ):
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_intent_invalid",
            "A intenção SMS/RCS V2 persistida é inválida.",
            retryable=False,
        )
    parsed.update(
        {
            "component_ref_id": component,
            "channel": channel,
            "contact_list_member_id": member_id,
            "dispatch_sequence": sequence,
            "correlation_key": build_channel_dispatch_correlation_key(
                session_uuid=parsed["session_uuid"],
                flow_uuid=parsed["flow_uuid"],
                flow_revision_id=parsed["flow_revision_id"],
                component_ref_id=component,
                channel=channel,
                dispatch_sequence=sequence,
            ),
        }
    )
    return parsed


def register_channel_dispatch(
    *,
    workspace_uuid: str,
    intent: Mapping[str, Any],
    settings: Settings | None = None,
) -> ChannelDispatchRegistrationResult:
    resolved = settings or get_settings()
    workspace = _required_uuid(workspace_uuid, "workspace_uuid")
    parsed = parse_channel_dispatch_intent(intent)
    base_url = str(resolved.target_core_supplier_api_base_url or "").strip().rstrip("/")
    bearer = str(resolved.target_core_api_bearer_token or "").strip()
    if not base_url or not bearer:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_target_config_missing",
            "A integração interna com o Target Core não está configurada.",
            retryable=False,
        )
    body = json.dumps(
        {field: parsed[field] for field in _INTENT_FIELDS},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    req = request.Request(
        url=f"{base_url}/v2/contact-supplier/channel-dispatches",
        method="POST",
        data=body,
    )
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {bearer}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Idempotency-Key", str(parsed["idempotency_key"]))
    req.add_header("X-WORKSPACE-UUID", workspace)
    status_code = 599
    response_body = b""
    try:
        with request.urlopen(
            req,
            timeout=float(resolved.channel_supplier_v2_http_timeout_seconds),
        ) as response:  # noqa: S310 - internal configured endpoint
            status_code = int(response.status)
            response_body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        status_code = int(exc.code)
        response_body = exc.read(_MAX_RESPONSE_BYTES + 1)
    except (URLError, TimeoutError, OSError) as exc:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_unavailable",
            "A Supplier V2 está indisponível para registrar o dispatch.",
            retryable=True,
        ) from exc
    if len(response_body) > _MAX_RESPONSE_BYTES:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_response_too_large",
            "A resposta da Supplier V2 excedeu o limite seguro.",
            retryable=True,
            status_code=status_code,
        )
    if status_code not in {200, 201}:
        raise ChannelSupplierV2RegistrationError(
            _extract_error_code(response_body) or "channel_supplier_v2_http_error",
            "A Supplier V2 recusou o registro do dispatch.",
            retryable=status_code in _RETRYABLE_STATUS_CODES,
            status_code=status_code,
        )
    try:
        payload = json.loads(response_body.decode("utf-8"))
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            raise ValueError("missing data")
        for field in (
            "session_uuid",
            "flow_uuid",
            "flow_revision_id",
            "component_ref_id",
            "contact_list_id",
            "contact_list_member_id",
            "channel",
            "dispatch_sequence",
            "envelope_checksum",
        ):
            if str(data[field]) != str(parsed[field]):
                raise ValueError(f"mismatch:{field}")
        dispatch_id = _required_uuid(data["id"], "dispatch_id")
        state = str(data.get("state") or "").strip().lower()
        replayed = data.get("replayed")
        if state not in {"pending", "dispatching", "accepted", "failed", "uncertain"} or not isinstance(replayed, bool):
            raise ValueError("invalid state")
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ChannelSupplierV2RegistrationError(
            "channel_supplier_v2_invalid_response",
            "A Supplier V2 devolveu um envelope incompatível.",
            retryable=True,
        ) from exc
    return ChannelDispatchRegistrationResult(
        dispatch_id=dispatch_id,
        state=state,
        replayed=replayed,
    )


def _extract_error_code(response_body: bytes) -> str | None:
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    detail = payload.get("detail")
    if isinstance(detail, Mapping):
        value = detail.get("error_code")
        if value:
            return str(value)
    value = payload.get("error_code")
    return str(value) if value else None


__all__ = [
    "ChannelDispatchRegistrationResult",
    "ChannelSupplierV2RegistrationError",
    "build_channel_dispatch_intent",
    "build_channel_dispatch_correlation_key",
    "build_channel_callback_token",
    "build_channel_callback_urls",
    "channel_supplier_v2_callbacks_enabled_for_context",
    "channel_supplier_v2_enabled_for_context",
    "parse_channel_callback_token",
    "parse_channel_dispatch_intent",
    "register_channel_dispatch",
]
