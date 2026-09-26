from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.services.journey_workspace_snapshot_service import (
    JourneyWorkspaceSnapshotPayloadTooLargeError,
    _canonical_snapshot,
    _now,
    claim_journey_workspace_snapshot,
    cleanup_expired_journey_facts,
    fail_claimed_journey_workspace_snapshot,
    fetch_current_journey_workspace_snapshot,
    fetch_journey_workspace_snapshot_sequence,
    mark_journey_workspace_snapshot_dirty,
    store_claimed_journey_workspace_snapshot,
)
from app.services.migration_service import _run_migration_file


BASE_MIGRATION_PATH = "sql/023_create_orch_journey_metrics_tables.sql"
STATE_MIGRATION_PATH = "sql/024_create_orch_journey_workspace_snapshot_state.sql"


def test_snapshot_serializer_rejects_payload_above_hard_limit() -> None:
    with pytest.raises(JourneyWorkspaceSnapshotPayloadTooLargeError):
        _canonical_snapshot({"payload": "x" * (1024 * 1024)})


def test_snapshot_service_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _now(datetime(2026, 9, 25, 12, 0, 0))


@pytest.mark.asyncio
async def test_snapshot_state_coalesces_changes_and_preserves_new_generation() -> None:
    schema = f"orch_journey_state_test_{uuid4().hex}"
    safe_schema = schema.replace('"', '""')
    session_factory = get_session_factory()
    started_at = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

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
                migration_path=STATE_MIGRATION_PATH,
            )

            assert await mark_journey_workspace_snapshot_dirty(
                db_session,
                observed_at=started_at,
            ) == 1
            assert await mark_journey_workspace_snapshot_dirty(
                db_session,
                observed_at=started_at + timedelta(seconds=1),
            ) == 2
            assert await mark_journey_workspace_snapshot_dirty(
                db_session,
                observed_at=started_at - timedelta(seconds=1),
            ) == 3
            assert await claim_journey_workspace_snapshot(
                db_session,
                claimed_at=started_at + timedelta(seconds=2),
            ) is None

            first_claim = await claim_journey_workspace_snapshot(
                db_session,
                claimed_at=started_at + timedelta(seconds=3),
            )
            assert first_claim is not None
            assert first_claim.generation == 3
            assert first_claim.next_snapshot_sequence == 1
            assert first_claim.dirty_since == started_at - timedelta(seconds=1)
            assert first_claim.latest_dirty_at == started_at + timedelta(seconds=1)

            assert await mark_journey_workspace_snapshot_dirty(
                db_session,
                observed_at=started_at + timedelta(seconds=4),
            ) == 4
            first_snapshot = await store_claimed_journey_workspace_snapshot(
                db_session,
                claim=first_claim,
                snapshot={"type": "orchestration_workspace_snapshot", "value": 1},
                built_at=started_at + timedelta(seconds=5),
            )
            assert first_snapshot.snapshot_sequence == 1
            assert first_snapshot.remains_dirty is True
            current = await fetch_current_journey_workspace_snapshot(db_session)
            assert current is not None
            assert current.snapshot_id == first_snapshot.snapshot_id
            assert current.snapshot_sequence == 1
            assert current.payload["value"] == 1
            assert await fetch_journey_workspace_snapshot_sequence(db_session) == 1

            assert await claim_journey_workspace_snapshot(
                db_session,
                claimed_at=started_at + timedelta(seconds=6),
            ) is None
            second_claim = await claim_journey_workspace_snapshot(
                db_session,
                claimed_at=started_at + timedelta(seconds=7),
            )
            assert second_claim is not None
            assert second_claim.generation == 4
            assert second_claim.next_snapshot_sequence == 2
            second_snapshot = await store_claimed_journey_workspace_snapshot(
                db_session,
                claim=second_claim,
                snapshot={"type": "orchestration_workspace_snapshot", "value": 2},
                built_at=started_at + timedelta(seconds=8),
            )
            assert second_snapshot.snapshot_sequence == 2
            assert second_snapshot.remains_dirty is False

            state = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            dirty_generation,
                            built_generation,
                            snapshot_sequence,
                            dirty_since,
                            claim_token,
                            attempt_count
                        FROM orch_journey_workspace_snapshot_state
                        """
                    )
                )
            ).mappings().one()
            assert dict(state) == {
                "dirty_generation": 4,
                "built_generation": 4,
                "snapshot_sequence": 2,
                "dirty_since": None,
                "claim_token": None,
                "attempt_count": 0,
            }
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_snapshot_failed_claim_retries_without_losing_dirty_state() -> None:
    schema = f"orch_journey_retry_test_{uuid4().hex}"
    safe_schema = schema.replace('"', '""')
    session_factory = get_session_factory()
    started_at = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

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
                migration_path=STATE_MIGRATION_PATH,
            )
            await mark_journey_workspace_snapshot_dirty(
                db_session,
                observed_at=started_at,
            )
            claim = await claim_journey_workspace_snapshot(
                db_session,
                claimed_at=started_at + timedelta(seconds=3),
            )
            assert claim is not None
            assert await fail_claimed_journey_workspace_snapshot(
                db_session,
                claim=claim,
                error_code="synthetic_failure",
                failed_at=started_at + timedelta(seconds=4),
                retry_seconds=5,
            ) is True
            assert await claim_journey_workspace_snapshot(
                db_session,
                claimed_at=started_at + timedelta(seconds=8),
            ) is None
            retry_claim = await claim_journey_workspace_snapshot(
                db_session,
                claimed_at=started_at + timedelta(seconds=9),
            )
            assert retry_claim is not None
            assert retry_claim.attempt_count == 1
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_cleanup_removes_only_expired_terminal_sessions() -> None:
    schema = f"orch_journey_cleanup_test_{uuid4().hex}"
    safe_schema = schema.replace('"', '""')
    session_factory = get_session_factory()
    cleaned_at = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

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
                migration_path=STATE_MIGRATION_PATH,
            )
            await db_session.execute(
                text(
                    """
                    UPDATE orch_journey_settings
                    SET retention_days = 1
                    WHERE singleton_id = 1
                    """
                )
            )

            for lifecycle_status, age_days in (
                ("completed", 2),
                ("in_progress", 2),
                ("completed", 0),
            ):
                session_uuid = uuid4()
                source_session_id = (
                    await db_session.execute(
                        text(
                            """
                            INSERT INTO orch_sessions (uuid)
                            VALUES (:session_uuid)
                            RETURNING id
                            """
                        ),
                        {"session_uuid": session_uuid},
                    )
                ).scalar_one()
                timestamp = cleaned_at - timedelta(days=age_days)
                ended_at = None if lifecycle_status == "in_progress" else timestamp
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_journey_sessions (
                            source_session_id,
                            session_uuid,
                            flow_uuid,
                            lifecycle_status,
                            started_at,
                            last_progress_at,
                            ended_at
                        )
                        VALUES (
                            :source_session_id,
                            :session_uuid,
                            :flow_uuid,
                            :lifecycle_status,
                            CAST(:timestamp AS TIMESTAMPTZ),
                            CAST(:timestamp AS TIMESTAMPTZ),
                            CAST(:ended_at AS TIMESTAMPTZ)
                        )
                        """
                    ),
                    {
                        "source_session_id": source_session_id,
                        "session_uuid": session_uuid,
                        "flow_uuid": uuid4(),
                        "lifecycle_status": lifecycle_status,
                        "timestamp": timestamp,
                        "ended_at": ended_at,
                    },
                )

            assert await cleanup_expired_journey_facts(
                db_session,
                cleaned_at=cleaned_at,
                batch_size=10,
            ) == 1
            remaining = (
                await db_session.execute(
                    text(
                        """
                        SELECT lifecycle_status, COUNT(*) AS count
                        FROM orch_journey_sessions
                        GROUP BY lifecycle_status
                        ORDER BY lifecycle_status
                        """
                    )
                )
            ).all()
            assert remaining == [("completed", 1), ("in_progress", 1)]
        finally:
            await transaction.rollback()
