from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.services.journey_metrics_service import (
    finalize_journey_session_metrics,
    initialize_journey_session_metrics,
    record_journey_channel_action_event,
    record_journey_channel_action_requested,
    record_journey_component_entry,
    record_journey_component_transition,
)
from app.services.migration_service import _run_migration_file


BASE_MIGRATION_PATH = "sql/023_create_orch_journey_metrics_tables.sql"
STATE_MIGRATION_PATH = "sql/024_create_orch_journey_workspace_snapshot_state.sql"


async def _prepare_schema(db_session, schema: str) -> None:
    safe_schema = schema.replace('"', '""')
    await db_session.execute(text(f'CREATE SCHEMA "{safe_schema}"'))
    await db_session.execute(
        text(
            f"""
            CREATE TABLE "{safe_schema}".orch_sessions (
                id BIGSERIAL PRIMARY KEY,
                uuid UUID NOT NULL UNIQUE,
                flow_uuid UUID NOT NULL,
                state INTEGER NOT NULL DEFAULT 2,
                started_at TIMESTAMPTZ,
                ended_at TIMESTAMPTZ,
                abandoned_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    await _run_migration_file(
        db_session,
        schema=schema,
        migration_path=BASE_MIGRATION_PATH,
    )


@pytest.mark.asyncio
async def test_session_stage_writer_is_idempotent_and_tracks_resume_and_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = f"orch_journey_writer_test_{uuid4().hex}"
    session_factory = get_session_factory()
    flow_uuid = uuid4()
    revision_uuid = uuid4()
    session_uuid = uuid4()
    first_component_uuid = uuid4()
    second_component_uuid = uuid4()
    started_at = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "app.services.journey_metrics_service.get_current_workspace_schema",
        lambda: schema,
    )
    monkeypatch.setattr(
        "app.services.alarm_service.get_current_workspace_schema",
        lambda: schema,
    )

    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            await _prepare_schema(db_session, schema)
            await _run_migration_file(
                db_session,
                schema=schema,
                migration_path=STATE_MIGRATION_PATH,
            )
            source_session_id = (
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_sessions (
                            uuid,
                            flow_uuid,
                            state,
                            started_at,
                            created_at
                        )
                        VALUES (
                            :session_uuid,
                            :flow_uuid,
                            2,
                            :started_at,
                            :started_at
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "session_uuid": session_uuid,
                        "flow_uuid": flow_uuid,
                        "started_at": started_at,
                    },
                )
            ).scalar_one()

            context = await initialize_journey_session_metrics(
                db_session,
                source_session_id=source_session_id,
                flow_uuid=str(flow_uuid),
                flow_revision_id=str(revision_uuid),
                session_scope="person",
            )
            assert context is not None
            repeated_context = await initialize_journey_session_metrics(
                db_session,
                source_session_id=source_session_id,
                flow_uuid=str(flow_uuid),
                flow_revision_id=str(revision_uuid),
                session_scope="person",
            )
            assert repeated_context == context

            first_component = {
                "ref_id": str(first_component_uuid),
                "parameters": {"stage": {"id": "entrada"}},
            }
            await record_journey_component_entry(
                db_session,
                context=context,
                component=first_component,
                card_cursor=str(first_component_uuid),
                component_kind="create_contact",
                occurred_at=started_at + timedelta(seconds=1),
            )
            await record_journey_component_entry(
                db_session,
                context=context,
                component=first_component,
                card_cursor=str(first_component_uuid),
                component_kind="create_contact",
                occurred_at=started_at + timedelta(seconds=2),
            )
            await record_journey_component_transition(
                db_session,
                context=context,
                component=first_component,
                card_cursor=str(first_component_uuid),
                occurred_at=started_at + timedelta(seconds=3),
            )
            await record_journey_component_transition(
                db_session,
                context=context,
                component=first_component,
                card_cursor=str(first_component_uuid),
                occurred_at=started_at + timedelta(seconds=4),
            )

            second_component = {
                "ref_id": str(second_component_uuid),
                "parameters": {"stage": "abordagem"},
            }
            await record_journey_component_entry(
                db_session,
                context=context,
                component=second_component,
                card_cursor=str(second_component_uuid),
                component_kind="send_with_dialer_handoff",
                occurred_at=started_at + timedelta(seconds=5),
            )
            await finalize_journey_session_metrics(
                db_session,
                context=context,
                stopped_reason="blocked_send_with_dialer_handoff",
                occurred_at=started_at + timedelta(seconds=6),
            )
            await record_journey_component_entry(
                db_session,
                context=context,
                component=second_component,
                card_cursor=str(second_component_uuid),
                component_kind="send_with_dialer_handoff",
                occurred_at=started_at + timedelta(seconds=7),
            )
            await record_journey_component_transition(
                db_session,
                context=context,
                component=second_component,
                card_cursor=str(second_component_uuid),
                occurred_at=started_at + timedelta(seconds=8),
            )
            await record_journey_component_entry(
                db_session,
                context=context,
                component=first_component,
                card_cursor=str(first_component_uuid),
                component_kind="create_contact",
                occurred_at=started_at + timedelta(seconds=9),
            )
            await record_journey_component_transition(
                db_session,
                context=context,
                component=first_component,
                card_cursor=str(first_component_uuid),
                occurred_at=started_at + timedelta(seconds=10),
            )
            await db_session.execute(
                text(
                    """
                    UPDATE orch_sessions
                    SET state = 3,
                        ended_at = :ended_at
                    WHERE id = :source_session_id
                    """
                ),
                {
                    "source_session_id": source_session_id,
                    "ended_at": started_at + timedelta(seconds=11),
                },
            )
            await finalize_journey_session_metrics(
                db_session,
                context=context,
                stopped_reason="finished_by_component",
                occurred_at=started_at + timedelta(seconds=11),
            )

            projection = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            lifecycle_status,
                            current_stage,
                            highest_stage,
                            terminal_class
                        FROM orch_journey_sessions
                        WHERE source_session_id = :source_session_id
                        """
                    ),
                    {"source_session_id": source_session_id},
                )
            ).mappings().one()
            assert dict(projection) == {
                "lifecycle_status": "completed",
                "current_stage": "entrada",
                "highest_stage": "abordagem",
                "terminal_class": "completed",
            }
            visits = (
                await db_session.execute(
                    text(
                        """
                        SELECT stage_id, visit_number, exit_kind, exited_at IS NOT NULL
                        FROM orch_journey_stage_visits
                        ORDER BY id
                        """
                    )
                )
            ).all()
            assert visits == [
                ("entrada", 1, "transition", True),
                ("abordagem", 1, "transition", True),
                ("entrada", 2, "transition", True),
            ]
            state = (
                await db_session.execute(
                    text(
                        """
                        SELECT dirty_generation, built_generation, dirty_since
                        FROM orch_journey_workspace_snapshot_state
                        """
                    )
                )
            ).mappings().one()
            assert state["dirty_generation"] == 10
            assert state["built_generation"] == 0
            assert state["dirty_since"] is not None
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_writer_failure_rolls_back_telemetry_without_touching_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = f"orch_journey_failure_test_{uuid4().hex}"
    session_factory = get_session_factory()
    flow_uuid = uuid4()
    revision_uuid = uuid4()
    session_uuid = uuid4()
    monkeypatch.setattr(
        "app.services.journey_metrics_service.get_current_workspace_schema",
        lambda: schema,
    )
    monkeypatch.setattr(
        "app.services.alarm_service.get_current_workspace_schema",
        lambda: schema,
    )

    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            await _prepare_schema(db_session, schema)
            source_session_id = (
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_sessions (uuid, flow_uuid, state)
                        VALUES (:session_uuid, :flow_uuid, 2)
                        RETURNING id
                        """
                    ),
                    {"session_uuid": session_uuid, "flow_uuid": flow_uuid},
                )
            ).scalar_one()

            context = await initialize_journey_session_metrics(
                db_session,
                source_session_id=source_session_id,
                flow_uuid=str(flow_uuid),
                flow_revision_id=str(revision_uuid),
                session_scope="person",
            )

            assert context is None
            source_state = (
                await db_session.execute(
                    text("SELECT state FROM orch_sessions WHERE id = :session_id"),
                    {"session_id": source_session_id},
                )
            ).scalar_one()
            projection_count = (
                await db_session.execute(
                    text("SELECT COUNT(*) FROM orch_journey_sessions")
                )
            ).scalar_one()
            assert source_state == 2
            assert projection_count == 0
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_channel_actions_are_idempotent_and_keep_one_voice_action_per_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = f"orch_journey_action_test_{uuid4().hex}"
    session_factory = get_session_factory()
    flow_uuid = uuid4()
    revision_uuid = uuid4()
    session_uuid = uuid4()
    sms_component_uuid = uuid4()
    voice_component_uuid = uuid4()
    started_at = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "app.services.journey_metrics_service.get_current_workspace_schema",
        lambda: schema,
    )
    monkeypatch.setattr(
        "app.services.alarm_service.get_current_workspace_schema",
        lambda: schema,
    )

    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            await _prepare_schema(db_session, schema)
            await _run_migration_file(
                db_session,
                schema=schema,
                migration_path=STATE_MIGRATION_PATH,
            )
            source_session_id = (
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_sessions (
                            uuid, flow_uuid, state, started_at, created_at
                        )
                        VALUES (:session_uuid, :flow_uuid, 2, :started_at, :started_at)
                        RETURNING id
                        """
                    ),
                    {
                        "session_uuid": session_uuid,
                        "flow_uuid": flow_uuid,
                        "started_at": started_at,
                    },
                )
            ).scalar_one()
            context = await initialize_journey_session_metrics(
                db_session,
                source_session_id=source_session_id,
                flow_uuid=str(flow_uuid),
                flow_revision_id=str(revision_uuid),
                session_scope="person",
            )
            assert context is not None

            sms_component = {
                "ref_id": str(sms_component_uuid),
                "parameters": {"stage": "abordagem"},
            }
            await record_journey_component_entry(
                db_session,
                context=context,
                component=sms_component,
                card_cursor=str(sms_component_uuid),
                component_kind="send_with_sms",
                occurred_at=started_at + timedelta(seconds=1),
            )
            first_action = await record_journey_channel_action_requested(
                db_session,
                context=context,
                component=sms_component,
                component_kind="send_with_sms",
                channel="sms",
                source_kind="channel_supplier_v2_dispatch",
                source_id="sms-correlation-1",
                action_sequence=1,
                requested_at=started_at + timedelta(seconds=2),
            )
            repeated_action = await record_journey_channel_action_requested(
                db_session,
                context=context,
                component=sms_component,
                component_kind="send_with_sms",
                channel="sms",
                source_kind="channel_supplier_v2_dispatch",
                source_id="sms-correlation-1",
                action_sequence=1,
                requested_at=started_at + timedelta(seconds=2),
            )
            assert first_action is not None
            assert repeated_action is not None
            assert repeated_action.action_id == first_action.action_id
            assert repeated_action.created is False

            for native_status, event_id, offset in (
                ("sent", "provider-sms-1", 3),
                ("delivered", "provider-sms-1", 5),
                ("delivered", "provider-sms-1", 5),
            ):
                await record_journey_channel_action_event(
                    db_session,
                    source_session_id=source_session_id,
                    flow_uuid=str(flow_uuid),
                    session_uuid=str(session_uuid),
                    channel="sms",
                    source_kind="channel_supplier_v2_dispatch",
                    source_id="sms-correlation-1",
                    native_status=native_status,
                    event_id=event_id,
                    occurred_at=started_at + timedelta(seconds=offset),
                    component_ref_id=str(sms_component_uuid),
                    component_kind="send_with_sms",
                    action_sequence=1,
                    provider_reference=event_id,
                )

            voice_component = {
                "ref_id": str(voice_component_uuid),
                "parameters": {"stage": "abordagem"},
            }
            await record_journey_component_entry(
                db_session,
                context=context,
                component=voice_component,
                card_cursor=str(voice_component_uuid),
                component_kind="send_with_dialer_handoff",
                occurred_at=started_at + timedelta(seconds=6),
            )
            for attempt_id, event_id, outcome, offset in (
                ("attempt-1", "voice-event-1", "machine", 7),
                ("attempt-2", "voice-event-2", "answered", 8),
            ):
                await record_journey_channel_action_event(
                    db_session,
                    source_session_id=source_session_id,
                    flow_uuid=str(flow_uuid),
                    session_uuid=str(session_uuid),
                    channel="voice",
                    source_kind="dialer_supplier_v2_attempt",
                    source_id=attempt_id,
                    native_status=outcome,
                    event_id=event_id,
                    occurred_at=started_at + timedelta(seconds=offset),
                    component_ref_id=str(voice_component_uuid),
                    component_kind="send_with_dialer_handoff",
                    provider_reference=attempt_id,
                )

            actions = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            channel,
                            action_sequence,
                            lifecycle_status,
                            normalized_outcome,
                            provider_reference_hash IS NOT NULL AS has_provider_hash
                        FROM orch_journey_channel_actions
                        ORDER BY channel, action_sequence
                        """
                    )
                )
            ).all()
            assert actions == [
                ("sms", 1, "delivered", "delivered", True),
                ("voice", 1, "completed", "machine", True),
                ("voice", 2, "completed", "answered", True),
            ]
            event_count = (
                await db_session.execute(
                    text("SELECT COUNT(*) FROM orch_journey_channel_action_events")
                )
            ).scalar_one()
            assert event_count == 4
            dirty_generation = (
                await db_session.execute(
                    text(
                        "SELECT dirty_generation FROM orch_journey_workspace_snapshot_state"
                    )
                )
            ).scalar_one()
            assert dirty_generation == 8
        finally:
            await transaction.rollback()
