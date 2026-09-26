from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.database import get_session_factory
from app.services.migration_service import MIGRATIONS, _run_migration_file


BASE_MIGRATION_PATH = "sql/023_create_orch_journey_metrics_tables.sql"
MIGRATION_PATH = "sql/024_create_orch_journey_workspace_snapshot_state.sql"


def test_workspace_snapshot_state_migration_is_registered_last() -> None:
    versions = [version for version, _path in MIGRATIONS]

    assert versions[-2] == "0023_create_orch_journey_metrics_tables"
    assert versions[-1] == "0024_create_orch_journey_workspace_snapshot_state"


def test_workspace_snapshot_state_migration_is_additive_and_bounded() -> None:
    sql = Path(MIGRATION_PATH).read_text(encoding="utf-8")

    assert (
        "CREATE TABLE IF NOT EXISTS orch_journey_workspace_snapshot_state"
        in sql
    )
    assert "singleton_id SMALLINT PRIMARY KEY DEFAULT 1" in sql
    assert "snapshot_sequence BIGINT NOT NULL DEFAULT 0" in sql
    assert "dirty_generation BIGINT NOT NULL DEFAULT 0" in sql
    assert "built_generation BIGINT NOT NULL DEFAULT 0" in sql
    assert "dirty_since TIMESTAMPTZ" in sql
    assert "latest_dirty_at TIMESTAMPTZ" in sql
    assert "claim_generation BIGINT" in sql
    assert "claim_expires_at TIMESTAMPTZ" in sql
    assert "snapshot_payload JSONB" in sql
    assert "snapshot_sha256 TEXT" in sql
    assert "INSERT INTO orch_journey_workspace_snapshot_state" in sql
    assert "ALTER TABLE" not in sql
    assert "DROP TABLE" not in sql
    assert "DROP COLUMN" not in sql
    assert "orch_journey_snapshot_delivery" not in sql


@pytest.mark.asyncio
async def test_workspace_snapshot_state_migration_executes_idempotently() -> None:
    schema = f"orch_journey_snapshot_test_{uuid4().hex}"
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
                migration_path=BASE_MIGRATION_PATH,
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

            state = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            singleton_id,
                            timezone,
                            snapshot_sequence,
                            dirty_generation,
                            built_generation,
                            dirty_since,
                            snapshot_payload
                        FROM orch_journey_workspace_snapshot_state
                        """
                    )
                )
            ).mappings().one()
            assert dict(state) == {
                "singleton_id": 1,
                "timezone": "America/Sao_Paulo",
                "snapshot_sequence": 0,
                "dirty_generation": 0,
                "built_generation": 0,
                "dirty_since": None,
                "snapshot_payload": None,
            }

            await db_session.execute(
                text(
                    """
                    UPDATE orch_journey_workspace_snapshot_state
                    SET
                        snapshot_sequence = 1,
                        dirty_generation = 1,
                        built_generation = 1,
                        snapshot_id = :snapshot_id,
                        snapshot_payload = CAST(:payload AS jsonb),
                        snapshot_bytes = 2,
                        snapshot_sha256 = :snapshot_sha256
                    WHERE singleton_id = 1
                    """
                ),
                {
                    "snapshot_id": uuid4(),
                    "payload": "{}",
                    "snapshot_sha256": "a" * 64,
                },
            )

            with pytest.raises(IntegrityError):
                async with db_session.begin_nested():
                    await db_session.execute(
                        text(
                            """
                            UPDATE orch_journey_workspace_snapshot_state
                            SET snapshot_sequence = -1
                            WHERE singleton_id = 1
                            """
                        )
                    )

            with pytest.raises(IntegrityError):
                async with db_session.begin_nested():
                    await db_session.execute(
                        text(
                            """
                            UPDATE orch_journey_workspace_snapshot_state
                            SET
                                dirty_generation = 2,
                                dirty_since = NOW(),
                                latest_dirty_at = NOW(),
                                claim_token = :claim_token
                            WHERE singleton_id = 1
                            """
                        ),
                        {"claim_token": uuid4()},
                    )
        finally:
            await transaction.rollback()
