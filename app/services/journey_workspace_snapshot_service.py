from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


SNAPSHOT_MAX_BYTES = 1024 * 1024
DEFAULT_DEBOUNCE_SECONDS = 3
DEFAULT_LEASE_SECONDS = 30
DEFAULT_RETRY_SECONDS = 5
DEFAULT_CLEANUP_BATCH_SIZE = 500
MAX_CLEANUP_BATCH_SIZE = 1000


class JourneyWorkspaceSnapshotError(RuntimeError):
    pass


class JourneyWorkspaceSnapshotClaimLostError(JourneyWorkspaceSnapshotError):
    pass


class JourneyWorkspaceSnapshotPayloadTooLargeError(JourneyWorkspaceSnapshotError):
    pass


@dataclass(frozen=True)
class JourneyWorkspaceSnapshotClaim:
    token: UUID
    generation: int
    next_snapshot_sequence: int
    dirty_since: datetime
    latest_dirty_at: datetime
    attempt_count: int


@dataclass(frozen=True)
class JourneyWorkspaceSnapshotStored:
    snapshot_id: UUID
    snapshot_sequence: int
    snapshot_bytes: int
    snapshot_sha256: str
    remains_dirty: bool


@dataclass(frozen=True)
class JourneyWorkspaceSnapshotCurrent:
    snapshot_id: UUID
    snapshot_sequence: int
    payload: dict[str, Any]
    built_at: datetime


def _now(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("O instante precisa conter timezone.")
    return result


def _positive_seconds(value: int, *, field: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 3600:
        raise ValueError(f"{field} precisa estar entre 1 e 3600 segundos.")
    return parsed


def _canonical_snapshot(snapshot: dict[str, Any]) -> tuple[str, int, str]:
    if not isinstance(snapshot, dict):
        raise TypeError("O snapshot precisa ser um objeto JSON.")
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    size = len(encoded)
    if size > SNAPSHOT_MAX_BYTES:
        raise JourneyWorkspaceSnapshotPayloadTooLargeError(
            f"Snapshot com {size} bytes excede o limite de {SNAPSHOT_MAX_BYTES}."
        )
    return encoded.decode("utf-8"), size, hashlib.sha256(encoded).hexdigest()


async def journey_workspace_snapshot_is_due(
    db_session: AsyncSession,
    *,
    checked_at: datetime | None = None,
) -> bool:
    safe_checked_at = _now(checked_at)
    result = await db_session.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM orch_journey_workspace_snapshot_state AS state
                CROSS JOIN orch_journey_settings AS settings
                WHERE state.singleton_id = 1
                  AND settings.singleton_id = 1
                  AND settings.enabled = TRUE
                  AND state.dirty_since IS NOT NULL
                  AND state.built_generation < state.dirty_generation
                  AND state.next_attempt_at <= CAST(:checked_at AS timestamptz)
                  AND (
                      state.claim_token IS NULL
                      OR state.claim_expires_at <= CAST(:checked_at AS timestamptz)
                  )
            )
            """
        ),
        {"checked_at": safe_checked_at},
    )
    return bool(result.scalar_one())


async def fetch_current_journey_workspace_snapshot(
    db_session: AsyncSession,
) -> JourneyWorkspaceSnapshotCurrent | None:
    row = (
        await db_session.execute(
            text(
                """
                SELECT
                    snapshot_id,
                    snapshot_sequence,
                    snapshot_payload,
                    last_built_at
                FROM orch_journey_workspace_snapshot_state
                WHERE singleton_id = 1
                  AND snapshot_payload IS NOT NULL
                """
            )
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    return JourneyWorkspaceSnapshotCurrent(
        snapshot_id=row["snapshot_id"],
        snapshot_sequence=int(row["snapshot_sequence"]),
        payload=dict(row["snapshot_payload"]),
        built_at=row["last_built_at"],
    )


async def fetch_journey_workspace_snapshot_sequence(
    db_session: AsyncSession,
) -> int:
    value = await db_session.scalar(
        text(
            """
            SELECT snapshot_sequence
            FROM orch_journey_workspace_snapshot_state
            WHERE singleton_id = 1
            """
        )
    )
    return int(value or 0)


async def mark_journey_workspace_snapshot_notified(
    db_session: AsyncSession,
    *,
    snapshot_sequence: int,
    notified_at: datetime | None = None,
) -> bool:
    safe_notified_at = _now(notified_at)
    result = await db_session.execute(
        text(
            """
            UPDATE orch_journey_workspace_snapshot_state
            SET last_notification_at = CAST(:notified_at AS timestamptz),
                updated_at = GREATEST(updated_at, CAST(:notified_at AS timestamptz))
            WHERE singleton_id = 1
              AND snapshot_sequence = :snapshot_sequence
            """
        ),
        {
            "snapshot_sequence": int(snapshot_sequence),
            "notified_at": safe_notified_at,
        },
    )
    return result.rowcount == 1


async def mark_journey_workspace_snapshot_dirty(
    db_session: AsyncSession,
    *,
    observed_at: datetime | None = None,
    debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
) -> int:
    safe_observed_at = _now(observed_at)
    safe_debounce_seconds = _positive_seconds(
        debounce_seconds,
        field="debounce_seconds",
    )
    next_attempt_at = safe_observed_at + timedelta(seconds=safe_debounce_seconds)
    result = await db_session.execute(
        text(
            """
            INSERT INTO orch_journey_workspace_snapshot_state (
                singleton_id,
                dirty_generation,
                built_generation,
                dirty_since,
                latest_dirty_at,
                next_attempt_at
            )
            VALUES (
                1,
                1,
                0,
                CAST(:observed_at AS timestamptz),
                CAST(:observed_at AS timestamptz),
                CAST(:next_attempt_at AS timestamptz)
            )
            ON CONFLICT (singleton_id) DO UPDATE
            SET
                dirty_generation =
                    orch_journey_workspace_snapshot_state.dirty_generation + 1,
                dirty_since = CASE
                    WHEN orch_journey_workspace_snapshot_state.dirty_since IS NULL
                    THEN EXCLUDED.dirty_since
                    ELSE LEAST(
                        orch_journey_workspace_snapshot_state.dirty_since,
                        EXCLUDED.dirty_since
                    )
                END,
                latest_dirty_at = CASE
                    WHEN orch_journey_workspace_snapshot_state.latest_dirty_at IS NULL
                    THEN EXCLUDED.latest_dirty_at
                    ELSE GREATEST(
                        orch_journey_workspace_snapshot_state.latest_dirty_at,
                        EXCLUDED.latest_dirty_at
                    )
                END,
                next_attempt_at = CASE
                    WHEN orch_journey_workspace_snapshot_state.dirty_since IS NULL
                    THEN EXCLUDED.next_attempt_at
                    ELSE orch_journey_workspace_snapshot_state.next_attempt_at
                END,
                updated_at = GREATEST(
                    orch_journey_workspace_snapshot_state.updated_at,
                    CAST(:observed_at AS timestamptz)
                )
            RETURNING dirty_generation
            """
        ),
        {
            "observed_at": safe_observed_at,
            "next_attempt_at": next_attempt_at,
        },
    )
    return int(result.scalar_one())


async def claim_journey_workspace_snapshot(
    db_session: AsyncSession,
    *,
    claimed_at: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> JourneyWorkspaceSnapshotClaim | None:
    safe_claimed_at = _now(claimed_at)
    safe_lease_seconds = _positive_seconds(lease_seconds, field="lease_seconds")
    claim_token = uuid4()
    claim_expires_at = safe_claimed_at + timedelta(seconds=safe_lease_seconds)
    result = await db_session.execute(
        text(
            """
            WITH candidate AS (
                SELECT singleton_id
                FROM orch_journey_workspace_snapshot_state
                WHERE
                    singleton_id = 1
                    AND dirty_since IS NOT NULL
                    AND built_generation < dirty_generation
                    AND next_attempt_at <= CAST(:claimed_at AS timestamptz)
                    AND (
                        claim_token IS NULL
                        OR claim_expires_at <= CAST(:claimed_at AS timestamptz)
                    )
                FOR UPDATE SKIP LOCKED
            )
            UPDATE orch_journey_workspace_snapshot_state AS state
            SET
                claim_token = :claim_token,
                claim_generation = state.dirty_generation,
                claimed_at = CAST(:claimed_at AS timestamptz),
                claim_expires_at = CAST(:claim_expires_at AS timestamptz),
                updated_at = CAST(:claimed_at AS timestamptz)
            FROM candidate
            WHERE state.singleton_id = candidate.singleton_id
            RETURNING
                state.claim_generation,
                state.snapshot_sequence + 1 AS next_snapshot_sequence,
                state.dirty_since,
                state.latest_dirty_at,
                state.attempt_count
            """
        ),
        {
            "claim_token": claim_token,
            "claimed_at": safe_claimed_at,
            "claim_expires_at": claim_expires_at,
        },
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None
    return JourneyWorkspaceSnapshotClaim(
        token=claim_token,
        generation=int(row["claim_generation"]),
        next_snapshot_sequence=int(row["next_snapshot_sequence"]),
        dirty_since=row["dirty_since"],
        latest_dirty_at=row["latest_dirty_at"],
        attempt_count=int(row["attempt_count"]),
    )


async def store_claimed_journey_workspace_snapshot(
    db_session: AsyncSession,
    *,
    claim: JourneyWorkspaceSnapshotClaim,
    snapshot: dict[str, Any],
    snapshot_id: UUID | None = None,
    built_at: datetime | None = None,
    debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
) -> JourneyWorkspaceSnapshotStored:
    safe_built_at = _now(built_at)
    safe_debounce_seconds = _positive_seconds(
        debounce_seconds,
        field="debounce_seconds",
    )
    snapshot_json, snapshot_bytes, snapshot_sha256 = _canonical_snapshot(snapshot)
    safe_snapshot_id = snapshot_id or uuid4()
    result = await db_session.execute(
        text(
            """
            UPDATE orch_journey_workspace_snapshot_state
            SET
                snapshot_sequence = snapshot_sequence + 1,
                snapshot_id = :snapshot_id,
                snapshot_payload = CAST(:snapshot_payload AS jsonb),
                snapshot_bytes = :snapshot_bytes,
                snapshot_sha256 = :snapshot_sha256,
                built_generation = :claim_generation,
                dirty_since = CASE
                    WHEN dirty_generation = :claim_generation THEN NULL
                    ELSE latest_dirty_at
                END,
                latest_dirty_at = CASE
                    WHEN dirty_generation = :claim_generation THEN NULL
                    ELSE latest_dirty_at
                END,
                next_attempt_at = CASE
                    WHEN dirty_generation = :claim_generation
                    THEN CAST(:built_at AS timestamptz)
                    ELSE GREATEST(
                        CAST(:built_at AS timestamptz),
                        latest_dirty_at
                            + CAST(:debounce_seconds AS double precision)
                                * INTERVAL '1 second'
                    )
                END,
                attempt_count = 0,
                claim_token = NULL,
                claim_generation = NULL,
                claimed_at = NULL,
                claim_expires_at = NULL,
                last_built_at = CAST(:built_at AS timestamptz),
                last_error_code = NULL,
                last_error_at = NULL,
                updated_at = CAST(:built_at AS timestamptz)
            WHERE
                singleton_id = 1
                AND claim_token = :claim_token
                AND claim_generation = :claim_generation
            RETURNING
                snapshot_sequence,
                dirty_since IS NOT NULL AS remains_dirty
            """
        ),
        {
            "snapshot_id": safe_snapshot_id,
            "snapshot_payload": snapshot_json,
            "snapshot_bytes": snapshot_bytes,
            "snapshot_sha256": snapshot_sha256,
            "claim_token": claim.token,
            "claim_generation": claim.generation,
            "built_at": safe_built_at,
            "debounce_seconds": safe_debounce_seconds,
        },
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise JourneyWorkspaceSnapshotClaimLostError(
            "O lease do snapshot nao pertence mais a este worker."
        )
    return JourneyWorkspaceSnapshotStored(
        snapshot_id=safe_snapshot_id,
        snapshot_sequence=int(row["snapshot_sequence"]),
        snapshot_bytes=snapshot_bytes,
        snapshot_sha256=snapshot_sha256,
        remains_dirty=bool(row["remains_dirty"]),
    )


async def fail_claimed_journey_workspace_snapshot(
    db_session: AsyncSession,
    *,
    claim: JourneyWorkspaceSnapshotClaim,
    error_code: str,
    failed_at: datetime | None = None,
    retry_seconds: int = DEFAULT_RETRY_SECONDS,
) -> bool:
    safe_failed_at = _now(failed_at)
    safe_retry_seconds = _positive_seconds(retry_seconds, field="retry_seconds")
    safe_error_code = str(error_code or "snapshot_build_failed").strip()[:120]
    next_attempt_at = safe_failed_at + timedelta(seconds=safe_retry_seconds)
    result = await db_session.execute(
        text(
            """
            UPDATE orch_journey_workspace_snapshot_state
            SET
                attempt_count = attempt_count + 1,
                next_attempt_at = CAST(:next_attempt_at AS timestamptz),
                claim_token = NULL,
                claim_generation = NULL,
                claimed_at = NULL,
                claim_expires_at = NULL,
                last_error_code = :error_code,
                last_error_at = CAST(:failed_at AS timestamptz),
                updated_at = CAST(:failed_at AS timestamptz)
            WHERE
                singleton_id = 1
                AND claim_token = :claim_token
                AND claim_generation = :claim_generation
            """
        ),
        {
            "claim_token": claim.token,
            "claim_generation": claim.generation,
            "error_code": safe_error_code,
            "failed_at": safe_failed_at,
            "next_attempt_at": next_attempt_at,
        },
    )
    return result.rowcount == 1


async def cleanup_expired_journey_facts(
    db_session: AsyncSession,
    *,
    cleaned_at: datetime | None = None,
    batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE,
) -> int:
    safe_cleaned_at = _now(cleaned_at)
    safe_batch_size = int(batch_size)
    if safe_batch_size < 1 or safe_batch_size > MAX_CLEANUP_BATCH_SIZE:
        raise ValueError(
            f"batch_size precisa estar entre 1 e {MAX_CLEANUP_BATCH_SIZE}."
        )
    result = await db_session.execute(
        text(
            """
            WITH expired AS (
                SELECT session.id
                FROM orch_journey_sessions AS session
                CROSS JOIN orch_journey_settings AS settings
                WHERE
                    settings.singleton_id = 1
                    AND session.lifecycle_status IN (
                        'completed', 'abandoned', 'failed'
                    )
                    AND COALESCE(
                        session.ended_at,
                        session.last_progress_at,
                        session.started_at
                    ) < CAST(:cleaned_at AS timestamptz)
                        - settings.retention_days * INTERVAL '1 day'
                ORDER BY session.id
                LIMIT :batch_size
                FOR UPDATE OF session SKIP LOCKED
            )
            DELETE FROM orch_journey_sessions AS session
            USING expired
            WHERE session.id = expired.id
            RETURNING session.id
            """
        ),
        {
            "cleaned_at": safe_cleaned_at,
            "batch_size": safe_batch_size,
        },
    )
    return len(result.scalars().all())
