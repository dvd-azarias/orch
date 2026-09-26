from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.workspace import get_current_workspace_schema
from app.services.alarm_service import persist_alarm
from app.services.journey_workspace_snapshot_service import (
    mark_journey_workspace_snapshot_dirty,
)

logger = get_logger(__name__)


STAGE_ORDINALS = {
    "entrada": 1,
    "identificacao": 2,
    "qualificacao": 3,
    "abordagem": 4,
    "proposta": 5,
    "decisao": 6,
    "desfecho": 7,
}

WAITING_STOP_REASONS = {
    "frozen_wait_active",
    "scheduled_wait",
}
ABANDONED_STOP_REASONS = {
    "end_of_branch",
    "no_next_card",
}


@dataclass(frozen=True)
class JourneyMetricsContext:
    journey_session_id: int
    source_session_id: int
    session_uuid: str
    flow_uuid: str
    flow_revision_id: str


@dataclass(frozen=True)
class JourneyChannelAction:
    action_id: str
    action_sequence: int
    created: bool


def _normalize_stage_token(raw_value: Any) -> tuple[str | None, str | None]:
    value = raw_value
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("id") or value.get("value") or value.get("name")
    if value is None or not str(value).strip():
        return None, "missing"

    normalized = unicodedata.normalize("NFKD", str(value).strip())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.lower().replace("-", "_").replace(" ", "_")
    if normalized not in STAGE_ORDINALS:
        return None, "invalid"
    return normalized, None


def _component_ref_id(component: dict[str, Any], card_cursor: str) -> str:
    raw_value = component.get("ref_id") or card_cursor
    return str(UUID(str(raw_value)))


def _component_stage(component: dict[str, Any]) -> tuple[str | None, str | None]:
    parameters = component.get("parameters")
    if not isinstance(parameters, dict):
        return None, "missing"
    return _normalize_stage_token(parameters.get("stage"))


async def _set_workspace_search_path(db_session: AsyncSession) -> None:
    safe_schema = get_current_workspace_schema().replace('"', '""')
    await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))


async def _journey_tables_available(db_session: AsyncSession) -> bool:
    result = await db_session.execute(
        text("SELECT to_regclass('orch_journey_settings') IS NOT NULL")
    )
    return bool(result.scalar_one())


async def _flow_coverage_active(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
) -> bool:
    row = (
        await db_session.execute(
            text(
                """
                SELECT status
                FROM orch_journey_flow_coverage
                WHERE flow_uuid = CAST(:flow_uuid AS uuid)
                """
            ),
            {"flow_uuid": flow_uuid},
        )
    ).mappings().first()
    return row is not None and row["status"] == "active"


async def _touch_flow_coverage(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    occurred_at: datetime,
) -> None:
    await db_session.execute(
        text(
            """
            UPDATE orch_journey_flow_coverage
            SET last_fact_at = GREATEST(
                    COALESCE(last_fact_at, CAST(:occurred_at AS timestamptz)),
                    CAST(:occurred_at AS timestamptz)
                ),
                updated_at = NOW()
            WHERE flow_uuid = CAST(:flow_uuid AS uuid)
              AND status = 'active'
            """
        ),
        {"flow_uuid": flow_uuid, "occurred_at": occurred_at},
    )


async def _persist_instrumentation_failure(
    db_session: AsyncSession,
    *,
    operation: str,
    flow_uuid: str,
    source_session_id: int,
    session_uuid: str | None,
    exc: Exception,
) -> None:
    logger.exception(
        "journey metrics instrumentation failed",
        extra={
            "event": "orch.journey_metrics.instrumentation_failed",
            "operation": operation,
            "flow_uuid": flow_uuid,
            "session_id": source_session_id,
            "session_uuid": session_uuid,
            "error_type": type(exc).__name__,
        },
    )
    try:
        tx_context = (
            db_session.begin_nested()
            if db_session.in_transaction()
            else db_session.begin()
        )
        async with tx_context:
            await _set_workspace_search_path(db_session)
            await persist_alarm(
                db_session,
                level="error",
                code="journey_metrics_instrumentation_failed",
                message=(
                    "A telemetria de jornada falhou sem interromper a execução do fluxo."
                ),
                details={
                    "operation": operation,
                    "source_session_id": source_session_id,
                    "error_type": type(exc).__name__,
                },
                flow_uuid=flow_uuid,
                session_uuid=session_uuid,
            )
    except Exception as alarm_exc:
        logger.exception(
            "journey metrics instrumentation alarm failed",
            extra={
                "event": "orch.journey_metrics.instrumentation_alarm_failed",
                "operation": operation,
                "flow_uuid": flow_uuid,
                "session_id": source_session_id,
                "session_uuid": session_uuid,
                "error_type": type(alarm_exc).__name__,
            },
        )


async def initialize_journey_session_metrics(
    db_session: AsyncSession,
    *,
    source_session_id: int,
    flow_uuid: str,
    flow_revision_id: str,
    session_scope: str | None,
) -> JourneyMetricsContext | None:
    session_uuid: str | None = None
    try:
        tx_context = (
            db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
        )
        async with tx_context:
            await _set_workspace_search_path(db_session)
            if not await _journey_tables_available(db_session):
                return None

            enabled = (
                await db_session.execute(
                    text(
                        """
                        SELECT enabled
                        FROM orch_journey_settings
                        WHERE singleton_id = 1
                        """
                    )
                )
            ).scalar_one_or_none()
            if enabled is not True:
                return None

            now = datetime.now(timezone.utc)
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_journey_flow_coverage (
                        flow_uuid,
                        status,
                        coverage_started_at,
                        last_fact_at
                    )
                    VALUES (
                        CAST(:flow_uuid AS uuid),
                        'active',
                        CAST(:now AS timestamptz),
                        CAST(:now AS timestamptz)
                    )
                    ON CONFLICT (flow_uuid) DO NOTHING
                    """
                ),
                {"flow_uuid": flow_uuid, "now": now},
            )
            if not await _flow_coverage_active(db_session, flow_uuid=flow_uuid):
                return None

            source_row = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            id,
                            uuid::text AS session_uuid,
                            flow_uuid::text AS flow_uuid,
                            state,
                            started_at,
                            ended_at,
                            abandoned_at,
                            created_at
                        FROM orch_sessions
                        WHERE id = :source_session_id
                        FOR UPDATE
                        """
                    ),
                    {"source_session_id": source_session_id},
                )
            ).mappings().first()
            if source_row is None:
                return None
            if str(source_row["flow_uuid"]) != str(flow_uuid):
                raise ValueError("source_session_flow_mismatch")

            session_uuid = str(source_row["session_uuid"])
            lifecycle_status = "in_progress"
            if source_row["abandoned_at"] is not None or int(source_row["state"]) == 5:
                lifecycle_status = "abandoned"
            elif int(source_row["state"]) == 3:
                lifecycle_status = "failed"

            projection_row = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            id,
                            session_uuid::text AS session_uuid,
                            flow_uuid::text AS flow_uuid,
                            flow_revision_id::text AS flow_revision_id,
                            session_scope
                        FROM orch_journey_sessions
                        WHERE source_session_id = :source_session_id
                        FOR UPDATE
                        """
                    ),
                    {"source_session_id": source_session_id},
                )
            ).mappings().first()
            projection_changed = projection_row is None
            if projection_row is None:
                projection_row = (
                    await db_session.execute(
                        text(
                            """
                            INSERT INTO orch_journey_sessions (
                                source_session_id,
                                session_uuid,
                                flow_uuid,
                                flow_revision_id,
                                session_scope,
                                lifecycle_status,
                                started_at,
                                last_progress_at,
                                ended_at
                            )
                            VALUES (
                                :source_session_id,
                                CAST(:session_uuid AS uuid),
                                CAST(:flow_uuid AS uuid),
                                CAST(:flow_revision_id AS uuid),
                                :session_scope,
                                :lifecycle_status,
                                COALESCE(
                                    CAST(:started_at AS timestamptz),
                                    CAST(:created_at AS timestamptz),
                                    CAST(:now AS timestamptz)
                                ),
                                COALESCE(
                                    CAST(:started_at AS timestamptz),
                                    CAST(:created_at AS timestamptz),
                                    CAST(:now AS timestamptz)
                                ),
                                CASE
                                    WHEN :lifecycle_status IN ('abandoned', 'failed')
                                    THEN COALESCE(
                                        CAST(:abandoned_at AS timestamptz),
                                        CAST(:ended_at AS timestamptz),
                                        CAST(:now AS timestamptz)
                                    )
                                    ELSE NULL
                                END
                            )
                            RETURNING
                                id,
                                session_uuid::text AS session_uuid,
                                flow_uuid::text AS flow_uuid,
                                flow_revision_id::text AS flow_revision_id,
                                session_scope
                            """
                        ),
                        {
                            "source_session_id": source_session_id,
                            "session_uuid": session_uuid,
                            "flow_uuid": flow_uuid,
                            "flow_revision_id": flow_revision_id,
                            "session_scope": session_scope,
                            "lifecycle_status": lifecycle_status,
                            "started_at": source_row["started_at"],
                            "created_at": source_row["created_at"],
                            "ended_at": source_row["ended_at"],
                            "abandoned_at": source_row["abandoned_at"],
                            "now": now,
                        },
                    )
                ).mappings().one()
            elif projection_row["session_scope"] is None and session_scope is not None:
                projection_row = (
                    await db_session.execute(
                        text(
                            """
                            UPDATE orch_journey_sessions
                            SET session_scope = :session_scope,
                                updated_at = NOW()
                            WHERE id = :journey_session_id
                            RETURNING
                                id,
                                session_uuid::text AS session_uuid,
                                flow_uuid::text AS flow_uuid,
                                flow_revision_id::text AS flow_revision_id,
                                session_scope
                            """
                        ),
                        {
                            "journey_session_id": int(projection_row["id"]),
                            "session_scope": session_scope,
                        },
                    )
                ).mappings().one()
                projection_changed = True
            if str(projection_row["flow_uuid"]) != str(flow_uuid):
                raise ValueError("journey_projection_flow_mismatch")
            if str(projection_row["flow_revision_id"]) != str(flow_revision_id):
                raise ValueError("journey_projection_revision_mismatch")

            if projection_changed:
                await _touch_flow_coverage(
                    db_session,
                    flow_uuid=flow_uuid,
                    occurred_at=now,
                )
                await mark_journey_workspace_snapshot_dirty(
                    db_session,
                    observed_at=now,
                )
            return JourneyMetricsContext(
                journey_session_id=int(projection_row["id"]),
                source_session_id=source_session_id,
                session_uuid=session_uuid,
                flow_uuid=flow_uuid,
                flow_revision_id=flow_revision_id,
            )
    except Exception as exc:
        await _persist_instrumentation_failure(
            db_session,
            operation="initialize_session",
            flow_uuid=flow_uuid,
            source_session_id=source_session_id,
            session_uuid=session_uuid,
            exc=exc,
        )
        return None


async def _stage_warning_already_recorded(
    db_session: AsyncSession,
    *,
    context: JourneyMetricsContext,
    component_ref_id: str,
    warning_code: str,
) -> bool:
    if not (
        await db_session.execute(
            text("SELECT to_regclass('orch_sessions_alarms') IS NOT NULL")
        )
    ).scalar_one():
        return False
    return bool(
        (
            await db_session.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM orch_sessions_alarms
                        WHERE session_uuid = CAST(:session_uuid AS uuid)
                          AND flow_uuid = CAST(:flow_uuid AS uuid)
                          AND code = :warning_code
                          AND details->>'component_ref_id' = :component_ref_id
                          AND details->>'flow_revision_id' = :flow_revision_id
                    )
                    """
                ),
                {
                    "session_uuid": context.session_uuid,
                    "flow_uuid": context.flow_uuid,
                    "warning_code": warning_code,
                    "component_ref_id": component_ref_id,
                    "flow_revision_id": context.flow_revision_id,
                },
            )
        ).scalar_one()
    )


async def record_journey_component_entry(
    db_session: AsyncSession,
    *,
    context: JourneyMetricsContext | None,
    component: dict[str, Any],
    card_cursor: str,
    component_kind: str,
    occurred_at: datetime | None = None,
) -> None:
    if context is None:
        return

    warning_issue: str | None = None
    warning_component_ref_id: str | None = None
    try:
        component_ref_id = _component_ref_id(component, card_cursor)
        stage_id, stage_issue = _component_stage(component)
        now = occurred_at or datetime.now(timezone.utc)
        tx_context = (
            db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
        )
        async with tx_context:
            await _set_workspace_search_path(db_session)
            if not await _flow_coverage_active(db_session, flow_uuid=context.flow_uuid):
                return

            projection = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            lifecycle_status,
                            current_stage,
                            current_stage_ordinal,
                            highest_stage,
                            highest_stage_ordinal,
                            current_component_ref_id::text AS current_component_ref_id
                        FROM orch_journey_sessions
                        WHERE id = :journey_session_id
                          AND source_session_id = :source_session_id
                        FOR UPDATE
                        """
                    ),
                    {
                        "journey_session_id": context.journey_session_id,
                        "source_session_id": context.source_session_id,
                    },
                )
            ).mappings().first()
            if projection is None or projection["lifecycle_status"] in {
                "completed",
                "abandoned",
                "failed",
            }:
                return

            open_visit = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            id,
                            component_ref_id::text AS component_ref_id,
                            stage_id
                        FROM orch_journey_stage_visits
                        WHERE journey_session_id = :journey_session_id
                          AND exited_at IS NULL
                        ORDER BY id DESC
                        LIMIT 1
                        """
                    ),
                    {"journey_session_id": context.journey_session_id},
                )
            ).mappings().first()

            same_open_component = (
                stage_id is not None
                and open_visit is not None
                and str(open_visit["component_ref_id"]) == component_ref_id
                and open_visit["stage_id"] == stage_id
            ) or (
                stage_id is None
                and open_visit is None
                and projection["current_stage"] is None
                and str(projection["current_component_ref_id"] or "")
                == component_ref_id
            )
            if same_open_component:
                if projection["lifecycle_status"] == "waiting":
                    await db_session.execute(
                        text(
                            """
                            UPDATE orch_journey_sessions
                            SET lifecycle_status = 'in_progress',
                                last_progress_at = GREATEST(
                                    last_progress_at,
                                    CAST(:occurred_at AS timestamptz)
                                ),
                                updated_at = NOW()
                            WHERE id = :journey_session_id
                            """
                        ),
                        {
                            "journey_session_id": context.journey_session_id,
                            "occurred_at": now,
                        },
                    )
                    await _touch_flow_coverage(
                        db_session,
                        flow_uuid=context.flow_uuid,
                        occurred_at=now,
                    )
                    await mark_journey_workspace_snapshot_dirty(
                        db_session,
                        observed_at=now,
                    )
                return

            await db_session.execute(
                text(
                    """
                    UPDATE orch_journey_stage_visits
                    SET exited_at = GREATEST(
                            entered_at,
                            CAST(:occurred_at AS timestamptz)
                        ),
                        exit_kind = COALESCE(exit_kind, 'transition'),
                        updated_at = NOW()
                    WHERE journey_session_id = :journey_session_id
                      AND exited_at IS NULL
                    """
                ),
                {
                    "journey_session_id": context.journey_session_id,
                    "occurred_at": now,
                },
            )

            if stage_id is None:
                await db_session.execute(
                    text(
                        """
                        UPDATE orch_journey_sessions
                        SET lifecycle_status = 'in_progress',
                            current_stage = NULL,
                            current_stage_ordinal = NULL,
                            current_component_ref_id = CAST(:component_ref_id AS uuid),
                            last_progress_at = GREATEST(
                                last_progress_at,
                                CAST(:occurred_at AS timestamptz)
                            ),
                            updated_at = NOW()
                        WHERE id = :journey_session_id
                        """
                    ),
                    {
                        "journey_session_id": context.journey_session_id,
                        "component_ref_id": component_ref_id,
                        "occurred_at": now,
                    },
                )
                warning_issue = stage_issue or "missing"
                warning_component_ref_id = component_ref_id
            else:
                stage_ordinal = STAGE_ORDINALS[stage_id]
                visit_number = int(
                    (
                        await db_session.execute(
                            text(
                                """
                                SELECT COALESCE(MAX(visit_number), 0) + 1
                                FROM orch_journey_stage_visits
                                WHERE journey_session_id = :journey_session_id
                                  AND component_ref_id = CAST(:component_ref_id AS uuid)
                                """
                            ),
                            {
                                "journey_session_id": context.journey_session_id,
                                "component_ref_id": component_ref_id,
                            },
                        )
                    ).scalar_one()
                )
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_journey_stage_visits (
                            journey_session_id,
                            session_uuid,
                            flow_uuid,
                            flow_revision_id,
                            component_ref_id,
                            component_kind,
                            stage_id,
                            stage_ordinal,
                            previous_stage_id,
                            previous_stage_ordinal,
                            visit_number,
                            entered_at
                        )
                        VALUES (
                            :journey_session_id,
                            CAST(:session_uuid AS uuid),
                            CAST(:flow_uuid AS uuid),
                            CAST(:flow_revision_id AS uuid),
                            CAST(:component_ref_id AS uuid),
                            :component_kind,
                            :stage_id,
                            :stage_ordinal,
                            :previous_stage_id,
                            :previous_stage_ordinal,
                            :visit_number,
                            CAST(:entered_at AS timestamptz)
                        )
                        """
                    ),
                    {
                        "journey_session_id": context.journey_session_id,
                        "session_uuid": context.session_uuid,
                        "flow_uuid": context.flow_uuid,
                        "flow_revision_id": context.flow_revision_id,
                        "component_ref_id": component_ref_id,
                        "component_kind": component_kind or "unknown",
                        "stage_id": stage_id,
                        "stage_ordinal": stage_ordinal,
                        "previous_stage_id": projection["current_stage"],
                        "previous_stage_ordinal": projection["current_stage_ordinal"],
                        "visit_number": visit_number,
                        "entered_at": now,
                    },
                )
                await db_session.execute(
                    text(
                        """
                        UPDATE orch_journey_sessions
                        SET lifecycle_status = 'in_progress',
                            current_stage = :stage_id,
                            current_stage_ordinal = :stage_ordinal,
                            highest_stage = CASE
                                WHEN highest_stage_ordinal IS NULL
                                  OR :stage_ordinal > highest_stage_ordinal
                                THEN :stage_id
                                ELSE highest_stage
                            END,
                            highest_stage_ordinal = CASE
                                WHEN highest_stage_ordinal IS NULL
                                  OR :stage_ordinal > highest_stage_ordinal
                                THEN :stage_ordinal
                                ELSE highest_stage_ordinal
                            END,
                            current_component_ref_id = CAST(:component_ref_id AS uuid),
                            last_progress_at = GREATEST(
                                last_progress_at,
                                CAST(:occurred_at AS timestamptz)
                            ),
                            updated_at = NOW()
                        WHERE id = :journey_session_id
                        """
                    ),
                    {
                        "journey_session_id": context.journey_session_id,
                        "stage_id": stage_id,
                        "stage_ordinal": stage_ordinal,
                        "component_ref_id": component_ref_id,
                        "occurred_at": now,
                    },
                )

            await _touch_flow_coverage(
                db_session,
                flow_uuid=context.flow_uuid,
                occurred_at=now,
            )
            await mark_journey_workspace_snapshot_dirty(
                db_session,
                observed_at=now,
            )

        if warning_issue is not None and warning_component_ref_id is not None:
            warning_code = f"journey_metrics_stage_{warning_issue}"
            tx_context = (
                db_session.begin_nested()
                if db_session.in_transaction()
                else db_session.begin()
            )
            async with tx_context:
                await _set_workspace_search_path(db_session)
                already_recorded = await _stage_warning_already_recorded(
                    db_session,
                    context=context,
                    component_ref_id=warning_component_ref_id,
                    warning_code=warning_code,
                )
            if not already_recorded:
                await persist_alarm(
                    db_session,
                    level="warning",
                    code=warning_code,
                    message=(
                        "O card executado não possui uma etapa de jornada válida na revisão fixada."
                    ),
                    details={
                        "source_session_id": context.source_session_id,
                        "flow_revision_id": context.flow_revision_id,
                        "component_ref_id": warning_component_ref_id,
                        "component_kind": component_kind or "unknown",
                    },
                    flow_uuid=context.flow_uuid,
                    session_uuid=context.session_uuid,
                )
    except Exception as exc:
        await _persist_instrumentation_failure(
            db_session,
            operation="component_entry",
            flow_uuid=context.flow_uuid,
            source_session_id=context.source_session_id,
            session_uuid=context.session_uuid,
            exc=exc,
        )


async def record_journey_component_transition(
    db_session: AsyncSession,
    *,
    context: JourneyMetricsContext | None,
    component: dict[str, Any],
    card_cursor: str,
    occurred_at: datetime | None = None,
) -> None:
    if context is None:
        return
    try:
        component_ref_id = _component_ref_id(component, card_cursor)
        now = occurred_at or datetime.now(timezone.utc)
        tx_context = (
            db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
        )
        async with tx_context:
            await _set_workspace_search_path(db_session)
            if not await _flow_coverage_active(db_session, flow_uuid=context.flow_uuid):
                return
            closed_visit_id = (
                await db_session.execute(
                text(
                    """
                    UPDATE orch_journey_stage_visits
                    SET exited_at = GREATEST(
                            entered_at,
                            CAST(:occurred_at AS timestamptz)
                        ),
                        exit_kind = COALESCE(exit_kind, 'transition'),
                        updated_at = NOW()
                    WHERE journey_session_id = :journey_session_id
                      AND component_ref_id = CAST(:component_ref_id AS uuid)
                      AND exited_at IS NULL
                    RETURNING id
                    """
                ),
                {
                    "journey_session_id": context.journey_session_id,
                    "component_ref_id": component_ref_id,
                    "occurred_at": now,
                },
                )
            ).scalar_one_or_none()
            if closed_visit_id is None:
                return
            await db_session.execute(
                text(
                    """
                    UPDATE orch_journey_sessions
                    SET last_progress_at = GREATEST(
                            last_progress_at,
                            CAST(:occurred_at AS timestamptz)
                        ),
                        updated_at = NOW()
                    WHERE id = :journey_session_id
                      AND lifecycle_status IN ('in_progress', 'waiting')
                    """
                ),
                {
                    "journey_session_id": context.journey_session_id,
                    "occurred_at": now,
                },
            )
            await _touch_flow_coverage(
                db_session,
                flow_uuid=context.flow_uuid,
                occurred_at=now,
            )
            await mark_journey_workspace_snapshot_dirty(
                db_session,
                observed_at=now,
            )
    except Exception as exc:
        await _persist_instrumentation_failure(
            db_session,
            operation="component_transition",
            flow_uuid=context.flow_uuid,
            source_session_id=context.source_session_id,
            session_uuid=context.session_uuid,
            exc=exc,
        )


def _lifecycle_for_finalize(
    *,
    stopped_reason: str,
    source_state: int,
    source_abandoned_at: datetime | None,
) -> tuple[str, str | None, str | None]:
    if stopped_reason == "finished_by_component":
        return "completed", stopped_reason, "completed"
    if source_abandoned_at is not None or source_state == 5:
        return "abandoned", stopped_reason, "abandoned"
    if stopped_reason in ABANDONED_STOP_REASONS:
        return "abandoned", stopped_reason, "abandoned"
    if source_state == 3:
        return "failed", stopped_reason, "failed"
    if stopped_reason in WAITING_STOP_REASONS or stopped_reason.startswith("blocked_"):
        return "waiting", None, None
    return "in_progress", None, None


async def finalize_journey_session_metrics(
    db_session: AsyncSession,
    *,
    context: JourneyMetricsContext | None,
    stopped_reason: str,
    occurred_at: datetime | None = None,
) -> None:
    if context is None:
        return
    try:
        now = occurred_at or datetime.now(timezone.utc)
        tx_context = (
            db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
        )
        async with tx_context:
            await _set_workspace_search_path(db_session)
            if not await _flow_coverage_active(db_session, flow_uuid=context.flow_uuid):
                return
            source_row = (
                await db_session.execute(
                    text(
                        """
                        SELECT state, ended_at, abandoned_at
                        FROM orch_sessions
                        WHERE id = :source_session_id
                        """
                    ),
                    {"source_session_id": context.source_session_id},
                )
            ).mappings().first()
            if source_row is None:
                return

            lifecycle_status, terminal_outcome, terminal_class = _lifecycle_for_finalize(
                stopped_reason=stopped_reason,
                source_state=int(source_row["state"]),
                source_abandoned_at=source_row["abandoned_at"],
            )
            is_terminal = lifecycle_status in {"completed", "abandoned", "failed"}
            ended_at = (
                source_row["abandoned_at"] or source_row["ended_at"] or now
                if is_terminal
                else None
            )
            projection = (
                await db_session.execute(
                    text(
                        """
                        SELECT lifecycle_status, ended_at, terminal_outcome, terminal_class
                        FROM orch_journey_sessions
                        WHERE id = :journey_session_id
                        FOR UPDATE
                        """
                    ),
                    {"journey_session_id": context.journey_session_id},
                )
            ).mappings().first()
            if projection is None:
                return
            if projection["lifecycle_status"] in {"completed", "abandoned", "failed"}:
                return

            changed = (
                projection["lifecycle_status"] != lifecycle_status
                or projection["ended_at"] != ended_at
                or projection["terminal_outcome"] != terminal_outcome
                or projection["terminal_class"] != terminal_class
            )
            if not changed:
                return

            await db_session.execute(
                text(
                    """
                    UPDATE orch_journey_sessions
                    SET lifecycle_status = :lifecycle_status,
                        ended_at = CAST(:ended_at AS timestamptz),
                        terminal_outcome = :terminal_outcome,
                        terminal_class = :terminal_class,
                        last_progress_at = CASE
                            WHEN CAST(:is_terminal AS boolean)
                            THEN GREATEST(
                                last_progress_at,
                                CAST(:occurred_at AS timestamptz)
                            )
                            ELSE last_progress_at
                        END,
                        updated_at = NOW()
                    WHERE id = :journey_session_id
                    """
                ),
                {
                    "journey_session_id": context.journey_session_id,
                    "lifecycle_status": lifecycle_status,
                    "ended_at": ended_at,
                    "terminal_outcome": terminal_outcome,
                    "terminal_class": terminal_class,
                    "is_terminal": is_terminal,
                    "occurred_at": now,
                },
            )
            if is_terminal:
                await db_session.execute(
                    text(
                        """
                        UPDATE orch_journey_stage_visits
                        SET exited_at = GREATEST(
                                entered_at,
                                CAST(:occurred_at AS timestamptz)
                            ),
                            exit_kind = COALESCE(exit_kind, :exit_kind),
                            updated_at = NOW()
                        WHERE journey_session_id = :journey_session_id
                          AND exited_at IS NULL
                        """
                    ),
                    {
                        "journey_session_id": context.journey_session_id,
                        "occurred_at": ended_at or now,
                        "exit_kind": terminal_class,
                    },
                )
            await _touch_flow_coverage(
                db_session,
                flow_uuid=context.flow_uuid,
                occurred_at=ended_at or now,
            )
            await mark_journey_workspace_snapshot_dirty(
                db_session,
                observed_at=ended_at or now,
            )
    except Exception as exc:
        await _persist_instrumentation_failure(
            db_session,
            operation="finalize_session",
            flow_uuid=context.flow_uuid,
            source_session_id=context.source_session_id,
            session_uuid=context.session_uuid,
            exc=exc,
        )


def _channel_action_status(
    *,
    channel: str,
    native_status: str,
) -> tuple[str, str]:
    normalized_channel = str(channel or "").strip().lower()
    normalized_native = str(native_status or "").strip().lower()
    if normalized_channel == "voice":
        if normalized_native == "failed":
            return "failed", "failed"
        if normalized_native in {
            "answered",
            "busy",
            "machine",
            "no_answer",
            "rejected",
            "invalid_number",
            "limit_reached",
        }:
            return "completed", normalized_native
    if normalized_channel == "whatsapp":
        mapping = {
            "sent": ("sent", "sent"),
            "delivered": ("delivered", "delivered"),
            "read": ("engaged", "read"),
            "failed": ("failed", "failed"),
            "limit_reached": ("failed", "limit_reached"),
        }
        if normalized_native in mapping:
            return mapping[normalized_native]
    if normalized_channel == "sms":
        mapping = {
            "accepted": ("accepted", "accepted"),
            "sent": ("sent", "sent"),
            "delivered": ("delivered", "delivered"),
            "response": ("engaged", "response"),
            "not_delivered": ("failed", "not_delivered"),
            "failed": ("failed", "failed"),
        }
        if normalized_native in mapping:
            return mapping[normalized_native]
    if normalized_channel == "rcs":
        mapping = {
            "accepted": ("accepted", "accepted"),
            "sent": ("sent", "sent"),
            "delivered": ("delivered", "delivered"),
            "read": ("engaged", "read"),
            "response": ("engaged", "response"),
            "unavailable": ("failed", "unavailable"),
            "expired": ("failed", "expired"),
            "failed": ("failed", "failed"),
        }
        if normalized_native in mapping:
            return mapping[normalized_native]
    return "unknown", normalized_native or "unknown"


def _safe_action_text(value: Any, *, field: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"invalid_{field}")
    return normalized


async def _insert_or_fetch_journey_channel_action(
    db_session: AsyncSession,
    *,
    journey_session_id: int,
    flow_uuid: str,
    flow_revision_id: str,
    component_ref_id: str,
    component_kind: str,
    channel: str,
    source_kind: str,
    source_id: str,
    requested_at: datetime,
    action_sequence: int | None,
    provider_reference: str | None,
) -> JourneyChannelAction:
    existing = (
        await db_session.execute(
            text(
                """
                SELECT action_id::text AS action_id, action_sequence
                FROM orch_journey_channel_actions
                WHERE source_kind = :source_kind
                  AND source_id = :source_id
                """
            ),
            {"source_kind": source_kind, "source_id": source_id},
        )
    ).mappings().first()
    if existing is not None:
        return JourneyChannelAction(
            action_id=str(existing["action_id"]),
            action_sequence=int(existing["action_sequence"]),
            created=False,
        )

    sequence = action_sequence
    if sequence is None:
        sequence = int(
            (
                await db_session.execute(
                    text(
                        """
                        SELECT COALESCE(MAX(action_sequence), 0) + 1
                        FROM orch_journey_channel_actions
                        WHERE journey_session_id = :journey_session_id
                          AND component_ref_id = CAST(:component_ref_id AS uuid)
                          AND channel = :channel
                        """
                    ),
                    {
                        "journey_session_id": journey_session_id,
                        "component_ref_id": component_ref_id,
                        "channel": channel,
                    },
                )
            ).scalar_one()
        )
    if int(sequence) < 1:
        raise ValueError("invalid_action_sequence")

    provider_reference_hash = (
        hashlib.sha256(str(provider_reference).encode("utf-8")).hexdigest()
        if provider_reference
        else None
    )
    inserted = (
        await db_session.execute(
            text(
                """
                INSERT INTO orch_journey_channel_actions (
                    journey_session_id,
                    source_kind,
                    source_id,
                    flow_uuid,
                    flow_revision_id,
                    component_ref_id,
                    component_kind,
                    channel,
                    action_sequence,
                    lifecycle_status,
                    requested_at,
                    provider_reference_hash
                )
                VALUES (
                    :journey_session_id,
                    :source_kind,
                    :source_id,
                    CAST(:flow_uuid AS uuid),
                    CAST(:flow_revision_id AS uuid),
                    CAST(:component_ref_id AS uuid),
                    :component_kind,
                    :channel,
                    :action_sequence,
                    'requested',
                    CAST(:requested_at AS timestamptz),
                    :provider_reference_hash
                )
                ON CONFLICT (source_kind, source_id) DO NOTHING
                RETURNING action_id::text AS action_id, action_sequence
                """
            ),
            {
                "journey_session_id": journey_session_id,
                "source_kind": source_kind,
                "source_id": source_id,
                "flow_uuid": flow_uuid,
                "flow_revision_id": flow_revision_id,
                "component_ref_id": component_ref_id,
                "component_kind": component_kind,
                "channel": channel,
                "action_sequence": int(sequence),
                "requested_at": requested_at,
                "provider_reference_hash": provider_reference_hash,
            },
        )
    ).mappings().first()
    was_created = inserted is not None
    if inserted is None:
        inserted = (
            await db_session.execute(
                text(
                    """
                    SELECT action_id::text AS action_id, action_sequence
                    FROM orch_journey_channel_actions
                    WHERE source_kind = :source_kind
                      AND source_id = :source_id
                    """
                ),
                {"source_kind": source_kind, "source_id": source_id},
            )
        ).mappings().one()
    return JourneyChannelAction(
        action_id=str(inserted["action_id"]),
        action_sequence=int(inserted["action_sequence"]),
        created=was_created,
    )


async def record_journey_channel_action_requested(
    db_session: AsyncSession,
    *,
    context: JourneyMetricsContext | None,
    component: dict[str, Any],
    component_kind: str,
    channel: str,
    source_kind: str,
    source_id: str,
    action_sequence: int | None = None,
    requested_at: datetime | None = None,
    provider_reference: str | None = None,
) -> JourneyChannelAction | None:
    if context is None:
        return None
    try:
        safe_channel = _safe_action_text(channel, field="channel", maximum=32).lower()
        safe_source_kind = _safe_action_text(
            source_kind,
            field="source_kind",
            maximum=128,
        )
        safe_source_id = _safe_action_text(source_id, field="source_id", maximum=512)
        component_ref_id = _component_ref_id(component, str(component.get("ref_id") or ""))
        safe_requested_at = requested_at or datetime.now(timezone.utc)
        tx_context = (
            db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
        )
        async with tx_context:
            await _set_workspace_search_path(db_session)
            projection = (
                await db_session.execute(
                    text(
                        """
                        SELECT id
                        FROM orch_journey_sessions
                        WHERE id = :journey_session_id
                          AND source_session_id = :source_session_id
                          AND flow_uuid = CAST(:flow_uuid AS uuid)
                        FOR UPDATE
                        """
                    ),
                    {
                        "journey_session_id": context.journey_session_id,
                        "source_session_id": context.source_session_id,
                        "flow_uuid": context.flow_uuid,
                    },
                )
            ).first()
            if projection is None:
                return None
            action = await _insert_or_fetch_journey_channel_action(
                db_session,
                journey_session_id=context.journey_session_id,
                flow_uuid=context.flow_uuid,
                flow_revision_id=context.flow_revision_id,
                component_ref_id=component_ref_id,
                component_kind=component_kind or "unknown",
                channel=safe_channel,
                source_kind=safe_source_kind,
                source_id=safe_source_id,
                requested_at=safe_requested_at,
                action_sequence=action_sequence,
                provider_reference=provider_reference,
            )
            if action.created:
                await mark_journey_workspace_snapshot_dirty(
                    db_session,
                    observed_at=safe_requested_at,
                )
            return action
    except Exception as exc:
        await _persist_instrumentation_failure(
            db_session,
            operation="channel_action_requested",
            flow_uuid=context.flow_uuid,
            source_session_id=context.source_session_id,
            session_uuid=context.session_uuid,
            exc=exc,
        )
        return None


async def record_journey_channel_action_event(
    db_session: AsyncSession,
    *,
    source_session_id: int,
    flow_uuid: str,
    session_uuid: str | None,
    channel: str,
    source_kind: str,
    source_id: str,
    native_status: str,
    event_id: str,
    occurred_at: datetime | None,
    received_at: datetime | None = None,
    component_ref_id: str | None = None,
    component_kind: str | None = None,
    action_sequence: int | None = None,
    provider_reference: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> JourneyChannelAction | None:
    resolved_session_uuid = session_uuid
    try:
        safe_channel = _safe_action_text(channel, field="channel", maximum=32).lower()
        safe_source_kind = _safe_action_text(
            source_kind,
            field="source_kind",
            maximum=128,
        )
        safe_source_id = _safe_action_text(source_id, field="source_id", maximum=512)
        safe_native_status = _safe_action_text(
            native_status,
            field="native_status",
            maximum=128,
        ).lower()
        safe_event_id = _safe_action_text(event_id, field="event_id", maximum=512)
        safe_received_at = received_at or datetime.now(timezone.utc)
        safe_occurred_at = occurred_at or safe_received_at
        safe_metadata = metadata if isinstance(metadata, dict) else {}
        if len(json.dumps(safe_metadata, ensure_ascii=False, default=str)) > 4096:
            raise ValueError("action_event_metadata_too_large")

        tx_context = (
            db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
        )
        async with tx_context:
            await _set_workspace_search_path(db_session)
            projection = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            id,
                            session_uuid::text AS session_uuid,
                            flow_revision_id::text AS flow_revision_id,
                            current_component_ref_id::text AS current_component_ref_id
                        FROM orch_journey_sessions
                        WHERE source_session_id = :source_session_id
                          AND flow_uuid = CAST(:flow_uuid AS uuid)
                        FOR UPDATE
                        """
                    ),
                    {
                        "source_session_id": source_session_id,
                        "flow_uuid": flow_uuid,
                    },
                )
            ).mappings().first()
            if projection is None:
                return None
            resolved_session_uuid = str(projection["session_uuid"])
            resolved_component_ref_id = str(
                component_ref_id or projection["current_component_ref_id"] or ""
            ).strip()
            resolved_component_ref_id = str(UUID(resolved_component_ref_id))
            action = await _insert_or_fetch_journey_channel_action(
                db_session,
                journey_session_id=int(projection["id"]),
                flow_uuid=flow_uuid,
                flow_revision_id=str(projection["flow_revision_id"]),
                component_ref_id=resolved_component_ref_id,
                component_kind=component_kind or f"send_with_{safe_channel}",
                channel=safe_channel,
                source_kind=safe_source_kind,
                source_id=safe_source_id,
                requested_at=safe_occurred_at,
                action_sequence=action_sequence,
                provider_reference=provider_reference,
            )
            event_key = hashlib.sha256(
                (
                    f"{safe_source_kind}\x00{safe_source_id}\x00"
                    f"{safe_native_status}\x00{safe_event_id}"
                ).encode("utf-8")
            ).hexdigest()
            lifecycle_status, normalized_status = _channel_action_status(
                channel=safe_channel,
                native_status=safe_native_status,
            )
            inserted_event = (
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_journey_channel_action_events (
                            action_id,
                            event_key,
                            event_type,
                            native_status,
                            normalized_status,
                            occurred_at,
                            received_at,
                            processed_at,
                            metadata
                        )
                        VALUES (
                            CAST(:action_id AS uuid),
                            :event_key,
                            'status',
                            :native_status,
                            :normalized_status,
                            CAST(:occurred_at AS timestamptz),
                            CAST(:received_at AS timestamptz),
                            CAST(:received_at AS timestamptz),
                            CAST(:metadata AS jsonb)
                        )
                        ON CONFLICT (action_id, event_key) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "action_id": action.action_id,
                        "event_key": event_key,
                        "native_status": safe_native_status,
                        "normalized_status": normalized_status,
                        "occurred_at": safe_occurred_at,
                        "received_at": safe_received_at,
                        "metadata": json.dumps(safe_metadata, ensure_ascii=False),
                    },
                )
            ).scalar_one_or_none()
            if inserted_event is None:
                return action

            latest = (
                await db_session.execute(
                    text(
                        """
                        SELECT native_status, normalized_status
                        FROM orch_journey_channel_action_events
                        WHERE action_id = CAST(:action_id AS uuid)
                        ORDER BY COALESCE(occurred_at, received_at) DESC, id DESC
                        LIMIT 1
                        """
                    ),
                    {"action_id": action.action_id},
                )
            ).mappings().one()
            latest_lifecycle, latest_normalized = _channel_action_status(
                channel=safe_channel,
                native_status=str(latest["native_status"] or ""),
            )
            event_time = safe_occurred_at
            await db_session.execute(
                text(
                    """
                    UPDATE orch_journey_channel_actions
                    SET lifecycle_status = :lifecycle_status,
                        native_outcome = :native_outcome,
                        normalized_outcome = :normalized_outcome,
                        accepted_at = CASE
                            WHEN :event_lifecycle = 'accepted'
                            THEN COALESCE(accepted_at, CAST(:event_time AS timestamptz))
                            ELSE accepted_at
                        END,
                        sent_at = CASE
                            WHEN :event_lifecycle = 'sent'
                            THEN COALESCE(sent_at, CAST(:event_time AS timestamptz))
                            ELSE sent_at
                        END,
                        delivered_at = CASE
                            WHEN :event_lifecycle = 'delivered'
                            THEN COALESCE(delivered_at, CAST(:event_time AS timestamptz))
                            ELSE delivered_at
                        END,
                        engaged_at = CASE
                            WHEN :event_lifecycle = 'engaged'
                            THEN COALESCE(engaged_at, CAST(:event_time AS timestamptz))
                            ELSE engaged_at
                        END,
                        failed_at = CASE
                            WHEN :event_lifecycle = 'failed'
                            THEN COALESCE(failed_at, CAST(:event_time AS timestamptz))
                            ELSE failed_at
                        END,
                        completed_at = CASE
                            WHEN :event_lifecycle IN ('completed', 'failed')
                            THEN COALESCE(completed_at, CAST(:event_time AS timestamptz))
                            ELSE completed_at
                        END,
                        provider_reference_hash = COALESCE(
                            provider_reference_hash,
                            :provider_reference_hash
                        ),
                        updated_at = NOW()
                    WHERE action_id = CAST(:action_id AS uuid)
                    """
                ),
                {
                    "action_id": action.action_id,
                    "lifecycle_status": latest_lifecycle,
                    "native_outcome": latest["native_status"],
                    "normalized_outcome": latest_normalized,
                    "event_lifecycle": lifecycle_status,
                    "event_time": event_time,
                    "provider_reference_hash": (
                        hashlib.sha256(str(provider_reference).encode("utf-8")).hexdigest()
                        if provider_reference
                        else None
                    ),
                },
            )
            await mark_journey_workspace_snapshot_dirty(
                db_session,
                observed_at=safe_received_at,
            )
            return action
    except Exception as exc:
        await _persist_instrumentation_failure(
            db_session,
            operation="channel_action_event",
            flow_uuid=flow_uuid,
            source_session_id=source_session_id,
            session_uuid=resolved_session_uuid,
            exc=exc,
        )
        return None
