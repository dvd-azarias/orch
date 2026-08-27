from __future__ import annotations

from datetime import datetime, timezone

import pytest
import app.repositories.orch_sessions_repository as repository

from app.repositories.orch_sessions_repository import (
    DialerStatusTimestamps,
    WhatsappStatusTimestamps,
    _compute_effective_whatsapp_limit,
    _derive_state_update,
    apply_switch_bot_flow_callback,
    fetch_contact_runtime_context_for_session,
    fetch_session_webhook_snapshot,
    persist_run_flow_event_for_recent_entity_address,
    set_session_cdr,
)


def test_derive_state_update_dialer_answered_does_not_finish_session() -> None:
    dialer_timestamps = DialerStatusTimestamps(
        dialer_answered_at=datetime.now(timezone.utc),
        dialer_busy_at=None,
        dialer_rejected_at=None,
        dialer_invalid_number_at=None,
        dialer_not_answered_at=None,
        dialer_failed_at=None,
    )
    whatsapp_timestamps = WhatsappStatusTimestamps(
        whatsapp_sent_at=None,
        whatsapp_delivered_at=None,
        whatsapp_read_at=None,
        whatsapp_failed_at=None,
    )

    result = _derive_state_update(
        app_name="DialerApp",
        whatsapp_timestamps=whatsapp_timestamps,
        dialer_timestamps=dialer_timestamps,
    )

    assert result.state == 1
    assert result.ended_at is None


def test_compute_effective_whatsapp_limit_with_unlimited_minus_one() -> None:
    assert (
        _compute_effective_whatsapp_limit(
            allowed_limit_raw=-1,
            percentual_consumo=50,
        )
        is None
    )


def test_compute_effective_whatsapp_limit_with_percentual_zero() -> None:
    assert (
        _compute_effective_whatsapp_limit(
            allowed_limit_raw=1000,
            percentual_consumo=0,
        )
        is None
    )


class _MappingsResult:
    def __init__(self, row: dict | None) -> None:
        self.row = row

    def mappings(self) -> "_MappingsResult":
        return self

    def first(self) -> dict | None:
        return self.row


class _RecordingSession:
    def __init__(self, row: dict | None) -> None:
        self.row = row
        self.statement = ""
        self.parameters: dict = {}

    async def execute(self, statement, parameters=None) -> _MappingsResult:  # noqa: ANN001
        self.statement = str(statement)
        self.parameters = parameters or {}
        return _MappingsResult(self.row)


@pytest.mark.asyncio
async def test_fetch_contact_context_uses_native_list_and_mailing_predicates() -> None:
    session = _RecordingSession({"contact_list_member_id": 10655})

    row = await fetch_contact_runtime_context_for_session(
        session,
        flow_uuid="3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17",
        session_id=6937,
        contact_list_id="dc7dc1c1-2c98-42e9-a788-5d186f458daa",
        mailing_id=1115,
    )

    assert row == {"contact_list_member_id": 10655}
    assert "clm.contact_list_id = CAST(:contact_list_id AS uuid)" in session.statement
    assert "clm.mailing_id = CAST(:mailing_id AS bigint)" in session.statement
    assert "contact_list_id::text =" not in session.statement
    assert "mailing_id::text =" not in session.statement
    assert session.parameters["contact_list_id"] == "dc7dc1c1-2c98-42e9-a788-5d186f458daa"
    assert session.parameters["mailing_id"] == 1115


@pytest.mark.asyncio
async def test_fetch_contact_context_cross_validates_lower_selectors_with_member_id() -> None:
    session = _RecordingSession(None)

    await fetch_contact_runtime_context_for_session(
        session,
        flow_uuid="3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17",
        session_id=6937,
        contact_list_member_id=10655,
        contact_list_id="dc7dc1c1-2c98-42e9-a788-5d186f458daa",
        mailing_id=1115,
    )

    assert "clm.id = :contact_list_member_id" in session.statement
    assert "clm.contact_list_id = CAST(:contact_list_id AS uuid)" in session.statement
    assert "clm.mailing_id = CAST(:mailing_id AS bigint)" in session.statement


@pytest.mark.asyncio
async def test_fetch_contact_context_without_scope_preserves_legacy_query() -> None:
    session = _RecordingSession(None)

    await fetch_contact_runtime_context_for_session(
        session,
        flow_uuid="3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17",
        session_id=6937,
    )

    assert "clm.id = :contact_list_member_id" not in session.statement
    assert "clm.contact_list_id = CAST(:contact_list_id AS uuid)" not in session.statement
    assert "clm.mailing_id = CAST(:mailing_id AS bigint)" not in session.statement


@pytest.mark.asyncio
async def test_set_session_cdr_overwrites_with_single_object() -> None:
    session = _RecordingSession(None)
    cdr = {"hangup": {"Disposition": "ANSWERED"}}

    stored = await set_session_cdr(session, session_id=6941, cdr=cdr)

    assert stored is False
    assert "jsonb_set" in session.statement
    assert "||" not in session.statement
    assert "runtime_variables->'finish_flow_webhook'->>'success'" not in session.statement
    assert "RETURNING id" in session.statement
    assert session.parameters["cdr"] == '{"hangup": {"Disposition": "ANSWERED"}}'


@pytest.mark.asyncio
async def test_fetch_session_webhook_snapshot_uses_complete_database_row() -> None:
    session = _RecordingSession({"session_data": {"id": 6941, "entity_address": "5511975620806"}})

    snapshot = await fetch_session_webhook_snapshot(session, session_id=6941)

    assert "to_jsonb(session_row)" in session.statement
    assert snapshot == {"id": 6941, "entity_address": "5511975620806"}


class _CallbackResult:
    def __init__(self, *, scalar_value=None, row=None) -> None:  # noqa: ANN001
        self.scalar_value = scalar_value
        self.row = row

    def scalar(self):  # noqa: ANN201
        return self.scalar_value

    def mappings(self) -> "_CallbackResult":
        return self

    def first(self):  # noqa: ANN201
        return self.row


class _CallbackSession:
    def __init__(self, results: list[_CallbackResult]) -> None:
        self.results = results

    async def execute(self, _statement, _parameters=None) -> _CallbackResult:  # noqa: ANN001
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_apply_switch_bot_flow_callback_marks_handoff_completed(monkeypatch) -> None:
    runtime_variables = {
        "workflow_v2": {
            "switch_bot_flow": {
                "status": "active",
                "target_session_id": "target-session-1",
            }
        }
    }
    session = _CallbackSession(
        [
            _CallbackResult(scalar_value=7001),
            _CallbackResult(),
            _CallbackResult(
                row={
                    "id": 7001,
                    "uuid": "orch-session-uuid",
                    "state": 1,
                    "runtime_variables": runtime_variables,
                    "last_card_uuid": "11111111-1111-1111-1111-111111111111",
                    "next_card_uuid": "11111111-1111-1111-1111-111111111111",
                }
            ),
        ]
    )
    captured: dict = {}

    async def _replace(*_args, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)

    monkeypatch.setattr(repository, "replace_session_workflow_state", _replace)
    result = await apply_switch_bot_flow_callback(
        session,  # type: ignore[arg-type]
        flow_uuid="d3c79b7c-4726-46d0-a787-d99e590242b7",
        target_session_id="target-session-1",
        terminal_status="completed",
        callback_payload={"session_id": "target-session-1", "status": "success"},
    )

    assert result == {
        "session_id": 7001,
        "session_uuid": "orch-session-uuid",
        "state": 1,
        "idempotent": False,
        "status": "completed",
    }
    assert captured["runtime_variables"]["workflow_v2"]["switch_bot_flow"]["status"] == "completed"


@pytest.mark.asyncio
async def test_apply_switch_bot_flow_callback_keeps_first_terminal_status(monkeypatch) -> None:
    runtime_variables = {
        "workflow_v2": {
            "switch_bot_flow": {
                "status": "completed",
                "target_session_id": "target-session-1",
            }
        }
    }
    session = _CallbackSession(
        [
            _CallbackResult(scalar_value=7001),
            _CallbackResult(),
            _CallbackResult(
                row={
                    "id": 7001,
                    "uuid": "orch-session-uuid",
                    "state": 3,
                    "runtime_variables": runtime_variables,
                    "last_card_uuid": "11111111-1111-1111-1111-111111111111",
                    "next_card_uuid": "11111111-1111-1111-1111-111111111111",
                }
            ),
        ]
    )

    async def _unexpected_replace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("callback terminal tardio não deve alterar o handoff")

    monkeypatch.setattr(repository, "replace_session_workflow_state", _unexpected_replace)
    result = await apply_switch_bot_flow_callback(
        session,  # type: ignore[arg-type]
        flow_uuid="d3c79b7c-4726-46d0-a787-d99e590242b7",
        target_session_id="target-session-1",
        terminal_status="failed",
        callback_payload={"session_id": "target-session-1", "status": "failed"},
    )

    assert result == {
        "session_id": 7001,
        "session_uuid": "orch-session-uuid",
        "state": 3,
        "idempotent": True,
        "status": "completed",
    }


@pytest.mark.asyncio
async def test_recent_event_does_not_reopen_successful_finish_webhook_session_by_default() -> None:
    session = _RecordingSession(None)

    result = await persist_run_flow_event_for_recent_entity_address(
        session,
        flow_uuid="3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17",
        app_name="DialerApp",
        entity_address="5511975620806",
        payload={"uniqueid": "GW01-late.1"},
        extracted={"entity": "action-late"},
        event_name="hangup",
        event_result="hangup",
        resume_card_uuid="3fcb8a0e-cd5f-4a9d-a941-e04951882bce",
        correlation_window_hours=36,
    )

    assert result is None
    assert "runtime_variables->'finish_flow_webhook'->>'success'" in session.statement
    assert "<> 'true'" in session.statement
    assert session.parameters["allow_confirmed_finish_flow_webhook"] is False


@pytest.mark.asyncio
async def test_recent_dialer_event_reopens_successful_finish_webhook_session_when_explicit() -> None:
    session = _RecordingSession(None)

    result = await persist_run_flow_event_for_recent_entity_address(
        session,
        flow_uuid="3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17",
        app_name="DialerApp",
        entity_address="5511975620806",
        payload={"uniqueid": "GW01-retry.1"},
        extracted={"entity": "action-retry"},
        event_name="hangup",
        event_result="hangup",
        resume_card_uuid="3fcb8a0e-cd5f-4a9d-a941-e04951882bce",
        correlation_window_hours=36,
        allow_confirmed_finish_flow_webhook=True,
    )

    assert result is None
    assert ":allow_confirmed_finish_flow_webhook" in session.statement
    assert session.parameters["allow_confirmed_finish_flow_webhook"] is True
