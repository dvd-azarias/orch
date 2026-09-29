from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.channel_supplier_v2_service import (
    build_channel_dispatch_correlation_key,
)
from app.services.journey_metrics_service import (
    record_journey_channel_action_event,
)


@dataclass(frozen=True)
class JourneySupplierProjectionResult:
    voice_scanned: int = 0
    voice_projected: int = 0
    channel_scanned: int = 0
    channel_projected: int = 0


def voice_provider_metadata(
    *,
    raw_outcome: Any,
    payload: Any,
) -> dict[str, Any]:
    raw_payload = dict(payload) if isinstance(payload, Mapping) else {}
    hangup = raw_payload.get("hangup")
    if not isinstance(hangup, Mapping):
        hangup = {}

    provider_status = str(raw_outcome or "").strip() or None
    if provider_status is None:
        for candidate in (
            hangup.get("ReleaseText"),
            hangup.get("Disposition"),
            hangup.get("Cause"),
            raw_payload.get("release"),
            raw_payload.get("status"),
        ):
            text_value = str(candidate or "").strip()
            if text_value:
                provider_status = text_value
                break

    duration_seconds: int | None = None
    for candidate in (
        hangup.get("Billsec"),
        hangup.get("Duration"),
        raw_payload.get("Billsec"),
        raw_payload.get("Duration"),
        raw_payload.get("duration_seconds"),
    ):
        try:
            parsed = int(float(str(candidate).strip()))
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            duration_seconds = parsed
            break

    metadata: dict[str, Any] = {"provider_status": provider_status}
    if duration_seconds is not None:
        metadata["duration_seconds"] = duration_seconds
    error_code = str(raw_payload.get("error_code") or "").strip()
    error_message = str(raw_payload.get("error_message") or "").strip()
    if error_code:
        metadata["error_code"] = error_code
    if error_message:
        metadata["error_message"] = error_message
    return metadata


async def _tables_available(
    db_session: AsyncSession,
    *,
    table_names: tuple[str, ...],
) -> bool:
    result = await db_session.execute(
        text(
            """
            SELECT BOOL_AND(to_regclass(table_name) IS NOT NULL)
            FROM unnest(CAST(:table_names AS text[])) AS names(table_name)
            """
        ),
        {"table_names": list(table_names)},
    )
    return bool(result.scalar_one())


async def _project_voice_attempts(
    db_session: AsyncSession,
    *,
    batch_size: int,
) -> tuple[int, int]:
    if not await _tables_available(
        db_session,
        table_names=(
            "orch_journey_sessions",
            "orch_journey_channel_actions",
            "contact_supplier_dial_cycles_v2",
            "contact_supplier_dial_attempts_v2",
            "contact_supplier_dial_events_v2",
        ),
    ):
        return 0, 0

    dialing_rows = (
        await db_session.execute(
            text(
                """
                SELECT
                    attempt.id::text AS attempt_id,
                    attempt.attempt_sequence,
                    attempt.provider_action_id,
                    attempt.provider_unique_id,
                    attempt.started_at,
                    cycle.session_uuid::text AS session_uuid,
                    cycle.flow_uuid::text AS flow_uuid,
                    cycle.component_ref_id,
                    journey.source_session_id,
                    event.id::text AS event_id,
                    event.raw_outcome,
                    event.payload,
                    event.occurred_at
                FROM contact_supplier_dial_events_v2 AS event
                JOIN contact_supplier_dial_attempts_v2 AS attempt
                  ON attempt.id = event.attempt_id
                JOIN contact_supplier_dial_cycles_v2 AS cycle
                  ON cycle.id = attempt.cycle_id
                JOIN orch_journey_sessions AS journey
                  ON journey.session_uuid = cycle.session_uuid
                 AND journey.flow_uuid = cycle.flow_uuid
                LEFT JOIN orch_journey_channel_actions AS action
                  ON action.source_kind = 'dialer_supplier_v2_attempt'
                 AND action.source_id = attempt.id::text
                WHERE event.provider_source = 'service_dialer'
                  AND event.normalized_outcome = 'dialing'
                  AND event.processed_at IS NOT NULL
                  AND (
                      action.action_id IS NULL
                      OR (
                          action.sent_at IS NULL
                          AND action.completed_at IS NULL
                      )
                  )
                ORDER BY event.occurred_at, event.id
                LIMIT :batch_size
                """
            ),
            {"batch_size": batch_size},
        )
    ).mappings().all()

    projected = 0
    for row in dialing_rows:
        metadata = voice_provider_metadata(
            raw_outcome=row["raw_outcome"],
            payload=row["payload"],
        )
        metadata.update(
            {
                "supplier_attempt_sequence": int(row["attempt_sequence"]),
                "projection_source": "supplier_v2_reconciler",
            }
        )
        action = await record_journey_channel_action_event(
            db_session,
            source_session_id=int(row["source_session_id"]),
            flow_uuid=str(row["flow_uuid"]),
            session_uuid=str(row["session_uuid"]),
            channel="voice",
            source_kind="dialer_supplier_v2_attempt",
            source_id=str(row["attempt_id"]),
            native_status="dialing",
            event_id=str(row["event_id"]),
            occurred_at=row["occurred_at"] or row["started_at"],
            component_ref_id=str(row["component_ref_id"]),
            component_kind="send_with_dialer_handoff",
            provider_reference=(
                str(
                    row["provider_unique_id"]
                    or row["provider_action_id"]
                    or row["attempt_id"]
                )
            ),
            metadata=metadata,
        )
        if action is not None:
            projected += 1

    rows = (
        await db_session.execute(
            text(
                """
                SELECT
                    attempt.id::text AS attempt_id,
                    attempt.attempt_sequence,
                    attempt.provider_action_id,
                    attempt.provider_unique_id,
                    attempt.outcome,
                    attempt.completed_at,
                    cycle.session_uuid::text AS session_uuid,
                    cycle.flow_uuid::text AS flow_uuid,
                    cycle.component_ref_id,
                    journey.source_session_id,
                    event.id::text AS event_id,
                    event.normalized_outcome,
                    event.raw_outcome,
                    event.payload,
                    event.occurred_at,
                    event.decision,
                    event.terminal,
                    event.terminal_reason
                FROM contact_supplier_dial_attempts_v2 AS attempt
                JOIN contact_supplier_dial_cycles_v2 AS cycle
                  ON cycle.id = attempt.cycle_id
                JOIN orch_journey_sessions AS journey
                  ON journey.session_uuid = cycle.session_uuid
                 AND journey.flow_uuid = cycle.flow_uuid
                JOIN LATERAL (
                    SELECT
                        dial_event.id,
                        dial_event.normalized_outcome,
                        dial_event.raw_outcome,
                        dial_event.payload,
                        dial_event.occurred_at,
                        dial_event.decision,
                        dial_event.terminal,
                        dial_event.terminal_reason
                    FROM contact_supplier_dial_events_v2 AS dial_event
                    WHERE dial_event.attempt_id = attempt.id
                      AND dial_event.processed_at IS NOT NULL
                      AND dial_event.normalized_outcome IS NOT NULL
                      AND dial_event.normalized_outcome <> 'dialing'
                    ORDER BY dial_event.processed_at DESC, dial_event.id DESC
                    LIMIT 1
                ) AS event ON TRUE
                LEFT JOIN orch_journey_channel_actions AS action
                  ON action.source_kind = 'dialer_supplier_v2_attempt'
                 AND action.source_id = attempt.id::text
                WHERE attempt.state = 'completed'
                  AND attempt.outcome IS NOT NULL
                  AND (
                      action.action_id IS NULL
                      OR action.completed_at IS NULL
                  )
                ORDER BY attempt.completed_at, attempt.id
                LIMIT :batch_size
                """
            ),
            {"batch_size": batch_size},
        )
    ).mappings().all()

    for row in rows:
        outcome = str(row["normalized_outcome"] or row["outcome"] or "").strip()
        if not outcome:
            continue
        metadata = voice_provider_metadata(
            raw_outcome=row["raw_outcome"],
            payload=row["payload"],
        )
        metadata.update(
            {
                "decision": row["decision"],
                "terminal": bool(row["terminal"]),
                "terminal_reason": row["terminal_reason"],
                "supplier_attempt_sequence": int(row["attempt_sequence"]),
                "projection_source": "supplier_v2_reconciler",
            }
        )
        action = await record_journey_channel_action_event(
            db_session,
            source_session_id=int(row["source_session_id"]),
            flow_uuid=str(row["flow_uuid"]),
            session_uuid=str(row["session_uuid"]),
            channel="voice",
            source_kind="dialer_supplier_v2_attempt",
            source_id=str(row["attempt_id"]),
            native_status=outcome,
            event_id=str(row["event_id"]),
            occurred_at=row["occurred_at"] or row["completed_at"],
            component_ref_id=str(row["component_ref_id"]),
            component_kind="send_with_dialer_handoff",
            provider_reference=(
                str(
                    row["provider_unique_id"]
                    or row["provider_action_id"]
                    or row["attempt_id"]
                )
            ),
            metadata=metadata,
        )
        if action is not None:
            projected += 1
    return len(dialing_rows) + len(rows), projected


async def _project_accepted_channel_dispatches(
    db_session: AsyncSession,
    *,
    batch_size: int,
) -> tuple[int, int]:
    if not await _tables_available(
        db_session,
        table_names=(
            "orch_journey_sessions",
            "orch_journey_channel_actions",
            "contact_supplier_channel_dispatches_v2",
        ),
    ):
        return 0, 0

    rows = (
        await db_session.execute(
            text(
                """
                SELECT
                    dispatch.id::text AS dispatch_id,
                    dispatch.session_uuid::text AS session_uuid,
                    dispatch.flow_uuid::text AS flow_uuid,
                    dispatch.flow_revision_id::text AS flow_revision_id,
                    dispatch.component_ref_id,
                    dispatch.channel,
                    dispatch.dispatch_sequence,
                    dispatch.provider_message_id,
                    dispatch.provider_status,
                    dispatch.accepted_at,
                    journey.source_session_id
                FROM contact_supplier_channel_dispatches_v2 AS dispatch
                JOIN orch_journey_sessions AS journey
                  ON journey.session_uuid = dispatch.session_uuid
                 AND journey.flow_uuid = dispatch.flow_uuid
                LEFT JOIN orch_journey_channel_actions AS action
                  ON action.journey_session_id = journey.id
                 AND action.component_ref_id::text = dispatch.component_ref_id
                 AND action.channel = dispatch.channel
                 AND action.action_sequence = dispatch.dispatch_sequence
                WHERE dispatch.state = 'accepted'
                  AND dispatch.accepted_at IS NOT NULL
                  AND (
                      action.action_id IS NULL
                      OR action.accepted_at IS NULL
                  )
                ORDER BY dispatch.accepted_at, dispatch.id
                LIMIT :batch_size
                """
            ),
            {"batch_size": batch_size},
        )
    ).mappings().all()

    scanned = 0
    projected = 0
    for row in rows:
        correlation_key = build_channel_dispatch_correlation_key(
            session_uuid=str(row["session_uuid"]),
            flow_uuid=str(row["flow_uuid"]),
            flow_revision_id=str(row["flow_revision_id"]),
            component_ref_id=str(row["component_ref_id"]),
            channel=str(row["channel"]),
            dispatch_sequence=int(row["dispatch_sequence"]),
        )
        scanned += 1
        action = await record_journey_channel_action_event(
            db_session,
            source_session_id=int(row["source_session_id"]),
            flow_uuid=str(row["flow_uuid"]),
            session_uuid=str(row["session_uuid"]),
            channel=str(row["channel"]),
            source_kind="channel_supplier_v2_dispatch",
            source_id=correlation_key,
            native_status="accepted",
            event_id=f"dispatch:{row['dispatch_id']}:accepted",
            occurred_at=row["accepted_at"],
            component_ref_id=str(row["component_ref_id"]),
            component_kind=f"send_with_{row['channel']}",
            action_sequence=int(row["dispatch_sequence"]),
            provider_reference=str(
                row["provider_message_id"] or row["dispatch_id"]
            ),
            metadata={
                "provider_status": row["provider_status"],
                "projection_source": "supplier_v2_reconciler",
            },
        )
        if action is not None:
            projected += 1
    return scanned, projected


async def project_supplier_v2_journey_actions(
    db_session: AsyncSession,
    *,
    batch_size: int = 500,
) -> JourneySupplierProjectionResult:
    safe_batch_size = max(1, min(int(batch_size), 1000))
    voice_scanned, voice_projected = await _project_voice_attempts(
        db_session,
        batch_size=safe_batch_size,
    )
    channel_scanned, channel_projected = await _project_accepted_channel_dispatches(
        db_session,
        batch_size=safe_batch_size,
    )
    return JourneySupplierProjectionResult(
        voice_scanned=voice_scanned,
        voice_projected=voice_projected,
        channel_scanned=channel_scanned,
        channel_projected=channel_projected,
    )
