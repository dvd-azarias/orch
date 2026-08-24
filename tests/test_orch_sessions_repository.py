from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.repositories.orch_sessions_repository import (
    DialerStatusTimestamps,
    WhatsappStatusTimestamps,
    _compute_effective_whatsapp_limit,
    _derive_state_update,
    fetch_contact_runtime_context_for_session,
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

    async def execute(self, statement, parameters) -> _MappingsResult:  # noqa: ANN001
        self.statement = str(statement)
        self.parameters = parameters
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
