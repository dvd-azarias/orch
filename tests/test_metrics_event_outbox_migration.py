from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.database import get_session_factory
from app.services.migration_service import MIGRATIONS, _run_migration_file


MIGRATION_PATH = "sql/025_create_orch_metrics_event_outbox.sql"


def test_metrics_event_outbox_migration_is_registered_and_additive() -> None:
    assert (
        "0025_create_orch_metrics_event_outbox",
        MIGRATION_PATH,
    ) in MIGRATIONS
    sql = Path(MIGRATION_PATH).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS orch_metrics_event_outbox" in sql
    assert "UNIQUE (idempotency_key)" in sql
    assert "status IN ('pending', 'publishing', 'published', 'dead')" in sql
    assert "WHERE status IN ('pending', 'publishing')" in sql
    assert "DROP TABLE" not in sql
    assert "DROP COLUMN" not in sql
    assert "INSERT INTO orch_sessions" not in sql


@pytest.mark.asyncio
async def test_metrics_event_outbox_migration_executes_idempotently() -> None:
    schema = f"orch_metrics_outbox_test_{uuid4().hex}"
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
            event_id = uuid4()
            session_uuid = uuid4()
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_metrics_event_outbox (
                        event_id, idempotency_key, event_type, session_uuid, envelope
                    )
                    VALUES (
                        :event_id, 'session:test:started',
                        'interaction.session.started.v1', :session_uuid,
                        CAST(:envelope AS jsonb)
                    )
                    """
                ),
                {
                    "event_id": event_id,
                    "session_uuid": session_uuid,
                    "envelope": '{"event_type":"interaction.session.started.v1"}',
                },
            )
            with pytest.raises(IntegrityError):
                async with db_session.begin_nested():
                    await db_session.execute(
                        text(
                            """
                            INSERT INTO orch_metrics_event_outbox (
                                event_id, idempotency_key, event_type, envelope
                            )
                            VALUES (
                                :event_id, 'session:test:started',
                                'interaction.session.started.v1', '{}'::jsonb
                            )
                            """
                        ),
                        {"event_id": uuid4()},
                    )
            row = (
                await db_session.execute(
                    text(
                        """
                        SELECT status, attempts, published_at
                        FROM orch_metrics_event_outbox
                        WHERE event_id = :event_id
                        """
                    ),
                    {"event_id": event_id},
                )
            ).mappings().one()
            assert dict(row) == {
                "status": "pending",
                "attempts": 0,
                "published_at": None,
            }
        finally:
            await transaction.rollback()
