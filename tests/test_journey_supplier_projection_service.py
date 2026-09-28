from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.services.channel_supplier_v2_service import (
    build_channel_dispatch_correlation_key,
)
from app.services.journey_supplier_projection_service import (
    project_supplier_v2_journey_actions,
)
from app.services.journey_metrics_service import record_journey_channel_action_event
from app.services.migration_service import _run_migration_file


@pytest.mark.asyncio
async def test_supplier_projection_records_each_voice_attempt_and_channel_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = f"orch_journey_supplier_projection_{uuid4().hex}"
    safe_schema = schema.replace('"', '""')
    flow_uuid = uuid4()
    revision_uuid = uuid4()
    session_uuid = uuid4()
    voice_component_ref = uuid4()
    sms_component_ref = uuid4()
    cycle_id = uuid4()
    attempt_one_id = uuid4()
    attempt_two_id = uuid4()
    event_one_id = uuid4()
    event_two_id = uuid4()
    dispatch_id = uuid4()
    now = datetime.now(UTC)
    session_factory = get_session_factory()

    monkeypatch.setattr(
        "app.services.journey_metrics_service.get_current_workspace_schema",
        lambda: schema,
    )
    monkeypatch.setattr(
        "app.services.alarm_service.get_current_workspace_schema",
        lambda: schema,
    )

    try:
        async with session_factory() as db_session:
            async with db_session.begin():
                await db_session.execute(text(f'CREATE SCHEMA "{safe_schema}"'))
                await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
                for statement in (
                    """
                        CREATE TABLE orch_sessions (
                            id BIGSERIAL PRIMARY KEY,
                            uuid UUID NOT NULL UNIQUE
                        )
                    """,
                    """
                        CREATE TABLE orch_sessions_alarms (
                            id BIGSERIAL PRIMARY KEY,
                            session_uuid UUID,
                            flow_uuid UUID,
                            level TEXT,
                            code TEXT,
                            message TEXT,
                            details JSONB,
                            app_name TEXT,
                            created_at TIMESTAMPTZ DEFAULT NOW()
                        )
                    """,
                ):
                    await db_session.execute(text(statement))
                await _run_migration_file(
                    db_session,
                    schema=schema,
                    migration_path="sql/023_create_orch_journey_metrics_tables.sql",
                )
                await _run_migration_file(
                    db_session,
                    schema=schema,
                    migration_path="sql/024_create_orch_journey_workspace_snapshot_state.sql",
                )
                for statement in (
                    """
                        CREATE TABLE contact_supplier_dial_cycles_v2 (
                            id UUID PRIMARY KEY,
                            session_uuid UUID NOT NULL,
                            flow_uuid UUID NOT NULL,
                            component_ref_id TEXT NOT NULL
                        )
                    """,
                    """
                        CREATE TABLE contact_supplier_dial_attempts_v2 (
                            id UUID PRIMARY KEY,
                            cycle_id UUID NOT NULL,
                            attempt_sequence INTEGER NOT NULL,
                            provider_action_id TEXT,
                            provider_unique_id TEXT,
                            outcome TEXT,
                            state TEXT NOT NULL,
                            completed_at TIMESTAMPTZ
                        )
                    """,
                    """
                        CREATE TABLE contact_supplier_dial_events_v2 (
                            id UUID PRIMARY KEY,
                            attempt_id UUID NOT NULL,
                            normalized_outcome TEXT,
                            occurred_at TIMESTAMPTZ,
                            decision TEXT,
                            terminal BOOLEAN,
                            terminal_reason TEXT,
                            processed_at TIMESTAMPTZ
                        )
                    """,
                    """
                        CREATE TABLE contact_supplier_channel_dispatches_v2 (
                            id UUID PRIMARY KEY,
                            session_uuid UUID NOT NULL,
                            flow_uuid UUID NOT NULL,
                            flow_revision_id UUID NOT NULL,
                            component_ref_id TEXT NOT NULL,
                            channel TEXT NOT NULL,
                            dispatch_sequence INTEGER NOT NULL,
                            provider_message_id TEXT,
                            provider_status TEXT,
                            state TEXT NOT NULL,
                            accepted_at TIMESTAMPTZ
                        )
                    """,
                ):
                    await db_session.execute(text(statement))
                source_session_id = (
                    await db_session.execute(
                        text(
                            "INSERT INTO orch_sessions (uuid) VALUES (:uuid) RETURNING id"
                        ),
                        {"uuid": session_uuid},
                    )
                ).scalar_one()
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_journey_sessions (
                            source_session_id, session_uuid, flow_uuid,
                            flow_revision_id, lifecycle_status, started_at,
                            last_progress_at
                        ) VALUES (
                            :source_session_id, :session_uuid, :flow_uuid,
                            :revision_uuid, 'in_progress', :started_at,
                            :started_at
                        )
                        """
                    ),
                    {
                        "source_session_id": source_session_id,
                        "session_uuid": session_uuid,
                        "flow_uuid": flow_uuid,
                        "revision_uuid": revision_uuid,
                        "started_at": now,
                    },
                )
                await db_session.execute(
                    text(
                        """
                        INSERT INTO contact_supplier_dial_cycles_v2 (
                            id, session_uuid, flow_uuid, component_ref_id
                        ) VALUES (
                            :cycle_id, :session_uuid, :flow_uuid, :component_ref_id
                        )
                        """
                    ),
                    {
                        "cycle_id": cycle_id,
                        "session_uuid": session_uuid,
                        "flow_uuid": flow_uuid,
                        "component_ref_id": str(voice_component_ref),
                    },
                )
                await db_session.execute(
                    text(
                        """
                        INSERT INTO contact_supplier_dial_attempts_v2 (
                            id, cycle_id, attempt_sequence, provider_action_id,
                            provider_unique_id, outcome, state, completed_at
                        ) VALUES
                            (
                                :attempt_one_id, :cycle_id, 1, 'action-1', 'call-1',
                                'machine', 'completed', :first_at
                            ),
                            (
                                :attempt_two_id, :cycle_id, 2, 'action-2', 'call-2',
                                'answered', 'completed', :second_at
                            )
                        """
                    ),
                    {
                        "attempt_one_id": attempt_one_id,
                        "attempt_two_id": attempt_two_id,
                        "cycle_id": cycle_id,
                        "first_at": now + timedelta(seconds=1),
                        "second_at": now + timedelta(seconds=2),
                    },
                )
                await db_session.execute(
                    text(
                        """
                        INSERT INTO contact_supplier_dial_events_v2 (
                            id, attempt_id, normalized_outcome, occurred_at,
                            decision, terminal, terminal_reason, processed_at
                        ) VALUES
                            (
                                :event_one_id, :attempt_one_id, 'machine', :first_at,
                                'intermediate', FALSE, 'retry_same_phone', :first_at
                            ),
                            (
                                :event_two_id, :attempt_two_id, 'answered', :second_at,
                                'terminal', TRUE, 'answered', :second_at
                            )
                        """
                    ),
                    {
                        "event_one_id": event_one_id,
                        "event_two_id": event_two_id,
                        "attempt_one_id": attempt_one_id,
                        "attempt_two_id": attempt_two_id,
                        "first_at": now + timedelta(seconds=1),
                        "second_at": now + timedelta(seconds=2),
                    },
                )
                await db_session.execute(
                    text(
                        """
                        INSERT INTO contact_supplier_channel_dispatches_v2 (
                            id, session_uuid, flow_uuid, flow_revision_id,
                            component_ref_id, channel, dispatch_sequence,
                            provider_message_id, provider_status, state, accepted_at
                        ) VALUES (
                            :dispatch_id, :session_uuid, :flow_uuid, :revision_uuid,
                            :sms_component_ref, 'sms', 1,
                            'provider-sms-1', '13', 'accepted', :accepted_at
                        )
                        """
                    ),
                    {
                        "session_uuid": session_uuid,
                        "flow_uuid": flow_uuid,
                        "dispatch_id": dispatch_id,
                        "revision_uuid": revision_uuid,
                        "sms_component_ref": str(sms_component_ref),
                        "accepted_at": now + timedelta(seconds=3),
                    },
                )

                terminal_action = await record_journey_channel_action_event(
                    db_session,
                    source_session_id=source_session_id,
                    flow_uuid=str(flow_uuid),
                    session_uuid=str(session_uuid),
                    channel="voice",
                    source_kind="dialer_supplier_v2_attempt",
                    source_id=str(attempt_two_id),
                    native_status="answered",
                    event_id=str(event_two_id),
                    occurred_at=now + timedelta(seconds=2),
                    component_ref_id=str(voice_component_ref),
                    component_kind="send_with_dialer_handoff",
                    provider_reference="call-2",
                    metadata={"projection_source": "terminal_callback"},
                )
                assert terminal_action is not None
                first = await project_supplier_v2_journey_actions(db_session)
                second = await project_supplier_v2_journey_actions(db_session)
                actions = (
                    await db_session.execute(
                        text(
                            """
                            SELECT channel, source_id, lifecycle_status,
                                   normalized_outcome
                            FROM orch_journey_channel_actions
                            ORDER BY channel, source_id
                            """
                        )
                    )
                ).all()

        assert first.voice_projected == 1
        assert first.channel_projected == 1
        assert second.voice_projected == 0
        assert second.channel_projected == 0
        assert set(actions) == {
            (
                "sms",
                build_channel_dispatch_correlation_key(
                    session_uuid=str(session_uuid),
                    flow_uuid=str(flow_uuid),
                    flow_revision_id=str(revision_uuid),
                    component_ref_id=str(sms_component_ref),
                    channel="sms",
                    dispatch_sequence=1,
                ),
                "accepted",
                "accepted",
            ),
            ("voice", str(attempt_one_id), "completed", "machine"),
            ("voice", str(attempt_two_id), "completed", "answered"),
        }
    finally:
        async with session_factory() as db_session:
            async with db_session.begin():
                await db_session.execute(
                    text(f'DROP SCHEMA IF EXISTS "{safe_schema}" CASCADE')
                )
