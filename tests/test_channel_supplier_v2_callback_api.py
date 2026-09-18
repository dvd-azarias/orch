from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.api.v1 import orch as orch_api
from app.services.channel_supplier_v2_callback_service import (
    ChannelCallbackPersistenceResult,
    ChannelSupplierV2CallbackError,
)
from app.services.channel_supplier_v2_service import build_channel_callback_token


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
FLOW_UUID = "c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152"
SESSION_UUID = "22222222-2222-4222-8222-222222222222"
REVISION_UUID = "44444444-4444-4444-8444-444444444444"
COMPONENT_REF_ID = "send-sms-1"


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _DbSession:
    def __init__(self) -> None:
        self.commits = 0
        self.executions: list[object] = []

    def in_transaction(self) -> bool:
        return True

    def begin_nested(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement: object, *_args: object, **_kwargs: object) -> None:
        self.executions.append(statement)

    async def commit(self) -> None:
        self.commits += 1


class _StreamingRequest:
    def __init__(self, body: bytes, *, chunk_size: int | None = None) -> None:
        self.body = body
        self.chunk_size = chunk_size or max(1, len(body))

    async def stream(self):
        for offset in range(0, len(self.body), self.chunk_size):
            yield self.body[offset : offset + self.chunk_size]


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        channel_supplier_v2_enabled=True,
        channel_supplier_v2_workspace_allowlist=(WORKSPACE_UUID,),
        channel_supplier_v2_flow_allowlist=(FLOW_UUID,),
        channel_supplier_v2_encryption_key=Fernet.generate_key().decode("ascii"),
        channel_supplier_v2_callbacks_enabled=True,
        celery_execute_queue="orch_execute_test",
    )


def _token(settings: SimpleNamespace, *, channel: str = "sms") -> str:
    return build_channel_callback_token(
        workspace_uuid=WORKSPACE_UUID,
        session_uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        flow_revision_id=REVISION_UUID,
        component_ref_id=COMPONENT_REF_ID,
        channel=channel,
        dispatch_sequence=1,
        settings=settings,
    )


def _request(payload: object) -> _StreamingRequest:
    return _StreamingRequest(json.dumps(payload).encode("utf-8"), chunk_size=7)


def _result(*, resume_required: bool = True) -> ChannelCallbackPersistenceResult:
    return ChannelCallbackPersistenceResult(
        session_id=701,
        session_uuid=SESSION_UUID,
        accepted_count=1,
        inserted_count=1,
        idempotent_count=0,
        resume_required=resume_required,
        late_callback=not resume_required,
    )


def _install_common_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: SimpleNamespace,
    result: ChannelCallbackPersistenceResult | None = None,
) -> tuple[list[dict], list[dict]]:
    persisted_payloads: list[dict] = []
    enqueued: list[dict] = []
    monkeypatch.setattr(orch_api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        orch_api,
        "bind_workspace_context",
        lambda value: (value, f"ws_{value.replace('-', '')}"),
    )

    async def ensure_workspace(*_args: object, **_kwargs: object) -> None:
        return None

    async def persist(*_args: object, **kwargs: object):
        persisted_payloads.append(dict(kwargs))
        return result or _result()

    monkeypatch.setattr(orch_api, "ensure_active_workspace", ensure_workspace)
    monkeypatch.setattr(orch_api, "persist_channel_supplier_v2_callback", persist)
    monkeypatch.setattr(
        orch_api.resume_channel_supplier_v2_callback_task,
        "apply_async",
        lambda **kwargs: enqueued.append(kwargs),
    )
    return persisted_payloads, enqueued


@pytest.mark.asyncio
async def test_valid_callback_commits_before_enqueuing_exact_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    db_session = _DbSession()
    persisted_payloads, enqueued = _install_common_mocks(
        monkeypatch, settings=settings
    )

    response = await orch_api.callback_channel_supplier_v2(
        callback_token=_token(settings),
        channel="sms",
        event_kind="status",
        request=_request({"message_id": "sms-123", "codigo_status": 4}),  # type: ignore[arg-type]
        db_session=db_session,  # type: ignore[arg-type]
    )

    assert response.accepted is True
    assert response.session_uuid == SESSION_UUID
    assert db_session.commits == 1
    assert persisted_payloads[0]["claims"]["session_uuid"] == SESSION_UUID
    assert persisted_payloads[0]["payload"]["message_id"] == "sms-123"
    assert enqueued == [
        {
            "kwargs": {
                "workspace_uuid": WORKSPACE_UUID,
                "flow_uuid": FLOW_UUID,
                "session_id": 701,
            },
            "queue": "orch_execute_test",
            "routing_key": "orch_execute_test",
        }
    ]


@pytest.mark.asyncio
async def test_tampered_token_is_rejected_before_body_or_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    db_session = _DbSession()
    _install_common_mocks(monkeypatch, settings=settings)
    token = _token(settings)
    signature = token.rsplit(".", 1)[1]
    tampered = token[: -(len(signature))] + (
        ("A" if signature[0] != "A" else "B") + signature[1:]
    )

    with pytest.raises(HTTPException) as exc_info:
        await orch_api.callback_channel_supplier_v2(
            callback_token=tampered,
            channel="sms",
            event_kind="status",
            request=_request({"message_id": "must-not-be-read"}),  # type: ignore[arg-type]
            db_session=db_session,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 401
    assert db_session.executions == []
    assert db_session.commits == 0


@pytest.mark.asyncio
async def test_callback_rejects_channel_different_from_signed_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    db_session = _DbSession()
    _install_common_mocks(monkeypatch, settings=settings)

    with pytest.raises(HTTPException) as exc_info:
        await orch_api.callback_channel_supplier_v2(
            callback_token=_token(settings),
            channel="rcs",
            event_kind="status",
            request=_request({"message_id": "must-not-be-read"}),  # type: ignore[arg-type]
            db_session=db_session,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 409
    assert db_session.executions == []


@pytest.mark.asyncio
async def test_callback_rejects_body_larger_than_one_mibibyte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    db_session = _DbSession()
    _install_common_mocks(monkeypatch, settings=settings)
    request = _StreamingRequest(
        b"{" + b"x" * (1024 * 1024) + b"}",
        chunk_size=64 * 1024,
    )

    with pytest.raises(HTTPException) as exc_info:
        await orch_api.callback_channel_supplier_v2(
            callback_token=_token(settings),
            channel="sms",
            event_kind="status",
            request=request,  # type: ignore[arg-type]
            db_session=db_session,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 413
    assert db_session.executions == []


@pytest.mark.asyncio
async def test_identity_mismatch_is_reported_as_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    db_session = _DbSession()
    _install_common_mocks(monkeypatch, settings=settings)

    async def reject(*_args: object, **_kwargs: object):
        raise ChannelSupplierV2CallbackError(
            "channel_supplier_v2_callback_identity_mismatch",
            "A intenção não corresponde.",
        )

    monkeypatch.setattr(orch_api, "persist_channel_supplier_v2_callback", reject)

    with pytest.raises(HTTPException) as exc_info:
        await orch_api.callback_channel_supplier_v2(
            callback_token=_token(settings),
            channel="sms",
            event_kind="status",
            request=_request({"message_id": "sms-123"}),  # type: ignore[arg-type]
            db_session=db_session,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 409
    assert db_session.commits == 0


@pytest.mark.asyncio
async def test_enqueue_failure_keeps_committed_callback_for_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    db_session = _DbSession()
    _install_common_mocks(monkeypatch, settings=settings)

    def fail_enqueue(**_kwargs: object) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        orch_api.resume_channel_supplier_v2_callback_task,
        "apply_async",
        fail_enqueue,
    )

    with pytest.raises(HTTPException) as exc_info:
        await orch_api.callback_channel_supplier_v2(
            callback_token=_token(settings),
            channel="sms",
            event_kind="status",
            request=_request({"message_id": "sms-123"}),  # type: ignore[arg-type]
            db_session=db_session,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 503
    assert db_session.commits == 1


@pytest.mark.asyncio
async def test_late_callback_is_accepted_without_reopening_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    db_session = _DbSession()
    _, enqueued = _install_common_mocks(
        monkeypatch,
        settings=settings,
        result=_result(resume_required=False),
    )

    response = await orch_api.callback_channel_supplier_v2(
        callback_token=_token(settings),
        channel="sms",
        event_kind="dlr",
        request=_request({"message_id": "late-sms", "codigo_status": 1}),  # type: ignore[arg-type]
        db_session=db_session,  # type: ignore[arg-type]
    )

    assert response.late_callback is True
    assert response.resume_required is False
    assert db_session.commits == 1
    assert enqueued == []
