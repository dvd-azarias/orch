from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.services.journey_workspace_aggregation_service import (
    build_journey_workspace_snapshot_payload,
    build_journey_workspace_view,
)
from app.services.journey_workspace_snapshot_service import _canonical_snapshot
from app.services.migration_service import _run_migration_file, _split_sql_statements


async def _execute_statements(db_session, sql: str, params: dict | None = None) -> None:
    for statement in _split_sql_statements(sql):
        await db_session.execute(text(statement), params or {})


@pytest.mark.asyncio
async def test_workspace_aggregation_is_filterable_complete_and_pii_safe() -> None:
    schema = f"orch_journey_aggregate_test_{uuid4().hex}"
    safe_schema = schema.replace('"', '""')
    workspace_uuid = uuid4()
    first_flow_uuid = uuid4()
    second_flow_uuid = uuid4()
    first_revision_uuid = uuid4()
    second_revision_uuid = uuid4()
    first_session_uuid = uuid4()
    second_session_uuid = uuid4()
    first_component_uuid = uuid4()
    second_component_uuid = uuid4()
    generated_at = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            await db_session.execute(text(f'CREATE SCHEMA "{safe_schema}"'))
            await _execute_statements(
                db_session,
                f"""
                    CREATE TABLE "{safe_schema}".orch_sessions (
                        id BIGSERIAL PRIMARY KEY,
                        uuid UUID NOT NULL UNIQUE
                    );
                    CREATE TABLE "{safe_schema}".orch_sessions_alarms (
                        id BIGSERIAL PRIMARY KEY,
                        flow_uuid UUID,
                        code TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL
                    );
                    CREATE TABLE "{safe_schema}".flow_v2 (
                        id UUID PRIMARY KEY,
                        slug TEXT,
                        display_name TEXT
                    );
                    CREATE TABLE "{safe_schema}".flow_v2_revision (
                        id UUID PRIMARY KEY,
                        flow_id UUID NOT NULL,
                        version INTEGER
                    )
                    """,
            )
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

            await _execute_statements(
                db_session,
                """
                    INSERT INTO flow_v2 (id, slug, display_name)
                    VALUES
                        (:first_flow_uuid, 'first', 'Fluxo Primeiro'),
                        (:second_flow_uuid, 'second', 'Fluxo Segundo');
                    INSERT INTO flow_v2_revision (id, flow_id, version)
                    VALUES
                        (:first_revision_uuid, :first_flow_uuid, 3),
                        (:second_revision_uuid, :second_flow_uuid, 7);
                    INSERT INTO orch_journey_flow_coverage (
                        flow_uuid, coverage_started_at, last_fact_at
                    )
                    VALUES
                        (:first_flow_uuid, :coverage_at, :generated_at),
                        (:second_flow_uuid, :coverage_at, :generated_at)
                    """,
                {
                    "first_flow_uuid": first_flow_uuid,
                    "second_flow_uuid": second_flow_uuid,
                    "first_revision_uuid": first_revision_uuid,
                    "second_revision_uuid": second_revision_uuid,
                    "coverage_at": generated_at - timedelta(days=1),
                    "generated_at": generated_at,
                },
            )
            first_source_id = (
                await db_session.execute(
                    text("INSERT INTO orch_sessions (uuid) VALUES (:uuid) RETURNING id"),
                    {"uuid": first_session_uuid},
                )
            ).scalar_one()
            second_source_id = (
                await db_session.execute(
                    text("INSERT INTO orch_sessions (uuid) VALUES (:uuid) RETURNING id"),
                    {"uuid": second_session_uuid},
                )
            ).scalar_one()
            first_journey_id = (
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_journey_sessions (
                            source_session_id, session_uuid, flow_uuid,
                            flow_revision_id, lifecycle_status, current_stage,
                            current_stage_ordinal, highest_stage,
                            highest_stage_ordinal, started_at, last_progress_at,
                            ended_at, terminal_class
                        )
                        VALUES (
                            :source_session_id, :session_uuid, :flow_uuid,
                            :revision_uuid, 'completed', 'abordagem', 4,
                            'abordagem', 4, :started_at, :ended_at, :ended_at,
                            'completed'
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "source_session_id": first_source_id,
                        "session_uuid": first_session_uuid,
                        "flow_uuid": first_flow_uuid,
                        "revision_uuid": first_revision_uuid,
                        "started_at": generated_at - timedelta(minutes=10),
                        "ended_at": generated_at - timedelta(minutes=8),
                    },
                )
            ).scalar_one()
            second_journey_id = (
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_journey_sessions (
                            source_session_id, session_uuid, flow_uuid,
                            flow_revision_id, lifecycle_status, current_stage,
                            current_stage_ordinal, highest_stage,
                            highest_stage_ordinal, started_at, last_progress_at
                        )
                        VALUES (
                            :source_session_id, :session_uuid, :flow_uuid,
                            :revision_uuid, 'in_progress', 'qualificacao', 3,
                            'qualificacao', 3, :started_at, :started_at
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "source_session_id": second_source_id,
                        "session_uuid": second_session_uuid,
                        "flow_uuid": second_flow_uuid,
                        "revision_uuid": second_revision_uuid,
                        "started_at": generated_at - timedelta(minutes=2),
                    },
                )
            ).scalar_one()
            await _execute_statements(
                db_session,
                """
                    INSERT INTO orch_journey_stage_visits (
                        journey_session_id, session_uuid, flow_uuid,
                        flow_revision_id, component_ref_id, component_kind,
                        stage_id, stage_ordinal, visit_number, entered_at,
                        exited_at, exit_kind
                    )
                    VALUES
                        (
                            :first_journey_id, :first_session_uuid,
                            :first_flow_uuid, :first_revision_uuid,
                            :first_component_uuid, 'identidade_person',
                            'entrada', 1, 1, :first_entered_at,
                            :first_exited_at, 'transition'
                        ),
                        (
                            :first_journey_id, :first_session_uuid,
                            :first_flow_uuid, :first_revision_uuid,
                            :second_component_uuid, 'send_with_sms',
                            'abordagem', 4, 1, :first_exited_at,
                            :ended_at, 'transition'
                        ),
                        (
                            :second_journey_id, :second_session_uuid,
                            :second_flow_uuid, :second_revision_uuid,
                            :first_component_uuid, 'select_contact_channel',
                            'qualificacao', 3, 1, :second_entered_at,
                            NULL, NULL
                        )
                    """,
                {
                    "first_journey_id": first_journey_id,
                    "second_journey_id": second_journey_id,
                    "first_session_uuid": first_session_uuid,
                    "second_session_uuid": second_session_uuid,
                    "first_flow_uuid": first_flow_uuid,
                    "second_flow_uuid": second_flow_uuid,
                    "first_revision_uuid": first_revision_uuid,
                    "second_revision_uuid": second_revision_uuid,
                    "first_component_uuid": first_component_uuid,
                    "second_component_uuid": second_component_uuid,
                    "first_entered_at": generated_at - timedelta(minutes=10),
                    "first_exited_at": generated_at - timedelta(minutes=9),
                    "ended_at": generated_at - timedelta(minutes=8),
                    "second_entered_at": generated_at - timedelta(minutes=2),
                },
            )
            action_id = uuid4()
            await _execute_statements(
                db_session,
                """
                    INSERT INTO orch_journey_channel_actions (
                        action_id, journey_session_id, source_kind, source_id,
                        flow_uuid, flow_revision_id, component_ref_id,
                        component_kind, channel, action_sequence,
                        lifecycle_status, requested_at, delivered_at,
                        provider_reference_hash
                    )
                    VALUES (
                        :action_id, :journey_session_id, 'sms_dispatch',
                        'safe-correlation', :flow_uuid, :revision_uuid,
                        :component_uuid, 'send_with_sms', 'sms', 1,
                        'delivered', :requested_at, :delivered_at,
                        :provider_reference_hash
                    );
                    INSERT INTO orch_journey_channel_action_events (
                        action_id, event_key, event_type, native_status,
                        normalized_status, occurred_at, received_at, metadata
                    )
                    VALUES (
                        :action_id, 'event-1', 'delivered', 'DELIVERED',
                        'delivered', :delivered_at, :delivered_at,
                        '{"raw_phone":"5511999999999","token":"secret"}'::jsonb
                    );
                    INSERT INTO orch_sessions_alarms (flow_uuid, code, created_at)
                    VALUES (
                        :flow_uuid, 'journey_metrics_stage_missing', :requested_at
                    )
                    """,
                {
                    "action_id": action_id,
                    "journey_session_id": first_journey_id,
                    "flow_uuid": first_flow_uuid,
                    "revision_uuid": first_revision_uuid,
                    "component_uuid": second_component_uuid,
                    "requested_at": generated_at - timedelta(minutes=9),
                    "delivered_at": generated_at - timedelta(minutes=8, seconds=30),
                    "provider_reference_hash": "a" * 64,
                },
            )

            payload = await build_journey_workspace_snapshot_payload(
                db_session,
                workspace_uuid=str(workspace_uuid),
                workspace_name="Workspace Teste",
                generated_at=generated_at,
            )
            encoded, size, _digest = _canonical_snapshot(payload)
            assert size < 256 * 1024
            assert payload["workspace_view"]["summary"] == {
                "sessions_started": 2,
                "sessions_in_progress": 1,
                "sessions_completed": 1,
                "sessions_abandoned": 0,
                "sessions_failed": 0,
                "sessions_without_actions": 1,
                "conversions": 0,
                "conversion_denominator": 2,
            }
            assert [item["stage_id"] for item in payload["workspace_view"]["funnel"]] == [
                "entrada",
                "identificacao",
                "qualificacao",
                "abordagem",
                "proposta",
                "decisao",
                "desfecho",
            ]
            assert payload["workspace_view"]["channels"] == [
                {
                    "channel": "sms",
                    "grain": "actions",
                    "actions": 1,
                    "outcomes": {"delivered": 1},
                }
            ]
            assert "5511999999999" not in encoded
            assert '"secret"' not in encoded
            assert "provider_reference_hash" not in encoded

            filtered = await build_journey_workspace_view(
                db_session,
                period_from=generated_at - timedelta(hours=1),
                period_to=generated_at + timedelta(seconds=1),
                flow_uuid=str(first_flow_uuid),
                revision_uuid=str(first_revision_uuid),
                channels=["sms"],
            )
            assert filtered["summary"]["sessions_started"] == 1
            assert filtered["summary"]["sessions_completed"] == 1
            assert filtered["summary"]["sessions_without_actions"] == 0
            assert filtered["duration"]["histogram"] == [
                {"lte_seconds": 60, "count": 0},
                {"lte_seconds": 300, "count": 1},
                {"lte_seconds": 900, "count": 1},
                {"lte_seconds": None, "count": 0},
            ]
        finally:
            await transaction.rollback()
