from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_SESSION_REVISION_SQL = "NULLIF(runtime_variables #>> '{workflow_v2,revision_id}', '')"


async def fetch_observed_revisions(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    period_from: datetime,
    period_to: datetime,
) -> list[dict[str, Any]]:
    result = await db_session.execute(
        text(
            f"""
            SELECT
                revision_id::text AS revision_id,
                COUNT(DISTINCT session_id)::bigint AS sessions,
                MIN(created_at) AS first_seen_at,
                MAX(created_at) AS last_seen_at
            FROM orch_session_metrics
            WHERE flow_uuid = CAST(:flow_uuid AS uuid)
              AND metric_type = 'card'
              AND created_at >= :period_from
              AND created_at < :period_to
              AND revision_id IS NOT NULL
            GROUP BY revision_id
            ORDER BY MAX(created_at) DESC
            LIMIT 20
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "period_from": period_from,
            "period_to": period_to,
        },
    )
    return [dict(row) for row in result.mappings().all()]


async def fetch_journey_node_aggregates(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    revision_id: str,
    period_from: datetime,
    period_to: datetime,
) -> list[dict[str, Any]]:
    result = await db_session.execute(
        text(
            """
            SELECT
                card_cursor AS node_id,
                COUNT(DISTINCT session_id)::bigint AS sessions,
                COUNT(*)::bigint AS visits,
                COUNT(*) FILTER (WHERE status = 'success')::bigint AS successes,
                COUNT(*) FILTER (WHERE status = 'error')::bigint AS errors,
                COUNT(*) FILTER (WHERE status IN ('stopped', 'locked'))::bigint AS stopped,
                COALESCE(AVG(latency_ms), 0)::double precision AS average_latency_ms,
                COALESCE(MAX(latency_ms), 0)::double precision AS maximum_latency_ms
            FROM orch_session_metrics
            WHERE flow_uuid = CAST(:flow_uuid AS uuid)
              AND revision_id = CAST(:revision_id AS uuid)
              AND metric_type = 'card'
              AND created_at >= :period_from
              AND created_at < :period_to
              AND card_cursor IS NOT NULL
            GROUP BY card_cursor
            ORDER BY card_cursor
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "revision_id": revision_id,
            "period_from": period_from,
            "period_to": period_to,
        },
    )
    return [dict(row) for row in result.mappings().all()]


async def fetch_journey_edge_aggregates(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    revision_id: str,
    period_from: datetime,
    period_to: datetime,
) -> list[dict[str, Any]]:
    result = await db_session.execute(
        text(
            """
            SELECT
                card_cursor AS source,
                NULLIF(details ->> 'next_card_uuid', '') AS target,
                NULLIF(details ->> 'branch_label', '') AS branch,
                COUNT(DISTINCT session_id)::bigint AS sessions,
                COUNT(*)::bigint AS visits
            FROM orch_session_metrics
            WHERE flow_uuid = CAST(:flow_uuid AS uuid)
              AND revision_id = CAST(:revision_id AS uuid)
              AND metric_type = 'card'
              AND created_at >= :period_from
              AND created_at < :period_to
              AND card_cursor IS NOT NULL
              AND NULLIF(details ->> 'next_card_uuid', '') IS NOT NULL
            GROUP BY
                card_cursor,
                NULLIF(details ->> 'next_card_uuid', ''),
                NULLIF(details ->> 'branch_label', '')
            ORDER BY card_cursor, target, branch
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "revision_id": revision_id,
            "period_from": period_from,
            "period_to": period_to,
        },
    )
    return [dict(row) for row in result.mappings().all()]


async def fetch_journey_aggregate_totals(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    revision_id: str,
    period_from: datetime,
    period_to: datetime,
) -> dict[str, Any]:
    result = await db_session.execute(
        text(
            """
            SELECT
                COUNT(DISTINCT session_id)::bigint AS observed_sessions,
                COUNT(*)::bigint AS observed_visits
            FROM orch_session_metrics
            WHERE flow_uuid = CAST(:flow_uuid AS uuid)
              AND revision_id = CAST(:revision_id AS uuid)
              AND metric_type = 'card'
              AND created_at >= :period_from
              AND created_at < :period_to
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "revision_id": revision_id,
            "period_from": period_from,
            "period_to": period_to,
        },
    )
    row = result.mappings().first()
    return dict(row) if row is not None else {"observed_sessions": 0, "observed_visits": 0}


async def fetch_journey_sessions(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    person_uuid: str | None,
    period_from: datetime,
    period_to: datetime,
    limit: int,
    cursor_created_at: datetime | None,
    cursor_id: int | None,
) -> list[dict[str, Any]]:
    query = f"""
        SELECT
            id,
            uuid::text AS session_uuid,
            state,
            entity,
            entity_type,
            entity_address,
            {_SESSION_REVISION_SQL} AS revision_id,
            started_at,
            ended_at,
            created_at,
            updated_at,
            last_card_uuid::text AS last_card_uuid,
            next_card_uuid::text AS next_card_uuid
        FROM orch_sessions
        WHERE flow_uuid = CAST(:flow_uuid AS uuid)
          AND created_at >= :period_from
          AND created_at < :period_to
    """
    params: dict[str, Any] = {
        "flow_uuid": flow_uuid,
        "period_from": period_from,
        "period_to": period_to,
        "limit": limit,
    }
    if person_uuid:
        query += """
          AND (
                EXISTS (
                    SELECT 1
                    FROM contact_list_members clm
                    WHERE clm.person_uuid = CAST(:person_uuid AS uuid)
                      AND clm.id::text = COALESCE(
                          orch_sessions.runtime_variables #>> '{session_identity,contact_list_member_id}',
                          orch_sessions.runtime_variables #>> '{input_payload,contact_list_member_id}',
                          orch_sessions.runtime_variables #>> '{last_payload,contact_list_member_id}'
                      )
                )
                OR (
                    orch_sessions.entity_type = 'person'
                    AND orch_sessions.entity = (
                        SELECT p.identifier
                        FROM persons p
                        WHERE p.uuid = CAST(:person_uuid AS uuid)
                        LIMIT 1
                    )
                )
          )
        """
        params["person_uuid"] = person_uuid
    if cursor_created_at is not None and cursor_id is not None:
        query += " AND (created_at, id) < (:cursor_created_at, :cursor_id)"
        params["cursor_created_at"] = cursor_created_at
        params["cursor_id"] = cursor_id
    query += " ORDER BY created_at DESC, id DESC LIMIT :limit"
    result = await db_session.execute(text(query), params)
    return [dict(row) for row in result.mappings().all()]


async def fetch_journey_session(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    session_uuid: str,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            f"""
            SELECT
                id,
                uuid::text AS session_uuid,
                state,
                entity,
                entity_type,
                entity_address,
                {_SESSION_REVISION_SQL} AS revision_id,
                started_at,
                ended_at,
                created_at,
                updated_at,
                last_card_uuid::text AS last_card_uuid,
                next_card_uuid::text AS next_card_uuid
            FROM orch_sessions
            WHERE flow_uuid = CAST(:flow_uuid AS uuid)
              AND uuid = CAST(:session_uuid AS uuid)
            LIMIT 1
            """
        ),
        {"flow_uuid": flow_uuid, "session_uuid": session_uuid},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def fetch_journey_trace_metrics(
    db_session: AsyncSession,
    *,
    session_id: int,
    limit: int,
) -> list[dict[str, Any]]:
    result = await db_session.execute(
        text(
            """
            SELECT
                id,
                revision_id::text AS revision_id,
                step_index,
                card_cursor AS node_id,
                component_kind,
                status,
                stopped_reason,
                latency_ms,
                started_at,
                finished_at,
                NULLIF(details ->> 'branch_label', '') AS branch,
                NULLIF(details ->> 'next_card_uuid', '') AS next_node_id
            FROM orch_session_metrics
            WHERE session_id = :session_id
              AND metric_type = 'card'
            ORDER BY created_at DESC, id DESC
            LIMIT :limit
            """
        ),
        {"session_id": session_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]
