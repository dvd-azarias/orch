from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


REPORTING_STATE_SELECT = """
singleton_id,
status,
coverage_started_at,
activated_at,
activated_by,
created_at,
updated_at
"""


async def fetch_channel_reporting_state(
    db_session: AsyncSession,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            f"""
            SELECT {REPORTING_STATE_SELECT}
            FROM orch_channel_reporting_state
            WHERE singleton_id = 1
            LIMIT 1
            """
        )
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def activate_channel_reporting(
    db_session: AsyncSession,
    *,
    activated_by: str,
) -> dict[str, Any]:
    result = await db_session.execute(
        text(
            f"""
            UPDATE orch_channel_reporting_state
            SET
                status = 'active',
                coverage_started_at = NOW(),
                activated_at = NOW(),
                activated_by = :activated_by,
                updated_at = NOW()
            WHERE singleton_id = 1
              AND status = 'pending'
              AND coverage_started_at IS NULL
              AND activated_at IS NULL
            RETURNING {REPORTING_STATE_SELECT}, TRUE AS newly_activated
            """
        ),
        {"activated_by": activated_by},
    )
    row = result.mappings().first()
    if row is not None:
        return dict(row)

    existing = await fetch_channel_reporting_state(db_session)
    if existing is None:
        raise RuntimeError(
            "Estado de reporting ausente; aplique a migration 0023 antes da ativação."
        )
    if existing.get("status") != "active" or existing.get("coverage_started_at") is None:
        raise RuntimeError("Estado de reporting inválido para ativação.")
    return {**existing, "newly_activated": False}


async def register_channel_action(
    db_session: AsyncSession,
    *,
    session_id: int,
    session_uuid: str,
    flow_uuid: str,
    flow_revision_id: str,
    component_ref_id: str,
    component_kind: str,
    channel: str,
    action_sequence: int,
    source_kind: str,
    source_id: str,
    person_uuid: str | None,
    destination_masked: str | None,
    provider_reference: str | None,
    lifecycle_status: str,
    requested_at: datetime,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            """
            INSERT INTO orch_channel_actions (
                session_id,
                session_uuid,
                flow_uuid,
                flow_revision_id,
                component_ref_id,
                component_kind,
                channel,
                action_sequence,
                source_kind,
                source_id,
                person_uuid,
                destination_masked,
                provider_reference,
                lifecycle_status,
                requested_at,
                queued_at,
                accepted_at,
                sent_at,
                failed_at,
                terminal_at,
                created_at,
                updated_at
            )
            SELECT
                session_row.id,
                session_row.uuid,
                session_row.flow_uuid,
                CAST(:flow_revision_id AS uuid),
                :component_ref_id,
                :component_kind,
                :channel,
                :action_sequence,
                :source_kind,
                :source_id,
                CAST(:person_uuid AS uuid),
                :destination_masked,
                :provider_reference,
                :lifecycle_status,
                CAST(:requested_at AS timestamptz),
                CASE WHEN :lifecycle_status = 'queued' THEN CAST(:requested_at AS timestamptz) END,
                CASE WHEN :lifecycle_status = 'accepted' THEN CAST(:requested_at AS timestamptz) END,
                CASE WHEN :lifecycle_status = 'in_progress' THEN CAST(:requested_at AS timestamptz) END,
                CASE WHEN :lifecycle_status = 'failed' THEN CAST(:requested_at AS timestamptz) END,
                CASE
                    WHEN :lifecycle_status IN ('completed', 'failed', 'cancelled')
                    THEN CAST(:requested_at AS timestamptz)
                END,
                NOW(),
                NOW()
            FROM orch_channel_reporting_state reporting_state
            JOIN orch_sessions session_row
              ON session_row.id = :session_id
             AND session_row.uuid = CAST(:session_uuid AS uuid)
             AND session_row.flow_uuid = CAST(:flow_uuid AS uuid)
            WHERE reporting_state.singleton_id = 1
              AND reporting_state.status = 'active'
              AND reporting_state.coverage_started_at IS NOT NULL
              AND CAST(:requested_at AS timestamptz) >= reporting_state.coverage_started_at
            ON CONFLICT (source_kind, source_id) DO UPDATE
            SET
                provider_reference = COALESCE(
                    EXCLUDED.provider_reference,
                    orch_channel_actions.provider_reference
                ),
                person_uuid = COALESCE(EXCLUDED.person_uuid, orch_channel_actions.person_uuid),
                destination_masked = COALESCE(
                    EXCLUDED.destination_masked,
                    orch_channel_actions.destination_masked
                ),
                updated_at = NOW()
            WHERE orch_channel_actions.session_id = EXCLUDED.session_id
              AND orch_channel_actions.session_uuid = EXCLUDED.session_uuid
              AND orch_channel_actions.flow_uuid = EXCLUDED.flow_uuid
              AND orch_channel_actions.flow_revision_id = EXCLUDED.flow_revision_id
              AND orch_channel_actions.component_ref_id = EXCLUDED.component_ref_id
              AND orch_channel_actions.component_kind = EXCLUDED.component_kind
              AND orch_channel_actions.channel = EXCLUDED.channel
              AND orch_channel_actions.action_sequence = EXCLUDED.action_sequence
            RETURNING
                id,
                uuid::text AS uuid,
                session_id,
                session_uuid::text AS session_uuid,
                flow_uuid::text AS flow_uuid,
                lifecycle_status,
                requested_at
            """
        ),
        {
            "session_id": session_id,
            "session_uuid": session_uuid,
            "flow_uuid": flow_uuid,
            "flow_revision_id": flow_revision_id,
            "component_ref_id": component_ref_id,
            "component_kind": component_kind,
            "channel": channel,
            "action_sequence": action_sequence,
            "source_kind": source_kind,
            "source_id": source_id,
            "person_uuid": person_uuid,
            "destination_masked": destination_masked,
            "provider_reference": provider_reference,
            "lifecycle_status": lifecycle_status,
            "requested_at": requested_at,
        },
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


_MILESTONE_COLUMNS = {
    "queued": "queued_at",
    "accepted": "accepted_at",
    "sent": "sent_at",
    "delivered": "delivered_at",
    "engaged": "engaged_at",
    "failed": "failed_at",
    "terminal": "terminal_at",
}


async def update_channel_action(
    db_session: AsyncSession,
    *,
    source_kind: str,
    source_id: str,
    lifecycle_status: str,
    milestone: str,
    occurred_at: datetime,
    native_outcome: str | None = None,
    provider_reference: str | None = None,
) -> dict[str, Any] | None:
    milestone_column = _MILESTONE_COLUMNS.get(milestone)
    if milestone_column is None:
        raise ValueError(f"Milestone de reporting inválido: {milestone}")

    result = await db_session.execute(
        text(
            f"""
            UPDATE orch_channel_actions
            SET
                lifecycle_status = CASE
                    WHEN lifecycle_status = 'completed' OR :lifecycle_status = 'completed'
                        THEN 'completed'
                    WHEN lifecycle_status IN ('failed', 'cancelled')
                        THEN lifecycle_status
                    WHEN :lifecycle_status IN ('failed', 'cancelled')
                        THEN :lifecycle_status
                    WHEN lifecycle_status = 'uncertain' OR :lifecycle_status = 'uncertain'
                        THEN 'uncertain'
                    WHEN lifecycle_status = 'in_progress' OR :lifecycle_status = 'in_progress'
                        THEN 'in_progress'
                    WHEN lifecycle_status = 'accepted' OR :lifecycle_status = 'accepted'
                        THEN 'accepted'
                    WHEN lifecycle_status = 'queued' OR :lifecycle_status = 'queued'
                        THEN 'queued'
                    ELSE :lifecycle_status
                END,
                {milestone_column} = CASE
                    WHEN {milestone_column} IS NULL THEN CAST(:occurred_at AS timestamptz)
                    ELSE LEAST({milestone_column}, CAST(:occurred_at AS timestamptz))
                END,
                native_outcome = CASE
                    WHEN CAST(:native_outcome AS TEXT) IS NOT NULL
                     AND (
                            native_outcome_at IS NULL
                            OR CAST(:occurred_at AS timestamptz) >= native_outcome_at
                         )
                    THEN CAST(:native_outcome AS TEXT)
                    ELSE native_outcome
                END,
                native_outcome_at = CASE
                    WHEN CAST(:native_outcome AS TEXT) IS NOT NULL
                     AND (
                            native_outcome_at IS NULL
                            OR CAST(:occurred_at AS timestamptz) >= native_outcome_at
                         )
                    THEN CAST(:occurred_at AS timestamptz)
                    ELSE native_outcome_at
                END,
                provider_reference = COALESCE(
                    CAST(:provider_reference AS TEXT),
                    provider_reference
                ),
                terminal_at = CASE
                    WHEN :lifecycle_status IN ('completed', 'failed', 'cancelled')
                    THEN CASE
                        WHEN terminal_at IS NULL THEN CAST(:occurred_at AS timestamptz)
                        ELSE LEAST(terminal_at, CAST(:occurred_at AS timestamptz))
                    END
                    ELSE terminal_at
                END,
                updated_at = NOW()
            WHERE source_kind = :source_kind
              AND source_id = :source_id
            RETURNING
                id,
                uuid::text AS uuid,
                lifecycle_status,
                native_outcome,
                native_outcome_at,
                requested_at,
                queued_at,
                accepted_at,
                sent_at,
                delivered_at,
                engaged_at,
                failed_at,
                terminal_at
            """
        ),
        {
            "source_kind": source_kind,
            "source_id": source_id,
            "lifecycle_status": lifecycle_status,
            "occurred_at": occurred_at,
            "native_outcome": native_outcome,
            "provider_reference": provider_reference,
        },
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def link_channel_event_to_action(
    db_session: AsyncSession,
    *,
    event_row_id: int,
    action_id: int,
) -> bool:
    result = await db_session.execute(
        text(
            """
            UPDATE orch_channel_events channel_event
            SET action_id = channel_action.id
            FROM orch_channel_actions channel_action
            WHERE channel_event.id = :event_row_id
              AND channel_action.id = :action_id
              AND channel_action.session_id = channel_event.session_id
              AND channel_action.flow_uuid = channel_event.flow_uuid
              AND channel_event.action_id IS NULL
            RETURNING channel_event.id
            """
        ),
        {"event_row_id": event_row_id, "action_id": action_id},
    )
    return result.scalar_one_or_none() is not None
