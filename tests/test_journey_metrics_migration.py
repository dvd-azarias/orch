from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.database import get_session_factory
from app.services.migration_service import MIGRATIONS, _run_migration_file


MIGRATION_PATH = "sql/023_create_orch_journey_metrics_tables.sql"


def test_journey_metrics_migration_precedes_workspace_snapshot_state() -> None:
    versions = [version for version, _path in MIGRATIONS]
    assert versions[-3] == "0022_create_orch_billing_batch_tables"
    assert versions[-2] == "0023_create_orch_journey_metrics_tables"
    assert versions[-1] == "0024_create_orch_journey_workspace_snapshot_state"


def test_journey_metrics_migration_is_additive_and_has_required_guards() -> None:
    sql = Path(MIGRATION_PATH).read_text(encoding="utf-8")

    for table in (
        "orch_journey_settings",
        "orch_journey_flow_coverage",
        "orch_journey_sessions",
        "orch_journey_stage_visits",
        "orch_journey_channel_actions",
        "orch_journey_channel_action_events",
        "orch_journey_snapshot_delivery",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql

    assert "enabled BOOLEAN NOT NULL DEFAULT TRUE" in sql
    assert "retention_days SMALLINT NOT NULL DEFAULT 30" in sql
    assert "CHECK (retention_days BETWEEN 1 AND 30)" in sql
    assert "status TEXT NOT NULL DEFAULT 'active'" in sql
    assert "last_socket_send_at TIMESTAMPTZ" in sql
    assert "last_socket_sequence BIGINT NOT NULL DEFAULT 0" in sql
    assert "delivered_at TIMESTAMPTZ" not in sql.split(
        "CREATE TABLE IF NOT EXISTS orch_journey_snapshot_delivery", 1
    )[1]
    assert "ALTER TABLE" not in sql
    assert "DROP TABLE" not in sql
    assert "DROP COLUMN" not in sql
    assert "INSERT INTO orch_sessions" not in sql
    assert "INSERT INTO orch_session_metrics" not in sql


@pytest.mark.asyncio
async def test_journey_metrics_migration_executes_in_rolled_back_schema() -> None:
    schema = f"orch_journey_test_{uuid4().hex}"
    safe_schema = schema.replace('"', '""')
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
                        uuid UUID NOT NULL UNIQUE
                    )
                    """
                )
            )
            await _run_migration_file(
                db_session,
                schema=schema,
                migration_path=MIGRATION_PATH,
            )
            await _run_migration_file(
                db_session,
                schema=schema,
                migration_path=MIGRATION_PATH,
            )

            tables = (
                await db_session.execute(
                    text(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = :schema
                          AND table_name LIKE 'orch_journey_%'
                        ORDER BY table_name
                        """
                    ),
                    {"schema": schema},
                )
            ).scalars().all()
            assert tables == [
                "orch_journey_channel_action_events",
                "orch_journey_channel_actions",
                "orch_journey_flow_coverage",
                "orch_journey_sessions",
                "orch_journey_settings",
                "orch_journey_snapshot_delivery",
                "orch_journey_stage_visits",
            ]

            settings = (
                await db_session.execute(
                    text("SELECT enabled, retention_days FROM orch_journey_settings")
                )
            ).mappings().one()
            assert dict(settings) == {"enabled": True, "retention_days": 30}

            with pytest.raises(IntegrityError):
                async with db_session.begin_nested():
                    await db_session.execute(
                        text(
                            """
                            UPDATE orch_journey_settings
                            SET retention_days = 31
                            WHERE singleton_id = 1
                            """
                        )
                    )

            flow_uuid = uuid4()
            session_uuid = uuid4()
            revision_uuid = uuid4()
            component_uuid = uuid4()
            source_session_id = (
                await db_session.execute(
                    text("INSERT INTO orch_sessions (uuid) VALUES (:uuid) RETURNING id"),
                    {"uuid": session_uuid},
                )
            ).scalar_one()
            coverage = (
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_journey_flow_coverage (flow_uuid)
                        VALUES (:flow_uuid)
                        RETURNING status, coverage_started_at
                        """
                    ),
                    {"flow_uuid": flow_uuid},
                )
            ).mappings().one()
            assert coverage["status"] == "active"
            assert coverage["coverage_started_at"] is not None

            journey_session_id = (
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_journey_sessions (
                            source_session_id,
                            session_uuid,
                            flow_uuid,
                            flow_revision_id,
                            session_scope,
                            lifecycle_status,
                            current_stage,
                            current_stage_ordinal,
                            highest_stage,
                            highest_stage_ordinal,
                            started_at,
                            last_progress_at
                        )
                        VALUES (
                            :source_session_id,
                            :session_uuid,
                            :flow_uuid,
                            :revision_uuid,
                            'person',
                            'in_progress',
                            'identificacao',
                            2,
                            'identificacao',
                            2,
                            NOW(),
                            NOW()
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "source_session_id": source_session_id,
                        "session_uuid": session_uuid,
                        "flow_uuid": flow_uuid,
                        "revision_uuid": revision_uuid,
                    },
                )
            ).scalar_one()

            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_journey_stage_visits (
                        journey_session_id,
                        session_uuid,
                        flow_uuid,
                        flow_revision_id,
                        component_ref_id,
                        component_kind,
                        stage_id,
                        stage_ordinal,
                        visit_number,
                        entered_at
                    )
                    VALUES (
                        :journey_session_id,
                        :session_uuid,
                        :flow_uuid,
                        :revision_uuid,
                        :component_uuid,
                        'identidade_person',
                        'identificacao',
                        2,
                        1,
                        NOW()
                    )
                    """
                ),
                {
                    "journey_session_id": journey_session_id,
                    "session_uuid": session_uuid,
                    "flow_uuid": flow_uuid,
                    "revision_uuid": revision_uuid,
                    "component_uuid": component_uuid,
                },
            )

            action_id = (
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_journey_channel_actions (
                            journey_session_id,
                            source_kind,
                            source_id,
                            flow_uuid,
                            flow_revision_id,
                            component_ref_id,
                            component_kind,
                            channel,
                            action_sequence,
                            requested_at
                        )
                        VALUES (
                            :journey_session_id,
                            'supplier_v2_dial_attempt',
                            'attempt-1',
                            :flow_uuid,
                            :revision_uuid,
                            :component_uuid,
                            'send_with_dialer_handoff',
                            'voice',
                            1,
                            NOW()
                        )
                        RETURNING action_id
                        """
                    ),
                    {
                        "journey_session_id": journey_session_id,
                        "flow_uuid": flow_uuid,
                        "revision_uuid": revision_uuid,
                        "component_uuid": component_uuid,
                    },
                )
            ).scalar_one()
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_journey_channel_action_events (
                        action_id,
                        event_key,
                        event_type,
                        native_status,
                        normalized_status,
                        occurred_at
                    )
                    VALUES (
                        :action_id,
                        'release-16',
                        'dial_result',
                        '16',
                        'answered',
                        NOW()
                    )
                    """
                ),
                {"action_id": action_id},
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_journey_snapshot_delivery (
                        flow_uuid,
                        snapshot_sequence,
                        last_socket_sequence,
                        snapshot_id,
                        payload,
                        payload_bytes,
                        dirty_since
                    )
                    VALUES (
                        :flow_uuid,
                        1,
                        0,
                        :snapshot_id,
                        CAST(:payload AS jsonb),
                        2,
                        NOW()
                    )
                    """
                ),
                {
                    "flow_uuid": flow_uuid,
                    "snapshot_id": uuid4(),
                    "payload": "{}",
                },
            )

            counts = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            (SELECT COUNT(*) FROM orch_journey_sessions) AS sessions,
                            (SELECT COUNT(*) FROM orch_journey_stage_visits) AS visits,
                            (SELECT COUNT(*) FROM orch_journey_channel_actions) AS actions,
                            (SELECT COUNT(*) FROM orch_journey_channel_action_events) AS events,
                            (SELECT COUNT(*) FROM orch_journey_snapshot_delivery) AS deliveries
                        """
                    )
                )
            ).mappings().one()
            assert dict(counts) == {
                "sessions": 1,
                "visits": 1,
                "actions": 1,
                "events": 1,
                "deliveries": 1,
            }
        finally:
            await transaction.rollback()
