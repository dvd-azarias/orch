from __future__ import annotations

import pytest

from app.repositories.send_with_sms_repository import assign_sms_routing_for_session


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


BASE = {
    "flow_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "session_id": 123,
    "contact_list_member_id": 77,
    "contact_list_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "mailing_id": 1140,
    "person_uuid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
}


@pytest.mark.asyncio
async def test_assign_sms_is_guarded_by_exact_active_session_and_sms_member() -> None:
    session = _RecordingSession(
        {
            "id": 77,
            "linked_actuator": "sms",
            "previous_linked_actuator": None,
        }
    )

    assignment = await assign_sms_routing_for_session(
        session,  # type: ignore[arg-type]
        **BASE,
    )

    assert assignment == {
        "contact_list_member_id": 77,
        "linked_actuator": "sms",
        "mode": "marked",
    }
    assert "os.id = :session_id" in session.statement
    assert "os.flow_uuid = CAST(:flow_uuid AS uuid)" in session.statement
    assert "os.state <> 3" in session.statement
    assert "os.ended_at IS NULL" in session.statement
    assert "os.unassigned_at IS NULL" in session.statement
    assert "os.entity = clm.contact_identifier" in session.statement
    assert "BTRIM(os.entity_address) = BTRIM(clm.contact_channel_address)" in session.statement
    assert "clm.id = :contact_list_member_id" in session.statement
    assert "clm.contact_list_id = CAST(:contact_list_id AS uuid)" in session.statement
    assert "clm.mailing_id = CAST(:mailing_id AS bigint)" in session.statement
    assert "clm.person_uuid = CAST(:person_uuid AS uuid)" in session.statement
    assert "LOWER(BTRIM(COALESCE(clm.contact_channel_type, ''))) = 'sms'" in session.statement
    assert "FOR UPDATE OF clm, os" in session.statement
    assert "linked_actuator = 'sms'" in session.statement
    assert session.parameters == BASE


@pytest.mark.asyncio
async def test_assign_sms_reports_already_marked_without_exposing_payload() -> None:
    session = _RecordingSession(
        {
            "id": 77,
            "linked_actuator": "sms",
            "previous_linked_actuator": "sms",
        }
    )

    assignment = await assign_sms_routing_for_session(
        session,  # type: ignore[arg-type]
        **BASE,
    )

    assert assignment == {
        "contact_list_member_id": 77,
        "linked_actuator": "sms",
        "mode": "already_marked",
    }
    assert set(assignment) == {"contact_list_member_id", "linked_actuator", "mode"}


@pytest.mark.asyncio
async def test_assign_sms_returns_none_when_guarded_update_matches_nothing() -> None:
    session = _RecordingSession(None)

    assignment = await assign_sms_routing_for_session(
        session,  # type: ignore[arg-type]
        **BASE,
    )

    assert assignment is None
