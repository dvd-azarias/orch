from __future__ import annotations

import pytest

from app.repositories.select_contact_channel_repository import (
    fetch_select_contact_channel_candidate,
    rebind_person_session_to_contact_channel,
)


class _MappingsResult:
    def __init__(self, row: dict | None) -> None:
        self.row = row

    def mappings(self) -> "_MappingsResult":
        return self

    def first(self) -> dict | None:
        return self.row

    def scalar_one_or_none(self):
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


BASE = {
    "flow_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "session_id": 123,
    "contact_list_member_id": 77,
    "contact_list_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "mailing_id": 1140,
    "person_uuid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
    "channel_type": "voice",
    "channel_label": None,
}


@pytest.mark.asyncio
async def test_channel_candidate_is_pinned_to_source_member_and_session_address() -> (
    None
):
    session = _RecordingSession({"contact_list_member_id": 77})

    row = await fetch_select_contact_channel_candidate(
        session,  # type: ignore[arg-type]
        session_scope="channel",
        **BASE,
    )

    assert row == {"contact_list_member_id": 77}
    assert "clm.id = :contact_list_member_id" in session.statement
    assert (
        "BTRIM(clm.contact_channel_address) = BTRIM(os.entity_address)"
        in session.statement
    )
    assert "source_channel.is_primary" in session.statement
    assert (
        "ORDER BY COALESCE(source_channel.is_primary, false) DESC, clm.id ASC"
        in session.statement
    )
    assert "FOR UPDATE OF clm, os" in session.statement
    assert "linked_actuator" not in session.statement


@pytest.mark.asyncio
async def test_person_candidate_can_change_member_but_preserves_person_list_and_mailing() -> (
    None
):
    session = _RecordingSession({"contact_list_member_id": 88})

    row = await fetch_select_contact_channel_candidate(
        session,  # type: ignore[arg-type]
        session_scope="person",
        **{**BASE, "channel_type": "whatsapp", "channel_label": "celular"},
    )

    assert row == {"contact_list_member_id": 88}
    assert "clm.id = :contact_list_member_id" not in session.statement
    assert (
        "BTRIM(clm.contact_channel_address) = BTRIM(os.entity_address)"
        not in session.statement
    )
    assert "clm.person_uuid = CAST(:person_uuid AS uuid)" in session.statement
    assert "clm.contact_list_id = CAST(:contact_list_id AS uuid)" in session.statement
    assert "clm.mailing_id = CAST(:mailing_id AS bigint)" in session.statement
    assert session.parameters["channel_type"] == "whatsapp"
    assert session.parameters["channel_label"] == "celular"


@pytest.mark.asyncio
async def test_sms_candidate_accepts_phone_source_without_reclassifying_it() -> None:
    session = _RecordingSession(
        {
            "contact_list_member_id": 77,
            "contact_channel_type": "voice",
        }
    )

    row = await fetch_select_contact_channel_candidate(
        session,  # type: ignore[arg-type]
        session_scope="channel",
        **{**BASE, "channel_type": "sms"},
    )

    assert row == {
        "contact_list_member_id": 77,
        "contact_channel_type": "voice",
    }
    assert ":channel_type = 'sms'" in session.statement
    assert "= 'voice'" in session.statement
    assert session.parameters["channel_type"] == "sms"


@pytest.mark.asyncio
async def test_rcs_candidate_uses_exact_persisted_type_without_phone_fallback() -> None:
    session = _RecordingSession(
        {"contact_list_member_id": 77, "contact_channel_type": "rcs"}
    )

    row = await fetch_select_contact_channel_candidate(
        session,  # type: ignore[arg-type]
        session_scope="channel",
        **{**BASE, "channel_type": "rcs"},
    )

    assert row == {"contact_list_member_id": 77, "contact_channel_type": "rcs"}
    assert session.parameters["channel_type"] == "rcs"
    assert ":channel_type = 'sms'" in session.statement
    assert ":channel_type = 'rcs'" not in session.statement


@pytest.mark.asyncio
async def test_person_rebind_validates_scope_and_active_session_without_actuator_write() -> (
    None
):
    session = _RecordingSession(123)

    rebound = await rebind_person_session_to_contact_channel(
        session,  # type: ignore[arg-type]
        flow_uuid=BASE["flow_uuid"],
        session_id=BASE["session_id"],
        contact_list_member_id=88,
        contact_list_id=BASE["contact_list_id"],
        mailing_id=BASE["mailing_id"],
        person_uuid=BASE["person_uuid"],
    )

    assert rebound is True
    assert "entity_address = BTRIM(clm.contact_channel_address)" in session.statement
    assert "clm.contact_identifier = os.entity" in session.statement
    assert "conflicting.state <> 3" in session.statement
    assert "conflicting.unassigned_at IS NULL" in session.statement
    assert "linked_actuator" not in session.statement


@pytest.mark.asyncio
async def test_person_rebind_returns_false_when_guarded_update_matches_nothing() -> (
    None
):
    session = _RecordingSession(None)

    rebound = await rebind_person_session_to_contact_channel(
        session,  # type: ignore[arg-type]
        flow_uuid=BASE["flow_uuid"],
        session_id=BASE["session_id"],
        contact_list_member_id=88,
        contact_list_id=BASE["contact_list_id"],
        mailing_id=BASE["mailing_id"],
        person_uuid=BASE["person_uuid"],
    )

    assert rebound is False
