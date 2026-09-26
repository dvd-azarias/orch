from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

import app.tasks.journey_dashboard_tasks as dashboard_tasks
from app.core.database import get_session_factory
from app.core.workspace import workspace_schema_from_uuid
from app.services.journey_workspace_snapshot_service import (
    mark_journey_workspace_snapshot_dirty,
)
from app.services.migration_service import _run_migration_file


@pytest.mark.asyncio
async def test_snapshot_worker_builds_and_persists_one_workspace_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_uuid = str(uuid4())
    schema = workspace_schema_from_uuid(workspace_uuid)
    safe_schema = schema.replace('"', '""')
    flow_uuid = uuid4()
    revision_uuid = uuid4()
    session_uuid = uuid4()
    session_factory = get_session_factory()
    now = datetime.now(UTC)
    settings = SimpleNamespace(
        orch_journey_dashboard_enabled=True,
        orch_journey_snapshot_lease_seconds=120,
        orch_journey_redis_url=None,
    )
    monkeypatch.setattr(dashboard_tasks, "get_settings", lambda: settings)

    try:
        async with session_factory() as db_session:
            async with db_session.begin():
                await db_session.execute(text(f'CREATE SCHEMA "{safe_schema}"'))
                for statement in (
                    f"""
                    CREATE TABLE "{safe_schema}".orch_sessions (
                        id BIGSERIAL PRIMARY KEY,
                        uuid UUID NOT NULL UNIQUE
                    )
                    """,
                    f"""
                    CREATE TABLE "{safe_schema}".orch_sessions_alarms (
                        id BIGSERIAL PRIMARY KEY,
                        flow_uuid UUID,
                        code TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL
                    )
                    """,
                    f"""
                    CREATE TABLE "{safe_schema}".flow_v2 (
                        id UUID PRIMARY KEY,
                        slug TEXT,
                        display_name TEXT
                    )
                    """,
                    f"""
                    CREATE TABLE "{safe_schema}".flow_v2_revision (
                        id UUID PRIMARY KEY,
                        flow_id UUID NOT NULL,
                        version INTEGER
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
                await db_session.execute(
                    text(
                        """
                        INSERT INTO flow_v2 (id, slug, display_name)
                        VALUES (:flow_uuid, 'canary', 'Canário');
                        """
                    ),
                    {"flow_uuid": flow_uuid},
                )
                await db_session.execute(
                    text(
                        """
                        INSERT INTO flow_v2_revision (id, flow_id, version)
                        VALUES (:revision_uuid, :flow_uuid, 1)
                        """
                    ),
                    {"revision_uuid": revision_uuid, "flow_uuid": flow_uuid},
                )
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
                        INSERT INTO orch_journey_flow_coverage (
                            flow_uuid, coverage_started_at, last_fact_at
                        )
                        VALUES (:flow_uuid, :now, :now);
                        """
                    ),
                    {"flow_uuid": flow_uuid, "now": now},
                )
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_journey_sessions (
                            source_session_id, session_uuid, flow_uuid,
                            flow_revision_id, lifecycle_status, started_at,
                            last_progress_at
                        )
                        VALUES (
                            :source_session_id, :session_uuid, :flow_uuid,
                            :revision_uuid, 'in_progress', :now, :now
                        )
                        """
                    ),
                    {
                        "source_session_id": source_session_id,
                        "session_uuid": session_uuid,
                        "flow_uuid": flow_uuid,
                        "revision_uuid": revision_uuid,
                        "now": now,
                    },
                )
                await mark_journey_workspace_snapshot_dirty(
                    db_session,
                    observed_at=now,
                    debounce_seconds=1,
                )
                await db_session.execute(
                    text(
                        """
                        UPDATE orch_journey_workspace_snapshot_state
                        SET next_attempt_at = :now
                        WHERE singleton_id = 1
                        """
                    ),
                    {"now": now},
                )

        result = await dashboard_tasks._build_journey_workspace_snapshot(  # noqa: SLF001
            workspace_uuid=workspace_uuid,
            workspace_name="Workspace Canário",
        )
        assert result["status"] == "stored"
        assert result["snapshot_sequence"] == 1
        assert result["redis_notified"] is False

        async with session_factory() as db_session:
            async with db_session.begin():
                await db_session.execute(
                    text(f'SET LOCAL search_path TO "{safe_schema}"')
                )
                row = (
                    await db_session.execute(
                        text(
                            """
                            SELECT snapshot_sequence, snapshot_payload
                            FROM orch_journey_workspace_snapshot_state
                            """
                        )
                    )
                ).mappings().one()
        assert row["snapshot_sequence"] == 1
        assert row["snapshot_payload"]["payload"]["workspace_view"]["summary"][
            "sessions_started"
        ] == 1
    finally:
        async with session_factory() as db_session:
            async with db_session.begin():
                await db_session.execute(
                    text(f'DROP SCHEMA IF EXISTS "{safe_schema}" CASCADE')
                )
