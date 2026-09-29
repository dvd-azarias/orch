from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.services.migration_service import MIGRATIONS, _run_migration_file


MIGRATION_PATH = "sql/026_create_orch_metrics_dispatch_snapshots.sql"


def test_metrics_dispatch_snapshot_migration_is_registered_last_and_additive() -> None:
    assert MIGRATIONS[-1] == (
        "0026_create_orch_metrics_dispatch_snapshots",
        MIGRATION_PATH,
    )
    sql = Path(MIGRATION_PATH).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS orch_metrics_dispatch_snapshots" in sql
    assert "REFERENCES orch_journey_channel_actions" in sql
    assert "UNIQUE (dispatch_id)" in sql
    assert "DROP TABLE" not in sql
    assert "DROP COLUMN" not in sql


@pytest.mark.asyncio
async def test_metrics_dispatch_snapshot_migration_executes_idempotently() -> None:
    schema = f"orch_metrics_dispatch_snapshot_test_{uuid4().hex}"
    safe_schema = schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            await db_session.execute(text(f'CREATE SCHEMA "{safe_schema}"'))
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            await db_session.execute(
                text(
                    """
                    CREATE TABLE orch_sessions (
                        id BIGSERIAL PRIMARY KEY
                    )
                    """
                )
            )
            await _run_migration_file(
                db_session,
                schema=schema,
                migration_path="sql/023_create_orch_journey_metrics_tables.sql",
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
            exists = (
                await db_session.execute(
                    text("SELECT to_regclass('orch_metrics_dispatch_snapshots')")
                )
            ).scalar_one()
            assert exists == "orch_metrics_dispatch_snapshots"
        finally:
            await transaction.rollback()
