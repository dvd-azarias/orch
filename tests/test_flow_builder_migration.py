from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.services.migration_service import MIGRATIONS, _run_migration_file


MIGRATION = "0027_create_orch_flow_builder_tables"
MIGRATION_PATH = "sql/027_create_orch_flow_builder_tables.sql"


def test_flow_builder_migration_is_registered_after_metrics_snapshots() -> None:
    versions = [version for version, _path in MIGRATIONS]
    assert versions.index(MIGRATION) > versions.index(
        "0026_create_orch_metrics_dispatch_snapshots"
    )
    assert (MIGRATION, MIGRATION_PATH) in MIGRATIONS


def test_flow_builder_migration_keeps_sessions_and_messages_workspace_local() -> None:
    sql = Path(MIGRATION_PATH).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS orch_flow_builder_sessions" in sql
    assert "CREATE TABLE IF NOT EXISTS orch_flow_builder_messages" in sql
    assert "workspace_uuid UUID NOT NULL" in sql
    assert "mode = 'orchestration'" in sql
    assert "REFERENCES orch_flow_builder_sessions(id) ON DELETE CASCADE" in sql
    assert "UNIQUE (session_id, sequence)" in sql
    assert "BYTEA" not in sql


@pytest.mark.asyncio
async def test_flow_builder_migration_executes_idempotently() -> None:
    schema = f"orch_flow_builder_test_{uuid4().hex}"
    safe_schema = schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            await db_session.execute(text(f'CREATE SCHEMA "{safe_schema}"'))
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
            sessions = (
                await db_session.execute(
                    text("SELECT to_regclass('orch_flow_builder_sessions')")
                )
            ).scalar_one()
            messages = (
                await db_session.execute(
                    text("SELECT to_regclass('orch_flow_builder_messages')")
                )
            ).scalar_one()
            assert sessions == "orch_flow_builder_sessions"
            assert messages == "orch_flow_builder_messages"
        finally:
            await transaction.rollback()
