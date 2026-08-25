from __future__ import annotations

import pytest

from app.repositories.orch_channel_events_repository import (
    fetch_channel_event_by_identity,
    fetch_next_pending_channel_event,
    has_channel_event_identity,
    mark_channel_event_processed,
    mark_channel_event_processed_by_identity,
    mark_pending_channel_events_processed,
)


class _Result:
    rowcount = 2

    def __init__(self, row: dict | None = None) -> None:
        self._row = row

    def mappings(self) -> "_Result":
        return self

    def first(self) -> dict | None:
        return self._row

    def scalar(self) -> bool:
        return bool(self._row)


class _Session:
    def __init__(self, row: dict | None = None) -> None:
        self.statement = ""
        self.parameters: dict = {}
        self.row = row

    async def execute(self, statement, parameters):  # noqa: ANN001
        self.statement = str(statement)
        self.parameters = parameters
        return _Result(self.row)


@pytest.mark.asyncio
async def test_mark_pending_channel_events_processed_is_scoped_to_session_and_channel() -> None:
    session = _Session()

    updated = await mark_pending_channel_events_processed(
        session,  # type: ignore[arg-type]
        session_id=6945,
        channel="dialer",
    )

    assert updated == 2
    assert "processed_at = NOW()" in session.statement
    assert "session_id = :session_id" in session.statement
    assert "channel = :channel" in session.statement
    assert "processed_at IS NULL" in session.statement
    assert session.parameters == {"session_id": 6945, "channel": "dialer"}


@pytest.mark.asyncio
async def test_has_channel_event_identity_ignores_event_type_for_dialer_dedupe() -> None:
    session = _Session({"exists": True})

    exists = await has_channel_event_identity(
        session,  # type: ignore[arg-type]
        session_id=6945,
        channel="dialer",
        event_id="GW01-duplicate.1",
    )

    assert exists is True
    assert "event_type" not in session.statement
    assert "event_id = :event_id" in session.statement
    assert session.parameters["event_id"] == "GW01-duplicate.1"


@pytest.mark.asyncio
async def test_fetch_next_pending_channel_event_selects_single_oldest_row_without_long_lock() -> None:
    expected = {"id": 13904, "channel": "dialer", "event_id": "GW02-later.1", "payload": {"x": 1}}
    session = _Session(expected)

    row = await fetch_next_pending_channel_event(
        session,  # type: ignore[arg-type]
        session_id=6946,
        channel="dialer",
    )

    assert row == expected
    assert "processed_at IS NULL" in session.statement
    assert "FOR UPDATE" not in session.statement
    assert "LIMIT 1" in session.statement
    assert session.parameters == {"session_id": 6946, "channel": "dialer"}


@pytest.mark.asyncio
async def test_fetch_channel_event_by_identity_can_recover_processed_cdr() -> None:
    expected = {
        "id": 13904,
        "channel": "dialer",
        "event_id": "GW02-later.1",
        "payload": {"x": 1},
        "processed_at": "2026-08-25T10:07:18.927236+00:00",
    }
    session = _Session(expected)

    row = await fetch_channel_event_by_identity(
        session,  # type: ignore[arg-type]
        session_id=6946,
        channel="dialer",
        event_id="GW02-later.1",
    )

    assert row == expected
    assert "processed_at IS NULL" not in session.statement
    assert "event_id = :event_id" in session.statement
    assert session.parameters["event_id"] == "GW02-later.1"


@pytest.mark.asyncio
async def test_mark_channel_event_processed_is_scoped_to_exact_row() -> None:
    session = _Session()

    updated = await mark_channel_event_processed(
        session,  # type: ignore[arg-type]
        event_row_id=13904,
        session_id=6946,
        channel="dialer",
    )

    assert updated == 2
    assert "id = :event_row_id" in session.statement
    assert "session_id = :session_id" in session.statement
    assert "channel = :channel" in session.statement
    assert "CAST(:discard_reason AS TEXT) IS NOT NULL" in session.statement
    assert session.parameters["event_row_id"] == 13904


@pytest.mark.asyncio
async def test_mark_channel_event_processed_by_identity_records_late_reason() -> None:
    session = _Session()

    updated = await mark_channel_event_processed_by_identity(
        session,  # type: ignore[arg-type]
        session_id=6946,
        channel="dialer",
        event_type="machine",
        event_id="GW01-late.1",
        discard_reason="finish_flow_webhook_already_succeeded",
    )

    assert updated == 2
    assert "event_type = :event_type" in session.statement
    assert "event_id = :event_id" in session.statement
    assert session.parameters["discard_reason"] == "finish_flow_webhook_already_succeeded"
