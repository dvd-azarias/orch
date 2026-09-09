from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import workflow_dispatcher_service


class _DummyTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


class _DummySession:
    def in_transaction(self) -> bool:
        return False

    def begin(self) -> _DummyTransaction:
        return _DummyTransaction()

    async def execute(self, *_args, **_kwargs) -> None:
        return None


@pytest.mark.asyncio
async def test_interactive_whatsapp_block_sets_session_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _DummySession()
    state_changes: list[dict] = []

    monkeypatch.setattr(
        workflow_dispatcher_service,
        "get_current_workspace_schema",
        lambda: "ws_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    async def _bootstrap(*_args, **_kwargs) -> None:
        return None

    async def _execute(*_args, **_kwargs) -> SimpleNamespace:
        return SimpleNamespace(stopped_reason="blocked_send_whatsapp_interactive")

    async def _set_state(*_args, **kwargs) -> None:
        state_changes.append(kwargs)

    async def _unexpected_finish(*_args, **_kwargs) -> None:
        pytest.fail("interactive WhatsApp blocking must not finish the session")

    monkeypatch.setattr(workflow_dispatcher_service, "bootstrap_workflow_for_session", _bootstrap)
    monkeypatch.setattr(workflow_dispatcher_service, "execute_workflow_m2_for_session", _execute)
    monkeypatch.setattr(workflow_dispatcher_service, "set_session_state", _set_state)
    monkeypatch.setattr(workflow_dispatcher_service, "mark_session_finished", _unexpected_finish)

    stopped_reason = await workflow_dispatcher_service.advance_session_once(
        session,
        flow_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        session_id=124,
    )

    assert stopped_reason == "blocked_send_whatsapp_interactive"
    assert state_changes == [
        {
            "session_id": 124,
            "state": 1,
            "only_if_not_finished": True,
        }
    ]


@pytest.mark.asyncio
async def test_sms_block_sets_session_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _DummySession()
    state_changes: list[dict] = []

    monkeypatch.setattr(
        workflow_dispatcher_service,
        "get_current_workspace_schema",
        lambda: "ws_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    monkeypatch.setattr(
        workflow_dispatcher_service,
        "bootstrap_workflow_for_session",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        workflow_dispatcher_service,
        "execute_workflow_m2_for_session",
        AsyncMock(return_value=SimpleNamespace(stopped_reason="blocked_send_with_sms")),
    )

    async def _set_state(*_args, **kwargs) -> None:
        state_changes.append(kwargs)

    async def _unexpected_finish(*_args, **_kwargs) -> None:
        pytest.fail("send_with_sms marker-only must leave the session running")

    monkeypatch.setattr(workflow_dispatcher_service, "set_session_state", _set_state)
    monkeypatch.setattr(
        workflow_dispatcher_service,
        "mark_session_finished",
        _unexpected_finish,
    )

    stopped_reason = await workflow_dispatcher_service.advance_session_once(
        session,
        flow_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        session_id=126,
    )

    assert stopped_reason == "blocked_send_with_sms"
    assert state_changes == [
        {
            "session_id": 126,
            "state": 1,
            "only_if_not_finished": True,
        }
    ]


@pytest.mark.asyncio
async def test_rcs_block_sets_session_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _DummySession()
    state_changes: list[dict] = []

    monkeypatch.setattr(
        workflow_dispatcher_service,
        "get_current_workspace_schema",
        lambda: "ws_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    monkeypatch.setattr(
        workflow_dispatcher_service,
        "bootstrap_workflow_for_session",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        workflow_dispatcher_service,
        "execute_workflow_m2_for_session",
        AsyncMock(return_value=SimpleNamespace(stopped_reason="blocked_send_with_rcs")),
    )

    async def _set_state(*_args, **kwargs) -> None:
        state_changes.append(kwargs)

    async def _unexpected_finish(*_args, **_kwargs) -> None:
        pytest.fail("send_with_rcs marker-only must leave the session running")

    monkeypatch.setattr(workflow_dispatcher_service, "set_session_state", _set_state)
    monkeypatch.setattr(
        workflow_dispatcher_service,
        "mark_session_finished",
        _unexpected_finish,
    )

    stopped_reason = await workflow_dispatcher_service.advance_session_once(
        session,
        flow_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        session_id=127,
    )

    assert stopped_reason == "blocked_send_with_rcs"
    assert state_changes == [
        {
            "session_id": 127,
            "state": 1,
            "only_if_not_finished": True,
        }
    ]


@pytest.mark.asyncio
async def test_email_block_sets_session_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _DummySession()
    state_changes: list[dict] = []

    monkeypatch.setattr(
        workflow_dispatcher_service,
        "get_current_workspace_schema",
        lambda: "ws_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    monkeypatch.setattr(
        workflow_dispatcher_service,
        "bootstrap_workflow_for_session",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        workflow_dispatcher_service,
        "execute_workflow_m2_for_session",
        AsyncMock(return_value=SimpleNamespace(stopped_reason="blocked_send_with_email")),
    )

    async def _set_state(*_args, **kwargs) -> None:
        state_changes.append(kwargs)

    async def _unexpected_finish(*_args, **_kwargs) -> None:
        pytest.fail("send_with_email marker-only must leave the session running")

    monkeypatch.setattr(workflow_dispatcher_service, "set_session_state", _set_state)
    monkeypatch.setattr(
        workflow_dispatcher_service,
        "mark_session_finished",
        _unexpected_finish,
    )

    stopped_reason = await workflow_dispatcher_service.advance_session_once(
        session,
        flow_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        session_id=128,
    )

    assert stopped_reason == "blocked_send_with_email"
    assert state_changes == [
        {
            "session_id": 128,
            "state": 1,
            "only_if_not_finished": True,
        }
    ]


@pytest.mark.asyncio
async def test_wait_for_event_block_remains_pending_for_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _DummySession()
    state_changes: list[dict] = []

    monkeypatch.setattr(
        workflow_dispatcher_service,
        "get_current_workspace_schema",
        lambda: "ws_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    async def _bootstrap(*_args, **_kwargs) -> None:
        return None

    async def _execute(*_args, **_kwargs) -> SimpleNamespace:
        return SimpleNamespace(stopped_reason="blocked_wait_for_event")

    async def _set_state(*_args, **kwargs) -> None:
        state_changes.append(kwargs)

    async def _unexpected_finish(*_args, **_kwargs) -> None:
        pytest.fail("wait_for_event must remain pending until callback or timeout")

    monkeypatch.setattr(workflow_dispatcher_service, "bootstrap_workflow_for_session", _bootstrap)
    monkeypatch.setattr(workflow_dispatcher_service, "execute_workflow_m2_for_session", _execute)
    monkeypatch.setattr(workflow_dispatcher_service, "set_session_state", _set_state)
    monkeypatch.setattr(workflow_dispatcher_service, "mark_session_finished", _unexpected_finish)

    stopped_reason = await workflow_dispatcher_service.advance_session_once(
        session,
        flow_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        session_id=125,
    )

    assert stopped_reason == "blocked_wait_for_event"
    assert state_changes == [
        {
            "session_id": 125,
            "state": 0,
            "only_if_not_finished": True,
        }
    ]


@pytest.mark.asyncio
async def test_api_call_missing_url_marks_session_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _DummySession()
    finished: list[dict] = []

    monkeypatch.setattr(
        workflow_dispatcher_service,
        "get_current_workspace_schema",
        lambda: "ws_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    monkeypatch.setattr(
        workflow_dispatcher_service,
        "bootstrap_workflow_for_session",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        workflow_dispatcher_service,
        "execute_workflow_m2_for_session",
        AsyncMock(return_value=SimpleNamespace(stopped_reason="api_call_missing_url")),
    )

    async def _finish(*_args, **kwargs) -> None:
        finished.append(kwargs)

    monkeypatch.setattr(workflow_dispatcher_service, "mark_session_finished", _finish)

    stopped_reason = await workflow_dispatcher_service.advance_session_once(
        session,
        flow_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        session_id=126,
    )

    assert stopped_reason == "api_call_missing_url"
    assert finished == [{"session_id": 126}]
