from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.repositories.orch_channel_reporting_repository import (
    activate_channel_reporting,
    link_channel_event_to_action,
    register_channel_action,
    update_channel_action,
)


class _Result:
    def __init__(
        self,
        *,
        row: dict | None = None,
        scalar_value: int | None = None,
    ) -> None:
        self._row = row
        self._scalar_value = scalar_value

    def mappings(self) -> "_Result":
        return self

    def first(self) -> dict | None:
        return self._row

    def scalar_one_or_none(self) -> int | None:
        return self._scalar_value


class _Session:
    def __init__(self, results: list[_Result]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict | None]] = []

    async def execute(self, statement, parameters=None):  # noqa: ANN001
        self.calls.append((str(statement), parameters))
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_activate_channel_reporting_sets_database_cutover_only_once() -> None:
    activated_at = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)
    session = _Session(
        [
            _Result(
                row={
                    "singleton_id": 1,
                    "status": "active",
                    "coverage_started_at": activated_at,
                    "activated_at": activated_at,
                    "activated_by": "release-operator",
                    "newly_activated": True,
                }
            )
        ]
    )

    state = await activate_channel_reporting(
        session,  # type: ignore[arg-type]
        activated_by="release-operator",
    )

    statement, parameters = session.calls[0]
    assert state["newly_activated"] is True
    assert "coverage_started_at = NOW()" in statement
    assert "status = 'pending'" in statement
    assert parameters == {"activated_by": "release-operator"}


@pytest.mark.asyncio
async def test_activate_channel_reporting_is_idempotent_after_cutover() -> None:
    activated_at = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)
    session = _Session(
        [
            _Result(row=None),
            _Result(
                row={
                    "singleton_id": 1,
                    "status": "active",
                    "coverage_started_at": activated_at,
                    "activated_at": activated_at,
                    "activated_by": "first-operator",
                }
            ),
        ]
    )

    state = await activate_channel_reporting(
        session,  # type: ignore[arg-type]
        activated_by="second-operator",
    )

    assert state["newly_activated"] is False
    assert state["activated_by"] == "first-operator"
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_register_action_requires_active_state_and_post_cutover_timestamp() -> None:
    requested_at = datetime(2026, 9, 24, 15, 1, tzinfo=timezone.utc)
    expected = {
        "id": 77,
        "uuid": "77777777-7777-4777-8777-777777777777",
        "lifecycle_status": "accepted",
    }
    session = _Session([_Result(row=expected)])

    result = await register_channel_action(
        session,  # type: ignore[arg-type]
        session_id=101,
        session_uuid="11111111-1111-4111-8111-111111111111",
        flow_uuid="22222222-2222-4222-8222-222222222222",
        flow_revision_id="33333333-3333-4333-8333-333333333333",
        component_ref_id="send-sms-1",
        component_kind="send_with_sms",
        channel="sms",
        action_sequence=1,
        source_kind="channel_supplier_v2_dispatch",
        source_id="dispatch-001",
        person_uuid="44444444-4444-4444-8444-444444444444",
        destination_masked="********0806",
        provider_reference="provider-001",
        lifecycle_status="accepted",
        requested_at=requested_at,
    )

    statement, parameters = session.calls[0]
    assert result == expected
    assert "reporting_state.status = 'active'" in statement
    assert "requested_at AS timestamptz) >= reporting_state.coverage_started_at" in statement
    assert "ON CONFLICT (source_kind, source_id) DO UPDATE" in statement
    assert "JOIN orch_sessions session_row" in statement
    assert "orch_channel_actions.flow_revision_id = EXCLUDED.flow_revision_id" in statement
    assert "orch_channel_actions.action_sequence = EXCLUDED.action_sequence" in statement
    assert parameters is not None
    assert parameters["source_id"] == "dispatch-001"


@pytest.mark.asyncio
async def test_callback_update_cannot_create_action() -> None:
    session = _Session([_Result(row=None)])

    result = await update_channel_action(
        session,  # type: ignore[arg-type]
        source_kind="channel_supplier_v2_dispatch",
        source_id="unknown-dispatch",
        lifecycle_status="completed",
        milestone="delivered",
        occurred_at=datetime(2026, 9, 24, 15, 2, tzinfo=timezone.utc),
        native_outcome="delivered",
    )

    statement, _parameters = session.calls[0]
    assert result is None
    assert statement.lstrip().startswith("UPDATE orch_channel_actions")
    assert "INSERT" not in statement
    assert "WHERE source_kind = :source_kind" in statement
    assert "AND source_id = :source_id" in statement
    assert "CAST(:occurred_at AS timestamptz) >= native_outcome_at" in statement


@pytest.mark.asyncio
async def test_channel_event_link_requires_same_session_and_flow() -> None:
    session = _Session([_Result(scalar_value=9001)])

    linked = await link_channel_event_to_action(
        session,  # type: ignore[arg-type]
        event_row_id=9001,
        action_id=77,
    )

    statement, parameters = session.calls[0]
    assert linked is True
    assert "channel_action.session_id = channel_event.session_id" in statement
    assert "channel_action.flow_uuid = channel_event.flow_uuid" in statement
    assert parameters == {"event_row_id": 9001, "action_id": 77}
