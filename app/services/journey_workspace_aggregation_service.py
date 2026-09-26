from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


MAX_RETENTION_DAYS = 30
STAGES = (
    ("entrada", 1, "Entrada"),
    ("identificacao", 2, "Identificação"),
    ("qualificacao", 3, "Qualificação"),
    ("abordagem", 4, "Abordagem"),
    ("proposta", 5, "Proposta"),
    ("decisao", 6, "Decisão"),
    ("desfecho", 7, "Desfecho"),
)
CHANNELS = ("voice", "whatsapp", "sms", "rcs", "email")


def _aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} precisa conter timezone")
    return value


def _iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _uuid_or_none(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    return str(UUID(str(value)))


def _period_bounds(
    *,
    generated_at: datetime,
    retention_days: int,
    period_from: datetime | None,
    period_to: datetime | None,
) -> tuple[datetime, datetime]:
    upper = _aware(period_to or generated_at, field="period_to")
    lower = _aware(
        period_from or (generated_at - timedelta(days=retention_days)),
        field="period_from",
    )
    if lower >= upper:
        raise ValueError("invalid_period")
    if upper - lower > timedelta(days=MAX_RETENTION_DAYS, seconds=1):
        raise ValueError("period_exceeds_retention")
    return lower, upper


async def _retention_and_coverage(
    db_session: AsyncSession,
    *,
    generated_at: datetime,
) -> tuple[int, datetime | None, datetime]:
    row = (
        await db_session.execute(
            text(
                """
                SELECT
                    settings.retention_days,
                    MIN(coverage.coverage_started_at) AS coverage_started_at
                FROM orch_journey_settings AS settings
                LEFT JOIN orch_journey_flow_coverage AS coverage
                  ON coverage.status = 'active'
                WHERE settings.singleton_id = 1
                GROUP BY settings.retention_days
                """
            )
        )
    ).mappings().one()
    retention_days = min(MAX_RETENTION_DAYS, max(1, int(row["retention_days"])))
    coverage_started_at = row["coverage_started_at"]
    retention_floor = generated_at - timedelta(days=retention_days)
    oldest_available_at = max(
        value
        for value in (retention_floor, coverage_started_at)
        if value is not None
    )
    return retention_days, coverage_started_at, oldest_available_at


async def build_journey_workspace_view(
    db_session: AsyncSession,
    *,
    period_from: datetime,
    period_to: datetime,
    flow_uuid: str | None = None,
    revision_uuid: str | None = None,
    channels: list[str] | None = None,
    timezone_name: str = "America/Sao_Paulo",
) -> dict[str, Any]:
    safe_flow_uuid = _uuid_or_none(flow_uuid)
    safe_revision_uuid = _uuid_or_none(revision_uuid)
    safe_channels = sorted(
        {
            str(channel).strip().lower()
            for channel in (channels or CHANNELS)
            if str(channel).strip().lower() in CHANNELS
        }
    )
    if channels is not None and not safe_channels:
        raise ValueError("invalid_channels")
    params = {
        "period_from": period_from,
        "period_to": period_to,
        "flow_uuid": safe_flow_uuid,
        "revision_uuid": safe_revision_uuid,
        "channels": safe_channels,
        "timezone_name": timezone_name,
    }
    session_filter = """
        session.started_at >= CAST(:period_from AS timestamptz)
        AND session.started_at < CAST(:period_to AS timestamptz)
        AND (
            CAST(:flow_uuid AS uuid) IS NULL
            OR session.flow_uuid = CAST(:flow_uuid AS uuid)
        )
        AND (
            CAST(:revision_uuid AS uuid) IS NULL
            OR session.flow_revision_id = CAST(:revision_uuid AS uuid)
        )
    """

    summary = dict(
        (
            await db_session.execute(
                text(
                    f"""
                    SELECT
                        COUNT(*) AS sessions_started,
                        COUNT(*) FILTER (
                            WHERE lifecycle_status IN ('in_progress', 'waiting')
                        ) AS sessions_in_progress,
                        COUNT(*) FILTER (
                            WHERE lifecycle_status = 'completed'
                        ) AS sessions_completed,
                        COUNT(*) FILTER (
                            WHERE lifecycle_status = 'abandoned'
                        ) AS sessions_abandoned,
                        COUNT(*) FILTER (
                            WHERE lifecycle_status = 'failed'
                        ) AS sessions_failed,
                        COUNT(*) FILTER (
                            WHERE NOT EXISTS (
                                SELECT 1
                                FROM orch_journey_channel_actions AS action
                                WHERE action.journey_session_id = session.id
                            )
                        ) AS sessions_without_actions
                    FROM orch_journey_sessions AS session
                    WHERE {session_filter}
                    """
                ),
                params,
            )
        ).mappings().one()
    )
    summary = {key: int(value or 0) for key, value in summary.items()}
    summary["conversions"] = 0
    summary["conversion_denominator"] = summary["sessions_started"]

    active_rows = (
        await db_session.execute(
            text(
                f"""
                SELECT current_stage AS stage_id, COUNT(*) AS sessions
                FROM orch_journey_sessions AS session
                WHERE {session_filter}
                  AND lifecycle_status IN ('in_progress', 'waiting')
                  AND current_stage IS NOT NULL
                GROUP BY current_stage, current_stage_ordinal
                ORDER BY current_stage_ordinal
                """
            ),
            params,
        )
    ).mappings().all()
    active_progress = [
        {"stage_id": row["stage_id"], "sessions": int(row["sessions"])}
        for row in active_rows
    ]

    health_row = (
        await db_session.execute(
            text(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE lifecycle_status = 'failed') AS error,
                    COUNT(*) FILTER (
                        WHERE lifecycle_status = 'waiting'
                          AND last_progress_at < NOW() - INTERVAL '30 minutes'
                    ) AS awaiting_intervention,
                    COUNT(*) FILTER (
                        WHERE lifecycle_status = 'in_progress'
                          AND last_progress_at < NOW() - INTERVAL '15 minutes'
                    ) AS stalled,
                    COUNT(*) FILTER (
                        WHERE lifecycle_status = 'waiting'
                          AND last_progress_at >= NOW() - INTERVAL '30 minutes'
                    ) AS delayed,
                    COUNT(*) FILTER (
                        WHERE lifecycle_status NOT IN ('failed', 'waiting')
                          AND NOT (
                              lifecycle_status = 'in_progress'
                              AND last_progress_at < NOW() - INTERVAL '15 minutes'
                          )
                    ) AS normal
                FROM orch_journey_sessions AS session
                WHERE {session_filter}
                """
            ),
            params,
        )
    ).mappings().one()
    health = {"grain": "sessions"}
    health.update({key: int(value or 0) for key, value in health_row.items()})

    funnel_rows = (
        await db_session.execute(
            text(
                f"""
                SELECT
                    visit.stage_id,
                    visit.stage_ordinal,
                    COUNT(DISTINCT visit.journey_session_id) AS reached_sessions,
                    COUNT(*) AS visits
                FROM orch_journey_stage_visits AS visit
                JOIN orch_journey_sessions AS session
                  ON session.id = visit.journey_session_id
                WHERE {session_filter}
                GROUP BY visit.stage_id, visit.stage_ordinal
                """
            ),
            params,
        )
    ).mappings().all()
    funnel_by_stage = {str(row["stage_id"]): row for row in funnel_rows}
    funnel = [
        {
            "stage_id": stage_id,
            "ordinal": ordinal,
            "reached_sessions": int(
                (funnel_by_stage.get(stage_id) or {}).get("reached_sessions") or 0
            ),
            "visits": int((funnel_by_stage.get(stage_id) or {}).get("visits") or 0),
        }
        for stage_id, ordinal, _label in STAGES
    ]

    dropoff_rows = (
        await db_session.execute(
            text(
                f"""
                SELECT current_stage AS stage_id, COUNT(*) AS sessions
                FROM orch_journey_sessions AS session
                WHERE {session_filter}
                  AND lifecycle_status IN ('abandoned', 'failed')
                  AND current_stage IS NOT NULL
                GROUP BY current_stage, current_stage_ordinal
                ORDER BY current_stage_ordinal
                """
            ),
            params,
        )
    ).mappings().all()
    stage_dropoffs = [
        {"stage_id": row["stage_id"], "sessions": int(row["sessions"])}
        for row in dropoff_rows
    ]

    channel_action_rows = (
        await db_session.execute(
            text(
                f"""
                SELECT action.channel, COUNT(*) AS actions
                FROM orch_journey_channel_actions AS action
                JOIN orch_journey_sessions AS session
                  ON session.id = action.journey_session_id
                WHERE {session_filter}
                  AND action.channel = ANY(CAST(:channels AS text[]))
                GROUP BY action.channel
                """
            ),
            params,
        )
    ).mappings().all()
    event_outcome_rows = (
        await db_session.execute(
            text(
                f"""
                SELECT
                    action.channel,
                    event.normalized_status AS outcome,
                    COUNT(DISTINCT action.action_id) AS actions
                FROM orch_journey_channel_action_events AS event
                JOIN orch_journey_channel_actions AS action
                  ON action.action_id = event.action_id
                JOIN orch_journey_sessions AS session
                  ON session.id = action.journey_session_id
                WHERE {session_filter}
                  AND action.channel = ANY(CAST(:channels AS text[]))
                GROUP BY action.channel, event.normalized_status
                """
            ),
            params,
        )
    ).mappings().all()
    outcomes_by_channel: dict[str, dict[str, int]] = {}
    for row in event_outcome_rows:
        outcomes_by_channel.setdefault(str(row["channel"]), {})[
            str(row["outcome"])
        ] = int(row["actions"])
    actions_by_channel = {
        str(row["channel"]): int(row["actions"]) for row in channel_action_rows
    }
    channel_view = [
        {
            "channel": channel,
            "grain": "actions",
            "actions": actions_by_channel[channel],
            "outcomes": outcomes_by_channel.get(channel, {}),
        }
        for channel in CHANNELS
        if channel in safe_channels and actions_by_channel.get(channel, 0) > 0
    ]

    session_outcome_rows = (
        await db_session.execute(
            text(
                f"""
                SELECT
                    COALESCE(terminal_class, lifecycle_status) AS outcome,
                    COUNT(*) AS total
                FROM orch_journey_sessions AS session
                WHERE {session_filter}
                  AND lifecycle_status IN ('completed', 'abandoned', 'failed')
                GROUP BY COALESCE(terminal_class, lifecycle_status)
                ORDER BY outcome
                """
            ),
            params,
        )
    ).mappings().all()
    outcomes = [
        {"grain": "sessions", "outcome": row["outcome"], "count": int(row["total"])}
        for row in session_outcome_rows
    ]

    duration_row = (
        await db_session.execute(
            text(
                f"""
                SELECT
                    AVG(EXTRACT(EPOCH FROM (ended_at - started_at))) AS average_seconds,
                    COUNT(*) FILTER (
                        WHERE ended_at IS NOT NULL
                          AND ended_at - started_at <= INTERVAL '60 seconds'
                    ) AS lte_60,
                    COUNT(*) FILTER (
                        WHERE ended_at IS NOT NULL
                          AND ended_at - started_at <= INTERVAL '300 seconds'
                    ) AS lte_300,
                    COUNT(*) FILTER (
                        WHERE ended_at IS NOT NULL
                          AND ended_at - started_at <= INTERVAL '900 seconds'
                    ) AS lte_900,
                    COUNT(*) FILTER (
                        WHERE ended_at IS NOT NULL
                          AND ended_at - started_at > INTERVAL '900 seconds'
                    ) AS gt_900
                FROM orch_journey_sessions AS session
                WHERE {session_filter}
                  AND ended_at IS NOT NULL
                """
            ),
            params,
        )
    ).mappings().one()
    duration = {
        "grain": "sessions",
        "average_seconds": round(float(duration_row["average_seconds"] or 0), 3),
        "histogram": [
            {"lte_seconds": 60, "count": int(duration_row["lte_60"] or 0)},
            {"lte_seconds": 300, "count": int(duration_row["lte_300"] or 0)},
            {"lte_seconds": 900, "count": int(duration_row["lte_900"] or 0)},
            {"lte_seconds": None, "count": int(duration_row["gt_900"] or 0)},
        ],
    }

    session_series_rows = (
        await db_session.execute(
            text(
                f"""
                SELECT
                    (started_at AT TIME ZONE :timezone_name)::date AS bucket_date,
                    COUNT(*) AS sessions_started,
                    COUNT(*) FILTER (
                        WHERE lifecycle_status = 'completed'
                    ) AS sessions_completed
                FROM orch_journey_sessions AS session
                WHERE {session_filter}
                GROUP BY bucket_date
                ORDER BY bucket_date
                """
            ),
            params,
        )
    ).mappings().all()
    action_series_rows = (
        await db_session.execute(
            text(
                f"""
                SELECT
                    (action.requested_at AT TIME ZONE :timezone_name)::date AS bucket_date,
                    COUNT(*) AS actions
                FROM orch_journey_channel_actions AS action
                JOIN orch_journey_sessions AS session
                  ON session.id = action.journey_session_id
                WHERE {session_filter}
                  AND action.channel = ANY(CAST(:channels AS text[]))
                GROUP BY bucket_date
                ORDER BY bucket_date
                """
            ),
            params,
        )
    ).mappings().all()
    series_by_date: dict[date, dict[str, Any]] = {}
    for row in session_series_rows:
        series_by_date[row["bucket_date"]] = {
            "date": _iso(row["bucket_date"]),
            "timezone": timezone_name,
            "sessions_started": int(row["sessions_started"]),
            "sessions_completed": int(row["sessions_completed"]),
            "actions": 0,
        }
    for row in action_series_rows:
        bucket = series_by_date.setdefault(
            row["bucket_date"],
            {
                "date": _iso(row["bucket_date"]),
                "timezone": timezone_name,
                "sessions_started": 0,
                "sessions_completed": 0,
                "actions": 0,
            },
        )
        bucket["actions"] = int(row["actions"])
    time_series = [series_by_date[key] for key in sorted(series_by_date)]

    alert_rows = (
        await db_session.execute(
            text(
                """
                SELECT code, COUNT(*) AS total
                FROM orch_sessions_alarms
                WHERE created_at >= CAST(:period_from AS timestamptz)
                  AND created_at < CAST(:period_to AS timestamptz)
                  AND code LIKE 'journey_metrics_%'
                  AND (CAST(:flow_uuid AS uuid) IS NULL OR flow_uuid = CAST(:flow_uuid AS uuid))
                GROUP BY code
                ORDER BY code
                """
            ),
            params,
        )
    ).mappings().all()
    alerts = [{"code": row["code"], "count": int(row["total"])} for row in alert_rows]

    return {
        "summary": summary,
        "active_progress": active_progress,
        "health": health,
        "funnel": funnel,
        "stage_dropoffs": stage_dropoffs,
        "channels": channel_view,
        "outcomes": outcomes,
        "duration": duration,
        "time_series": time_series,
        "alerts": alerts,
    }


async def build_journey_workspace_snapshot_payload(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    workspace_name: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    safe_generated_at = _aware(generated_at or datetime.now(UTC), field="generated_at")
    retention_days, coverage_started_at, oldest_available_at = (
        await _retention_and_coverage(db_session, generated_at=safe_generated_at)
    )
    period_from, period_to = _period_bounds(
        generated_at=safe_generated_at,
        retention_days=retention_days,
        period_from=oldest_available_at,
        period_to=safe_generated_at + timedelta(microseconds=1),
    )
    workspace_view = await build_journey_workspace_view(
        db_session,
        period_from=period_from,
        period_to=period_to,
    )

    flow_rows = (
        await db_session.execute(
            text(
                """
                SELECT
                    coverage.flow_uuid::text AS uuid,
                    COALESCE(flow.display_name, flow.slug, coverage.flow_uuid::text) AS name,
                    coverage.coverage_started_at,
                    coverage.last_fact_at
                FROM orch_journey_flow_coverage AS coverage
                LEFT JOIN flow_v2 AS flow ON flow.id = coverage.flow_uuid
                WHERE coverage.status = 'active'
                ORDER BY name, uuid
                """
            )
        )
    ).mappings().all()
    revision_rows = (
        await db_session.execute(
            text(
                """
                SELECT DISTINCT
                    session.flow_revision_id::text AS uuid,
                    session.flow_uuid::text AS flow_uuid,
                    revision.version
                FROM orch_journey_sessions AS session
                LEFT JOIN flow_v2_revision AS revision
                  ON revision.id = session.flow_revision_id
                WHERE session.started_at >= CAST(:period_from AS timestamptz)
                  AND session.started_at < CAST(:period_to AS timestamptz)
                  AND session.flow_revision_id IS NOT NULL
                ORDER BY flow_uuid, version NULLS LAST, uuid
                """
            ),
            {"period_from": period_from, "period_to": period_to},
        )
    ).mappings().all()
    flow_summary_rows = (
        await db_session.execute(
            text(
                """
                SELECT
                    coverage.flow_uuid::text AS flow_uuid,
                    COALESCE(flow.display_name, flow.slug, coverage.flow_uuid::text) AS flow_name,
                    coverage.coverage_started_at,
                    coverage.last_fact_at,
                    COUNT(session.id) AS sessions_started,
                    COUNT(session.id) FILTER (
                        WHERE session.lifecycle_status IN ('in_progress', 'waiting')
                    ) AS sessions_in_progress,
                    COUNT(session.id) FILTER (
                        WHERE session.lifecycle_status = 'completed'
                    ) AS sessions_completed,
                    COUNT(session.id) FILTER (
                        WHERE session.lifecycle_status = 'abandoned'
                    ) AS sessions_abandoned,
                    COUNT(session.id) FILTER (
                        WHERE session.lifecycle_status = 'failed'
                    ) AS sessions_failed,
                    COUNT(session.id) FILTER (
                        WHERE session.id IS NOT NULL
                          AND NOT EXISTS (
                              SELECT 1
                              FROM orch_journey_channel_actions AS action
                              WHERE action.journey_session_id = session.id
                          )
                    ) AS sessions_without_actions
                FROM orch_journey_flow_coverage AS coverage
                LEFT JOIN flow_v2 AS flow ON flow.id = coverage.flow_uuid
                LEFT JOIN orch_journey_sessions AS session
                  ON session.flow_uuid = coverage.flow_uuid
                 AND session.started_at >= CAST(:period_from AS timestamptz)
                 AND session.started_at < CAST(:period_to AS timestamptz)
                WHERE coverage.status = 'active'
                GROUP BY
                    coverage.flow_uuid,
                    flow.display_name,
                    flow.slug,
                    coverage.coverage_started_at,
                    coverage.last_fact_at
                ORDER BY flow_name, flow_uuid
                """
            ),
            {"period_from": period_from, "period_to": period_to},
        )
    ).mappings().all()
    flow_summaries: list[dict[str, Any]] = []
    for row in flow_summary_rows:
        flow_summary = {
            "sessions_started": int(row["sessions_started"] or 0),
            "sessions_in_progress": int(row["sessions_in_progress"] or 0),
            "sessions_completed": int(row["sessions_completed"] or 0),
            "sessions_abandoned": int(row["sessions_abandoned"] or 0),
            "sessions_failed": int(row["sessions_failed"] or 0),
            "sessions_without_actions": int(row["sessions_without_actions"] or 0),
            "conversions": 0,
            "conversion_denominator": int(row["sessions_started"] or 0),
        }
        flow_summaries.append(
            {
                "flow": {"uuid": row["flow_uuid"], "name": row["flow_name"]},
                "summary": flow_summary,
                "coverage_complete": row["coverage_started_at"] <= period_from,
                "last_fact_at": _iso(row["last_fact_at"]),
            }
        )

    warnings = [
        {
            "code": alert["code"],
            "count": alert["count"],
            "message": "Há dados de jornada com cobertura ou configuração incompleta.",
        }
        for alert in workspace_view["alerts"]
    ]
    if workspace_view["summary"]["sessions_completed"]:
        warnings.append(
            {
                "code": "journey_metrics_conversion_not_configured",
                "count": workspace_view["summary"]["sessions_completed"],
                "message": "Conversão permanece zerada até existir uma tabulação explícita.",
            }
        )

    return {
        "as_of": _iso(safe_generated_at),
        "workspace": {
            "uuid": str(UUID(str(workspace_uuid))),
            "name": str(workspace_name or workspace_uuid),
        },
        "retention": {
            "retention_days": retention_days,
            "maximum_days": MAX_RETENTION_DAYS,
            "coverage_started_at": _iso(coverage_started_at),
            "oldest_available_at": _iso(oldest_available_at),
        },
        "catalog": {
            "flows": [
                {"uuid": row["uuid"], "name": row["name"]} for row in flow_rows
            ],
            "revisions": [
                {
                    "uuid": row["uuid"],
                    "flow_uuid": row["flow_uuid"],
                    "version": row["version"],
                }
                for row in revision_rows
            ],
            "channels": list(CHANNELS),
            "stages": [
                {"id": stage_id, "ordinal": ordinal, "label": label}
                for stage_id, ordinal, label in STAGES
            ],
        },
        "workspace_view": workspace_view,
        "flow_summaries": flow_summaries,
        "data_quality": {"complete": not warnings, "warnings": warnings},
    }
