from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app.services.channel_supplier_v2_service import (
    ChannelSupplierV2RegistrationError,
    build_channel_callback_token,
    build_channel_callback_urls,
    build_channel_dispatch_intent,
    channel_supplier_v2_callbacks_enabled_for_context,
    channel_supplier_v2_enabled_for_context,
    parse_channel_callback_token,
    register_channel_dispatch,
)


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
FLOW_UUID = "c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152"
REVISION_UUID = "44444444-4444-4444-8444-444444444444"
SESSION_UUID = "22222222-2222-4222-8222-222222222222"
LIST_UUID = "55555555-5555-4555-8555-555555555555"


def _settings():
    key = Fernet.generate_key().decode("ascii")
    return SimpleNamespace(
        channel_supplier_v2_enabled=True,
        channel_supplier_v2_workspace_allowlist=(WORKSPACE_UUID,),
        channel_supplier_v2_flow_allowlist=(FLOW_UUID,),
        channel_supplier_v2_encryption_key=key,
        channel_supplier_v2_encryption_key_id="v1",
        channel_supplier_v2_callbacks_enabled=True,
        channel_supplier_v2_callback_base_url="https://syncwebhook.example.test",
        target_core_supplier_api_base_url="https://target.invalid",
        target_core_api_bearer_token="internal-token",
        channel_supplier_v2_http_timeout_seconds=5.0,
    )


def _intent(settings):
    return build_channel_dispatch_intent(
        session_uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        flow_revision_id=REVISION_UUID,
        component_ref_id="send-sms-1",
        contact_list_id=LIST_UUID,
        contact_list_member_id=71,
        channel="sms",
        dispatch_sequence=1,
        destination="(11) 99999-0001",
        provider={
            "basic_token": "secret-basic-token",
            "message": "Olá Deivid",
            "codigo_carteira": "comunicado_digital",
            "codigo_fornecedor": "100",
            "callbacks": {
                "dlr": "https://callback.invalid/dlr",
                "mo": "https://callback.invalid/mo",
                "status": "https://callback.invalid/status",
            },
        },
        settings=settings,
    )


def test_channel_dispatch_intent_is_encrypted_and_reentry_is_stable() -> None:
    settings = _settings()
    intent = _intent(settings)
    serialized = json.dumps(intent)
    assert "secret-basic-token" not in serialized
    assert "Olá Deivid" not in serialized
    assert "11999990001" not in serialized
    plaintext = Fernet(settings.channel_supplier_v2_encryption_key.encode()).decrypt(
        intent["envelope_ciphertext"].encode()
    )
    envelope = json.loads(plaintext)
    assert envelope["destination"] == "11999990001"
    assert envelope["provider"]["message"] == "Olá Deivid"
    fingerprint_key = hashlib.sha256(
        b"channel-dispatch-v2:destination-fingerprint:"
        + settings.channel_supplier_v2_encryption_key.encode()
    ).digest()
    assert intent["destination_fingerprint"] == hmac.new(
        fingerprint_key,
        b"11999990001",
        hashlib.sha256,
    ).hexdigest()
    canonical_plaintext = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    content_checksum = hashlib.sha256(canonical_plaintext).hexdigest()
    identity = {
        key: envelope["identity"][key]
        for key in (
            "session_uuid",
            "flow_uuid",
            "flow_revision_id",
            "component_ref_id",
            "contact_list_id",
            "contact_list_member_id",
            "channel",
            "dispatch_sequence",
        )
    }
    idempotency_fingerprint = hashlib.sha256(
        json.dumps(
            {**identity, "content_checksum": content_checksum},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert intent["idempotency_key"] == (
        f"orch:v2:channel-dispatch:{idempotency_fingerprint}"
    )

    replay = build_channel_dispatch_intent(
        session_uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        flow_revision_id=REVISION_UUID,
        component_ref_id="send-sms-1",
        contact_list_id=LIST_UUID,
        contact_list_member_id=71,
        channel="sms",
        dispatch_sequence=1,
        destination="11999990001",
        provider={"must": "not be re-encrypted"},
        settings=settings,
        existing=intent,
    )
    assert replay["envelope_ciphertext"] == intent["envelope_ciphertext"]
    assert replay["idempotency_key"] == intent["idempotency_key"]


def test_channel_dispatch_gate_requires_both_allowlists() -> None:
    settings = _settings()
    assert channel_supplier_v2_enabled_for_context(
        settings=settings, workspace_uuid=WORKSPACE_UUID, flow_uuid=FLOW_UUID
    )
    assert not channel_supplier_v2_enabled_for_context(
        settings=settings,
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid="33333333-3333-4333-8333-333333333333",
    )
    assert channel_supplier_v2_callbacks_enabled_for_context(
        settings=settings, workspace_uuid=WORKSPACE_UUID, flow_uuid=FLOW_UUID
    )


def test_callback_token_is_signed_scoped_and_contains_no_sensitive_data() -> None:
    settings = _settings()
    token = build_channel_callback_token(
        workspace_uuid=WORKSPACE_UUID,
        session_uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        flow_revision_id=REVISION_UUID,
        component_ref_id="send-sms-1",
        channel="sms",
        dispatch_sequence=2,
        settings=settings,
    )

    assert token.startswith("cdv2.1.")
    assert "11999990001" not in token
    assert "secret-basic-token" not in token
    assert parse_channel_callback_token(token, settings=settings) == {
        "workspace_uuid": WORKSPACE_UUID,
        "session_uuid": SESSION_UUID,
        "flow_uuid": FLOW_UUID,
        "flow_revision_id": REVISION_UUID,
        "component_ref_id": "send-sms-1",
        "channel": "sms",
        "dispatch_sequence": 2,
    }

    token_parts = token.split(".")
    signature = token_parts[-1]
    tampered_signature = (
        ("A" if signature[0] != "A" else "B") + signature[1:]
    )
    tampered = ".".join([*token_parts[:-1], tampered_signature])
    with pytest.raises(ChannelSupplierV2RegistrationError) as exc_info:
        parse_channel_callback_token(tampered, settings=settings)
    assert exc_info.value.code == "channel_supplier_v2_callback_token_invalid"


def test_callback_urls_are_generated_only_for_official_channel_events() -> None:
    settings = _settings()
    sms_urls = build_channel_callback_urls(
        workspace_uuid=WORKSPACE_UUID,
        session_uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        flow_revision_id=REVISION_UUID,
        component_ref_id="send-sms-1",
        channel="sms",
        dispatch_sequence=1,
        settings=settings,
    )
    rcs_urls = build_channel_callback_urls(
        workspace_uuid=WORKSPACE_UUID,
        session_uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        flow_revision_id=REVISION_UUID,
        component_ref_id="send-rcs-1",
        channel="rcs",
        dispatch_sequence=1,
        settings=settings,
    )

    assert set(sms_urls) == {"dlr", "mo", "status"}
    assert set(rcs_urls) == {"mo", "status"}
    assert all(
        url.startswith(
            "https://syncwebhook.example.test/v1/orch/channel-supplier-v2/callbacks/cdv2.1."
        )
        for url in (*sms_urls.values(), *rcs_urls.values())
    )


class _Response:
    status = 201

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return self._body


def test_register_channel_dispatch_uses_internal_v2_route_and_idempotency() -> None:
    settings = _settings()
    intent = _intent(settings)
    body = json.dumps(
        {
            "data": {
                "id": "13131313-1313-4313-8313-131313131313",
                **{key: intent[key] for key in (
                    "session_uuid",
                    "flow_uuid",
                    "flow_revision_id",
                    "component_ref_id",
                    "contact_list_id",
                    "contact_list_member_id",
                    "channel",
                    "dispatch_sequence",
                    "envelope_checksum",
                )},
                "state": "pending",
                "replayed": False,
            }
        }
    ).encode()
    captured = {}

    def _urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["timeout"] = timeout
        return _Response(body)

    with patch(
        "app.services.channel_supplier_v2_service.request.urlopen", _urlopen
    ):
        result = register_channel_dispatch(
            workspace_uuid=WORKSPACE_UUID,
            intent=intent,
            settings=settings,
        )

    assert captured["url"].endswith("/v2/contact-supplier/channel-dispatches")
    assert captured["headers"]["Idempotency-key"] == intent["idempotency_key"]
    assert captured["headers"]["X-workspace-uuid"] == WORKSPACE_UUID
    assert result.dispatch_id == "13131313-1313-4313-8313-131313131313"
    assert result.state == "pending"
