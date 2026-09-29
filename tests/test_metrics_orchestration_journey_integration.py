from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.services.journey_metrics_service import (
    finalize_journey_session_metrics,
    initialize_journey_session_metrics,
    record_journey_component_entry,
    record_journey_component_transition,
)
from app.services.migration_service import _run_migration_file


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"


@pytest.mark.asyncio
async def test_journey_writes_durable_metrics_events_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = f"orch_metrics_journey_test_{uuid4().hex}"
    safe_schema = schema.replace('"', '""')
    flow_uuid = uuid4()
    revision_uuid = uuid4()
    session_uuid = uuid4()
    person_uuid = uuid4()
    component_uuid = uuid4()
    started_at = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    enabled_settings = replace(
        get_settings(),
        orch_metrics_events_enabled=True,
        orch_metrics_events_workspace_allowlist=(WORKSPACE_UUID,),
    )
    monkeypatch.setattr(
        "app.services.journey_metrics_service.get_current_workspace_schema",
        lambda: schema,
    )
    monkeypatch.setattr(
        "app.services.alarm_service.get_current_workspace_schema",
        lambda: schema,
    )
    monkeypatch.setattr(
        "app.services.metrics_orchestration_event_service.get_current_workspace_uuid",
        lambda: WORKSPACE_UUID,
    )
    monkeypatch.setattr(
        "app.services.metrics_orchestration_event_service.get_settings",
        lambda: enabled_settings,
    )
    monkeypatch.setattr(
        "app.services.metrics_event_outbox_service.get_settings",
        lambda: enabled_settings,
    )

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            await db_session.execute(text(f'CREATE SCHEMA "{safe_schema}"'))
            await db_session.execute(
                text(
                    f"""
                    CREATE TABLE "{safe_schema}".orch_sessions (
                        id BIGSERIAL PRIMARY KEY,
                        uuid UUID NOT NULL UNIQUE,
                        flow_uuid UUID NOT NULL,
                        state INTEGER NOT NULL DEFAULT 2,
                        entity TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_address TEXT NOT NULL,
                        runtime_variables JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        started_at TIMESTAMPTZ,
                        ended_at TIMESTAMPTZ,
                        abandoned_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
            )
            for migration_path in (
                "sql/023_create_orch_journey_metrics_tables.sql",
                "sql/024_create_orch_journey_workspace_snapshot_state.sql",
                "sql/025_create_orch_metrics_event_outbox.sql",
            ):
                await _run_migration_file(
                    db_session,
                    schema=schema,
                    migration_path=migration_path,
                )
            source_session_id = (
                await db_session.execute(
                    text(
                        f"""
                        INSERT INTO "{safe_schema}".orch_sessions (
                            uuid, flow_uuid, entity, entity_type, entity_address,
                            runtime_variables, started_at, created_at
                        )
                        VALUES (
                            :session_uuid, :flow_uuid, 'external-1', 'person',
                            '5511975620806', CAST(:runtime_variables AS jsonb),
                            :started_at, :started_at
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "session_uuid": session_uuid,
                        "flow_uuid": flow_uuid,
                        "started_at": started_at,
                        "runtime_variables": (
                            '{"variables":{"contact":{"person_uuid":"'
                            + str(person_uuid)
                            + '","identifier":"34455521852"}}}'
                        ),
                    },
                )
            ).scalar_one()
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))

            context = await initialize_journey_session_metrics(
                db_session,
                source_session_id=source_session_id,
                flow_uuid=str(flow_uuid),
                flow_revision_id=str(revision_uuid),
                session_scope="person",
            )
            assert context is not None
            assert await initialize_journey_session_metrics(
                db_session,
                source_session_id=source_session_id,
                flow_uuid=str(flow_uuid),
                flow_revision_id=str(revision_uuid),
                session_scope="person",
            ) == context

            component = {
                "ref_id": str(component_uuid),
                "component_id": "identidade_person",
                "parameters": {"stage": "identificacao"},
            }
            await record_journey_component_entry(
                db_session,
                context=context,
                component=component,
                card_cursor=str(component_uuid),
                component_kind="identidade_person",
                occurred_at=started_at + timedelta(seconds=1),
            )
            await record_journey_component_entry(
                db_session,
                context=context,
                component=component,
                card_cursor=str(component_uuid),
                component_kind="identidade_person",
                occurred_at=started_at + timedelta(seconds=2),
            )
            await record_journey_component_transition(
                db_session,
                context=context,
                component=component,
                card_cursor=str(component_uuid),
                occurred_at=started_at + timedelta(seconds=3),
            )
            await record_journey_component_transition(
                db_session,
                context=context,
                component=component,
                card_cursor=str(component_uuid),
                occurred_at=started_at + timedelta(seconds=4),
            )
            await db_session.execute(
                text(
                    f"""
                    UPDATE "{safe_schema}".orch_sessions
                    SET state = 3, ended_at = :ended_at
                    WHERE id = :source_session_id
                    """
                ),
                {
                    "source_session_id": source_session_id,
                    "ended_at": started_at + timedelta(seconds=5),
                },
            )
            await finalize_journey_session_metrics(
                db_session,
                context=context,
                stopped_reason="finished_by_component",
                occurred_at=started_at + timedelta(seconds=5),
            )

            events = (
                await db_session.execute(
                    text(
                        f"""
                        SELECT event_type, idempotency_key, envelope
                        FROM "{safe_schema}".orch_metrics_event_outbox
                        ORDER BY id
                        """
                    )
                )
            ).mappings().all()
            assert [row["event_type"] for row in events] == [
                "interaction.session.started.v1",
                "flow.execution.started.v1",
                "flow.node.entered.v1",
                "flow.node.exited.v1",
                "flow.execution.completed.v1",
                "interaction.session.ended.v1",
            ]
            assert len({row["idempotency_key"] for row in events}) == 6
            assert all(
                row["envelope"]["context"]["flow_type"] == "orchestration"
                for row in events
            )
            assert all(
                row["envelope"]["context"]["contact_id"] == str(person_uuid)
                for row in events
            )
        finally:
            await transaction.rollback()
