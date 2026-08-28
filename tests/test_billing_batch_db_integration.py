from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.services.migration_service import _run_migration_file
from app.services.billing_batch_service import aggregate_next_billing_snapshot, reconcile_billing_events


@pytest.mark.asyncio
async def test_real_postgres_reconcile_and_aggregate_450_events_in_temporary_tables() -> None:
    """Exercise the real PostgreSQL locking/JSON/array SQL without applying a workspace migration."""
    workspace_uuid = "11111111-1111-1111-1111-111111111111"
    settings = SimpleNamespace(
        billing_batch_size=200,
        billing_application_code="target",
        billing_service_code="service-orch",
        billing_metric_code="service-orch",
    )
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_sessions (
                        id BIGINT PRIMARY KEY,
                        uuid UUID NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_billing_snapshots (
                        snapshot_id TEXT PRIMARY KEY,
                        workspace_uuid UUID NOT NULL,
                        billing_period DATE NOT NULL,
                        snapshot_at TIMESTAMPTZ NOT NULL,
                        payload JSONB NOT NULL,
                        quantity INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        last_attempt_at TIMESTAMPTZ,
                        claimed_at TIMESTAMPTZ,
                        claim_token UUID,
                        sent_at TIMESTAMPTZ,
                        last_error TEXT,
                        reprocess_count INTEGER NOT NULL DEFAULT 0,
                        reprocess_requested BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_billing_events (
                        id BIGSERIAL PRIMARY KEY,
                        workspace_uuid UUID NOT NULL,
                        source_session_id BIGINT NOT NULL,
                        source_session_uuid UUID NOT NULL,
                        billing_period DATE NOT NULL,
                        occurred_at TIMESTAMPTZ NOT NULL,
                        metric_code TEXT NOT NULL,
                        service_code TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        snapshot_id TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (workspace_uuid, source_session_uuid, billing_period, metric_code)
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (id, uuid, created_at)
                    SELECT
                        item,
                        ('00000000-0000-0000-0000-' || lpad(item::text, 12, '0'))::uuid,
                        TIMESTAMPTZ '2026-08-28 12:00:00+00' + make_interval(secs => item)
                    FROM generate_series(1, 450) AS item
                    """
                )
            )
            created = await reconcile_billing_events(
                db_session,
                workspace_uuid=workspace_uuid,
                period_start=datetime(2026, 8, 1, tzinfo=timezone.utc),
                period_end=datetime(2026, 9, 1, tzinfo=timezone.utc),
                settings=settings,  # type: ignore[arg-type]
            )
            quantities: list[int] = []
            while True:
                snapshot = await aggregate_next_billing_snapshot(
                    db_session,
                    workspace_uuid=workspace_uuid,
                    settings=settings,  # type: ignore[arg-type]
                )
                if snapshot is None:
                    break
                quantities.append(snapshot.quantity)

            rows = (
                await db_session.execute(
                    text("SELECT quantity, payload->'items'->0->>'quantity' AS payload_quantity FROM orch_billing_snapshots ORDER BY created_at, snapshot_id")
                )
            ).mappings().all()
            event_status = (
                await db_session.execute(
                    text("SELECT status, COUNT(*) AS total FROM orch_billing_events GROUP BY status")
                )
            ).mappings().one()

            assert created == 450
            assert quantities == [200, 200, 50]
            assert sorted(int(row["quantity"]) for row in rows) == [50, 200, 200]
            assert sorted(int(row["payload_quantity"]) for row in rows) == [50, 200, 200]
            assert dict(event_status) == {"status": "batched", "total": 450}


@pytest.mark.asyncio
async def test_billing_batch_migration_executes_in_rolled_back_schema() -> None:
    """Validate the exact migration SQL and parser without persisting a schema or applying a real migration."""
    schema = f"orch_billing_test_{uuid4().hex}"
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
                        uuid UUID NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
            )
            await _run_migration_file(
                db_session,
                schema=schema,
                migration_path="sql/022_create_orch_billing_batch_tables.sql",
            )
            tables = (
                await db_session.execute(
                    text(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = :schema
                          AND table_name LIKE 'orch_billing_%'
                        ORDER BY table_name
                        """
                    ),
                    {"schema": schema},
                )
            ).scalars().all()
            assert tables == [
                "orch_billing_events",
                "orch_billing_reprocess_requests",
                "orch_billing_snapshots",
            ]
        finally:
            await transaction.rollback()
