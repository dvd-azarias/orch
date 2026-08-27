from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

import app.api.v1.orch as orch_api
from app.schemas.orch import OrchTriggerAccepted, SessionExtraction


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
WORKSPACE_SCHEMA = f"ws_{WORKSPACE_UUID}"
FLOW_UUID = UUID("d3c79b7c-4726-46d0-a787-d99e590242b7")
TARGET_SESSION_ID = "9706f438-80be-47b7-a0e4-9923b1c489f0"


def _finish_payload(*, code: str = "success", category: str = "success") -> dict:
    return {
        "entity": {
            "type": "person",
            "address": "5511975620806",
            "identity": "34455521852",
        },
        "session": {"id": TARGET_SESSION_ID},
        "variables": {},
        "disposition": {
            "code": code,
            "category": category,
            "description": None,
        },
    }


def test_extract_switch_bot_flow_terminal_signal_maps_success() -> None:
    assert orch_api._extract_switch_bot_flow_terminal_signal(_finish_payload()) == {
        "target_session_id": TARGET_SESSION_ID,
        "terminal_status": "completed",
        "callback_status": "success",
    }


def test_extract_switch_bot_flow_terminal_signal_prioritizes_failure() -> None:
    signal = orch_api._extract_switch_bot_flow_terminal_signal(
        _finish_payload(code="failed", category="success")
    )

    assert signal is not None
    assert signal["terminal_status"] == "failed"
    assert signal["callback_status"] == "failed"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"session": {"id": TARGET_SESSION_ID}},
        {"session": {"id": ""}, "disposition": {"code": "success"}},
        {"session": {"id": TARGET_SESSION_ID}, "disposition": {"code": "pending"}},
    ],
)
def test_extract_switch_bot_flow_terminal_signal_rejects_non_terminal_payload(payload: dict) -> None:
    assert orch_api._extract_switch_bot_flow_terminal_signal(payload) is None


@pytest.mark.asyncio
async def test_alias_terminal_signal_completes_handoff_without_creating_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def _fake_bind(_workspace_uuid: str):  # type: ignore[no-untyped-def]
        return WORKSPACE_UUID, WORKSPACE_SCHEMA

    async def _fake_ensure(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    async def _fake_persist_callback(*_args, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {
            "session_id": 7105,
            "session_uuid": "b12bf980-df6d-48cf-ae70-9c349bfd276b",
            "state": 1,
            "idempotent": False,
            "status": "completed",
        }

    def _unexpected_detect(_payload):  # type: ignore[no-untyped-def]
        raise AssertionError("callback terminal correlacionado não deve seguir para persistência normal")

    monkeypatch.setattr(orch_api, "bind_workspace_context", _fake_bind)
    monkeypatch.setattr(orch_api, "ensure_active_workspace", _fake_ensure)
    monkeypatch.setattr(orch_api, "_persist_and_enqueue_switch_bot_flow_callback", _fake_persist_callback)
    monkeypatch.setattr(orch_api, "detect_app", _unexpected_detect)

    response = await orch_api._trigger_orch_for_workspace(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        payload=_finish_payload(),
        db_session=object(),  # type: ignore[arg-type]
        allow_switch_bot_flow_terminal_signal=True,
    )

    assert response.persistence == "switch_bot_flow_callback"
    assert response.session_id == 7105
    assert response.session_created is False
    assert response.extracted.entity_address == "5511975620806"
    assert response.extracted.entity_session_id == TARGET_SESSION_ID
    assert response.workflow_execution == {
        "mode": "async",
        "enqueued": True,
        "dispatcher": "celery",
        "reason": "switch_bot_flow_terminal_callback",
        "target_session_id": TARGET_SESSION_ID,
        "terminal_status": "completed",
        "idempotent": False,
    }
    assert captured["workspace_uuid"] == WORKSPACE_UUID
    assert captured["target_session_id"] == TARGET_SESSION_ID
    assert captured["terminal_status"] == "completed"
    assert captured["callback_payload"]["source"] == "finish_flow_alias"


@pytest.mark.asyncio
async def test_alias_terminal_signal_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_bind(_workspace_uuid: str):  # type: ignore[no-untyped-def]
        return WORKSPACE_UUID, WORKSPACE_SCHEMA

    async def _fake_ensure(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    async def _fake_persist_callback(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return {
            "session_id": 7105,
            "session_uuid": "b12bf980-df6d-48cf-ae70-9c349bfd276b",
            "state": 3,
            "idempotent": True,
            "status": "completed",
        }

    monkeypatch.setattr(orch_api, "bind_workspace_context", _fake_bind)
    monkeypatch.setattr(orch_api, "ensure_active_workspace", _fake_ensure)
    monkeypatch.setattr(orch_api, "_persist_and_enqueue_switch_bot_flow_callback", _fake_persist_callback)

    response = await orch_api._trigger_orch_for_workspace(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        payload=_finish_payload(),
        db_session=object(),  # type: ignore[arg-type]
        allow_switch_bot_flow_terminal_signal=True,
    )

    assert response.workflow_execution is not None
    assert response.workflow_execution["idempotent"] is True
    assert response.session_state == 3
    assert response.session_created is False


@pytest.mark.asyncio
async def test_unmatched_terminal_signal_preserves_existing_trigger_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    expected = OrchTriggerAccepted(
        status="accepted",
        accepted=True,
        flow_uuid=str(FLOW_UUID),
        app="GenericApp",
        persistence="saved",
        extracted=SessionExtraction(
            entity="generated",
            entity_type="api_request",
            entity_address="generated",
            entity_session_id="generated",
        ),
        session_id=7106,
        session_uuid="a9c9bbbd-d1a2-40b0-9b1e-5807f6790e95",
        session_state=1,
        session_created=True,
    )

    def _fake_bind(_workspace_uuid: str):  # type: ignore[no-untyped-def]
        return WORKSPACE_UUID, WORKSPACE_SCHEMA

    async def _fake_ensure(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    async def _fake_persist_callback(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    async def _fake_process(*_args, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(orch_api, "bind_workspace_context", _fake_bind)
    monkeypatch.setattr(orch_api, "ensure_active_workspace", _fake_ensure)
    monkeypatch.setattr(orch_api, "_persist_and_enqueue_switch_bot_flow_callback", _fake_persist_callback)
    monkeypatch.setattr(orch_api, "detect_app", lambda _payload: "GenericApp")
    monkeypatch.setattr(orch_api, "process_single_payload", _fake_process)

    response = await orch_api._trigger_orch_for_workspace(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        payload=_finish_payload(),
        db_session=object(),  # type: ignore[arg-type]
        allow_switch_bot_flow_terminal_signal=True,
    )

    assert response is expected
    assert captured["payload"] == _finish_payload()


class _DummyTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False


class _DummySession:
    def __init__(self) -> None:
        self.committed = False
        self.statements: list[str] = []

    def in_transaction(self) -> bool:
        return True

    def begin_nested(self) -> _DummyTransaction:
        return _DummyTransaction()

    async def execute(self, statement, _parameters=None):  # noqa: ANN001
        self.statements.append(str(statement))

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_persist_and_enqueue_switch_bot_flow_callback_uses_workspace_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _DummySession()
    captured: dict = {}

    async def _fake_apply(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return {
            "session_id": 7105,
            "session_uuid": "b12bf980-df6d-48cf-ae70-9c349bfd276b",
            "state": 1,
            "idempotent": False,
            "status": "completed",
        }

    def _fake_apply_async(*_args, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return SimpleNamespace(id="task-id")

    monkeypatch.setattr(orch_api, "apply_switch_bot_flow_callback", _fake_apply)
    monkeypatch.setattr(
        orch_api,
        "get_settings",
        lambda: SimpleNamespace(celery_execute_queue="orch_execute"),
    )
    monkeypatch.setattr(
        orch_api,
        "advance_session_task",
        SimpleNamespace(apply_async=_fake_apply_async),
    )

    persisted = await orch_api._persist_and_enqueue_switch_bot_flow_callback(
        workspace_uuid=WORKSPACE_UUID,
        workspace_schema=WORKSPACE_SCHEMA,
        flow_uuid=str(FLOW_UUID),
        target_session_id=TARGET_SESSION_ID,
        terminal_status="completed",
        callback_payload={"session_id": TARGET_SESSION_ID, "status": "success"},
        db_session=session,  # type: ignore[arg-type]
    )

    assert persisted is not None
    assert session.committed is True
    assert any("SET LOCAL search_path" in statement for statement in session.statements)
    assert captured["kwargs"] == {
        "workspace_uuid": WORKSPACE_UUID,
        "flow_uuid": str(FLOW_UUID),
        "session_id": 7105,
    }
    assert captured["queue"] == "orch_execute"
    assert captured["routing_key"] == "orch_execute"
