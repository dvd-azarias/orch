from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from app.services import dialer_supplier_v2_service as service


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
FLOW_UUID = "4e163399-e9a0-4335-895f-316c6a161299"
SESSION_UUID = "11111111-1111-4111-8111-111111111111"
REVISION_UUID = "22222222-2222-4222-8222-222222222222"
COMPONENT_REF_ID = "33333333-3333-4333-8333-333333333333"
CONTACT_LIST_ID = "44444444-4444-4444-8444-444444444444"
PROFILE_ID = "55555555-5555-4555-8555-555555555555"
CYCLE_ID = "66666666-6666-4666-8666-666666666666"
PROFILE_REVISION_ID = "77777777-7777-4777-8777-777777777777"
POLICY_ID = "88888888-8888-4888-8888-888888888888"
LIMIT_ID = "99999999-9999-4999-8999-999999999999"


class _Response:
    def __init__(self, payload: dict, *, status: int = 201) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "dialer_supplier_v2_enabled": True,
        "dialer_supplier_v2_workspace_allowlist": (WORKSPACE_UUID,),
        "dialer_supplier_v2_flow_allowlist": (FLOW_UUID,),
        "target_core_supplier_api_base_url": "https://supplier.internal",
        "target_core_api_bearer_token": "internal-bearer",
        "dialer_supplier_v2_http_timeout_seconds": 5.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _intent(**overrides: object) -> dict:
    values = {
        "session_uuid": SESSION_UUID,
        "flow_uuid": FLOW_UUID,
        "flow_revision_id": REVISION_UUID,
        "component_ref_id": COMPONENT_REF_ID,
        "contact_list_id": CONTACT_LIST_ID,
        "contact_list_member_id": 71,
        "dial_profile_id": PROFILE_ID,
    }
    values.update(overrides)
    return service.build_dialer_cycle_intent(**values)  # type: ignore[arg-type]


def _response(intent: dict, *, replayed: bool = False) -> dict:
    return {
        "data": {
            "id": CYCLE_ID,
            **{
                field: intent[field]
                for field in (
                    "session_uuid",
                    "flow_uuid",
                    "flow_revision_id",
                    "component_ref_id",
                    "contact_list_id",
                    "contact_list_member_id",
                    "dial_profile_id",
                )
            },
            "person_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "contact_channel_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "phone_normalized": "5511975620806",
            "dial_profile_revision_id": PROFILE_REVISION_ID,
            "attempt_policy_id": POLICY_ID,
            "attempt_limit_id": LIMIT_ID,
            "profile_snapshot_checksum": "c" * 64,
            "state": "ready",
            "ready_at": "2026-09-14T19:00:00-03:00",
            "callback_token": "csv2.1.must-not-be-persisted",
            "replayed": replayed,
        }
    }


def test_feature_flag_requires_workspace_and_flow_allowlists() -> None:
    settings = _settings()
    assert service.dialer_supplier_v2_enabled_for_context(
        settings=settings,
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
    )
    assert not service.dialer_supplier_v2_enabled_for_context(
        settings=settings,
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    assert not service.dialer_supplier_v2_enabled_for_context(
        settings=settings,
        workspace_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        flow_uuid=FLOW_UUID,
    )
    assert not service.dialer_supplier_v2_enabled_for_context(
        settings=_settings(dialer_supplier_v2_enabled=False),
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
    )


def test_multilane_gate_requires_supplier_and_dedicated_flow_allowlist() -> None:
    enabled = _settings(
        orch_dialer_multilane_v2_enabled=True,
        orch_dialer_multilane_v2_flow_uuids=(FLOW_UUID,),
    )
    assert service.dialer_supplier_v2_multilane_enabled_for_context(
        settings=enabled,
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
    )
    assert not service.dialer_supplier_v2_multilane_enabled_for_context(
        settings=_settings(
            orch_dialer_multilane_v2_enabled=False,
            orch_dialer_multilane_v2_flow_uuids=(FLOW_UUID,),
        ),
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
    )
    assert not service.dialer_supplier_v2_multilane_enabled_for_context(
        settings=_settings(
            orch_dialer_multilane_v2_enabled=True,
            orch_dialer_multilane_v2_flow_uuids=(),
        ),
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
    )


def test_intent_key_is_deterministic_and_existing_ready_state_is_preserved() -> None:
    first = _intent()
    second = _intent()
    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["idempotency_key"].startswith("orch:v2:dial-cycle:")
    assert len(first["idempotency_key"]) < 200

    ready = {
        **first,
        "status": "ready",
        "cycle_id": CYCLE_ID,
        "callback_token": "must-not-exist-in-real-runtime",
    }
    preserved = service.build_dialer_cycle_intent(
        session_uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        flow_revision_id=REVISION_UUID,
        component_ref_id=COMPONENT_REF_ID,
        contact_list_id=CONTACT_LIST_ID,
        contact_list_member_id=71,
        dial_profile_id=PROFILE_ID,
        existing=ready,
    )
    assert preserved["status"] == "ready"
    assert preserved["cycle_id"] == CYCLE_ID
    assert "callback_token" not in preserved


def test_register_cycle_posts_exact_contract_and_discards_callback_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    captured: dict[str, object] = {}

    def _urlopen(req, *, timeout):  # type: ignore[no-untyped-def]
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response(_response(intent))

    monkeypatch.setattr(service.request, "urlopen", _urlopen)

    result = service.register_dialer_cycle(
        workspace_uuid=WORKSPACE_UUID,
        intent=intent,
        settings=_settings(),  # type: ignore[arg-type]
    )

    assert captured["url"] == (
        "https://supplier.internal/v2/contact-supplier/dialer-cycles"
    )
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["X-workspace-uuid"] == WORKSPACE_UUID
    assert headers["Idempotency-key"] == intent["idempotency_key"]
    assert headers["Authorization"] == "Bearer internal-bearer"
    assert captured["timeout"] == 5.0
    assert captured["body"] == {
        key: intent[key]
        for key in (
            "session_uuid",
            "flow_uuid",
            "flow_revision_id",
            "component_ref_id",
            "contact_list_id",
            "contact_list_member_id",
            "dial_profile_id",
        )
    }
    assert result.cycle_id == CYCLE_ID
    assert result.replayed is False
    runtime_payload = result.runtime_payload()
    assert "callback_token" not in runtime_payload


def test_register_cycle_accepts_idempotent_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    monkeypatch.setattr(
        service.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            _response(intent, replayed=True), status=200
        ),
    )
    result = service.register_dialer_cycle(
        workspace_uuid=WORKSPACE_UUID,
        intent=intent,
        settings=_settings(),  # type: ignore[arg-type]
    )
    assert result.replayed is True
    assert result.cycle_id == CYCLE_ID


def test_register_cycle_accepts_terminal_idempotent_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    response = _response(intent, replayed=True)
    response["data"]["state"] = "terminal"
    monkeypatch.setattr(
        service.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(response, status=200),
    )

    result = service.register_dialer_cycle(
        workspace_uuid=WORKSPACE_UUID,
        intent=intent,
        settings=_settings(),  # type: ignore[arg-type]
    )

    assert result.replayed is True
    assert result.state == "terminal"


def test_register_cycle_rejects_terminal_on_initial_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    response = _response(intent, replayed=False)
    response["data"]["state"] = "terminal"
    monkeypatch.setattr(
        service.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(response, status=201),
    )

    with pytest.raises(service.DialerSupplierV2RegistrationError) as exc_info:
        service.register_dialer_cycle(
            workspace_uuid=WORKSPACE_UUID,
            intent=intent,
            settings=_settings(),  # type: ignore[arg-type]
        )

    assert exc_info.value.code == "dialer_supplier_v2_invalid_response"


def test_register_cycle_maps_422_as_permanent_without_exposing_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "message": "Erro de validação nos dados enviados",
        "errors": {"dial_profile_id": ["Perfil inválido."]},
        "error_code": "contact_supplier_v2_profile_invalid",
    }
    body = json.dumps(payload).encode("utf-8")

    def _urlopen(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise HTTPError(
            "https://supplier.internal",
            422,
            "unprocessable",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(service.request, "urlopen", _urlopen)
    with pytest.raises(service.DialerSupplierV2RegistrationError) as exc_info:
        service.register_dialer_cycle(
            workspace_uuid=WORKSPACE_UUID,
            intent=_intent(),
            settings=_settings(),  # type: ignore[arg-type]
        )
    assert exc_info.value.code == "contact_supplier_v2_profile_invalid"
    assert exc_info.value.status_code == 422
    assert exc_info.value.retryable is False
    assert "Perfil inválido" not in str(exc_info.value)


def test_register_cycle_retries_network_and_503_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    with pytest.raises(service.DialerSupplierV2RegistrationError) as exc_info:
        service.register_dialer_cycle(
            workspace_uuid=WORKSPACE_UUID,
            intent=_intent(),
            settings=_settings(),  # type: ignore[arg-type]
        )
    assert exc_info.value.retryable is True

    def _http_503(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise HTTPError(
            "https://supplier.internal",
            503,
            "unavailable",
            {},
            io.BytesIO(b"{}"),
        )

    monkeypatch.setattr(service.request, "urlopen", _http_503)
    with pytest.raises(service.DialerSupplierV2RegistrationError) as exc_info:
        service.register_dialer_cycle(
            workspace_uuid=WORKSPACE_UUID,
            intent=_intent(),
            settings=_settings(),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.retryable is True


def test_register_cycle_rejects_mismatched_success_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    payload = _response(intent)
    payload["data"]["flow_revision_id"] = (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    monkeypatch.setattr(
        service.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )
    with pytest.raises(service.DialerSupplierV2RegistrationError) as exc_info:
        service.register_dialer_cycle(
            workspace_uuid=WORKSPACE_UUID,
            intent=intent,
            settings=_settings(),  # type: ignore[arg-type]
        )
    assert exc_info.value.code == "dialer_supplier_v2_invalid_response"
    assert exc_info.value.retryable is True


def test_register_cycle_rejects_non_hex_snapshot_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    payload = _response(intent)
    payload["data"]["profile_snapshot_checksum"] = "z" * 64
    monkeypatch.setattr(
        service.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )

    with pytest.raises(service.DialerSupplierV2RegistrationError) as exc_info:
        service.register_dialer_cycle(
            workspace_uuid=WORKSPACE_UUID,
            intent=intent,
            settings=_settings(),  # type: ignore[arg-type]
        )

    assert exc_info.value.code == "dialer_supplier_v2_invalid_response"
    assert exc_info.value.retryable is True


def test_register_cycle_rejects_invalid_ready_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    payload = _response(intent)
    payload["data"]["ready_at"] = "not-a-timestamp"
    monkeypatch.setattr(
        service.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )

    with pytest.raises(service.DialerSupplierV2RegistrationError) as exc_info:
        service.register_dialer_cycle(
            workspace_uuid=WORKSPACE_UUID,
            intent=intent,
            settings=_settings(),  # type: ignore[arg-type]
        )

    assert exc_info.value.code == "dialer_supplier_v2_invalid_response"
    assert exc_info.value.retryable is True
