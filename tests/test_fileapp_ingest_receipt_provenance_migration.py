from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
import app.services.migration_service as migration_service
from app.services.migration_service import MIGRATIONS, _run_migration_file


MIGRATION = "0030_add_fileapp_ingest_receipt_provenance"
MIGRATION_PATH = "sql/030_add_fileapp_ingest_receipt_provenance.sql"


def test_fileapp_receipt_provenance_migration_is_registered_last() -> None:
    assert MIGRATIONS[-1] == (MIGRATION, MIGRATION_PATH)


def test_fileapp_receipt_provenance_migration_is_additive_and_idempotent() -> None:
    sql = Path(MIGRATION_PATH).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS orch_fileapp_ingest_receipts" in sql
    assert "ADD COLUMN IF NOT EXISTS mailing_uuid UUID" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_orch_fileapp_ingest_receipts_mailing" in sql
    assert "DROP TABLE" not in sql
    assert "DROP COLUMN" not in sql


@pytest.mark.asyncio
async def test_fileapp_receipt_provenance_migration_executes_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = f"orch_fileapp_receipt_test_{uuid4().hex}"
    safe_schema = schema.replace('"', '""')
    session_factory = get_session_factory()
    monkeypatch.setattr(
        migration_service,
        "_workspace_tablespace_from_schema",
        lambda _schema: "pg_default",
    )

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
            column_type = (
                await db_session.execute(
                    text(
                        """
                        SELECT data_type
                        FROM information_schema.columns
                        WHERE table_schema = :schema
                          AND table_name = 'orch_fileapp_ingest_receipts'
                          AND column_name = 'mailing_uuid'
                        """
                    ),
                    {"schema": schema},
                )
            ).scalar_one()
            assert column_type == "uuid"
        finally:
            await transaction.rollback()
