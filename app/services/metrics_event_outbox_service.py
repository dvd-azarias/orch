from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class MetricsEventClaim:
    id: int
    event_id: str
    idempotency_key: str
    event_type: str
    envelope: dict[str, Any]
    attempts: int


@dataclass(frozen=True)
class MetricsPublishResult:
    status_code: int | None
    response_text: str
    retryable: bool
    success: bool


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def metrics_events_enabled_for_workspace(
    workspace_uuid: str,
    *,
    settings: Settings | None = None,
) -> bool:
    effective_settings = settings or get_settings()
    if not effective_settings.orch_metrics_events_enabled:
        return False
    try:
        safe_workspace_uuid = str(UUID(str(workspace_uuid)))
    except (TypeError, ValueError, AttributeError):
        return False
    if bool(
        getattr(
            effective_settings,
            "orch_metrics_events_allow_all_workspaces",
            False,
        )
    ):
        return True
    allowed = {
        str(UUID(value))
        for value in effective_settings.orch_metrics_events_workspace_allowlist
    }
    return safe_workspace_uuid in allowed


def build_metrics_event(
    *,
    workspace_uuid: str,
    event_type: str,
    context: dict[str, Any],
    payload: dict[str, Any],
    occurred_at: datetime | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    safe_workspace_uuid = str(UUID(str(workspace_uuid)))
    safe_event_type = str(event_type or "").strip()
    if not safe_event_type:
        raise ValueError("metrics_event_type_required")
    safe_context = dict(context or {})
    safe_context["flow_type"] = "orchestration"
    return {
        "event_id": str(UUID(str(event_id))) if event_id else str(uuid4()),
        "event_type": safe_event_type,
        "timestamp": _utc_iso(occurred_at),
        "workspace_id": safe_workspace_uuid,
        "source": "orch",
        "context": safe_context,
        "payload": dict(payload or {}),
    }


async def enqueue_metrics_event(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    idempotency_key: str,
    event_type: str,
    context: dict[str, Any],
    payload: dict[str, Any],
    occurred_at: datetime | None = None,
    flow_uuid: str | None = None,
    session_uuid: str | None = None,
    settings: Settings | None = None,
) -> str | None:
    effective_settings = settings or get_settings()
    if not metrics_events_enabled_for_workspace(
        workspace_uuid,
        settings=effective_settings,
    ):
        return None
    safe_idempotency_key = str(idempotency_key or "").strip()
    if not safe_idempotency_key:
        raise ValueError("metrics_idempotency_key_required")
    event = build_metrics_event(
        workspace_uuid=workspace_uuid,
        event_type=event_type,
        context=context,
        payload=payload,
        occurred_at=occurred_at,
    )
    result = await db_session.execute(
        text(
            """
            INSERT INTO orch_metrics_event_outbox (
                event_id,
                idempotency_key,
                event_type,
                flow_uuid,
                session_uuid,
                envelope
            )
            VALUES (
                CAST(:event_id AS uuid),
                :idempotency_key,
                :event_type,
                CAST(:flow_uuid AS uuid),
                CAST(:session_uuid AS uuid),
                CAST(:envelope AS jsonb)
            )
            ON CONFLICT (idempotency_key) DO UPDATE
            SET idempotency_key = orch_metrics_event_outbox.idempotency_key
            RETURNING event_id::text
            """
        ),
        {
            "event_id": event["event_id"],
            "idempotency_key": safe_idempotency_key,
            "event_type": event_type,
            "flow_uuid": flow_uuid,
            "session_uuid": session_uuid,
            "envelope": json.dumps(event, ensure_ascii=False, default=str),
        },
    )
    return str(result.scalar_one())


async def claim_metrics_events(
    db_session: AsyncSession,
    *,
    claim_token: str,
    batch_size: int,
    lease_seconds: int,
    max_attempts: int,
) -> list[MetricsEventClaim]:
    safe_claim_token = str(UUID(str(claim_token)))
    result = await db_session.execute(
        text(
            """
            WITH candidates AS (
                SELECT id
                FROM orch_metrics_event_outbox
                WHERE attempts < :max_attempts
                  AND (
                      (status = 'pending' AND next_attempt_at <= NOW())
                      OR (
                          status = 'publishing'
                          AND claimed_at < NOW() - make_interval(secs => :lease_seconds)
                      )
                  )
                ORDER BY next_attempt_at, created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT :batch_size
            )
            UPDATE orch_metrics_event_outbox AS outbox
            SET status = 'publishing',
                attempts = attempts + 1,
                claimed_at = NOW(),
                claim_token = CAST(:claim_token AS uuid),
                updated_at = NOW()
            FROM candidates
            WHERE outbox.id = candidates.id
            RETURNING
                outbox.id,
                outbox.event_id::text AS event_id,
                outbox.idempotency_key,
                outbox.event_type,
                outbox.envelope,
                outbox.attempts
            """
        ),
        {
            "claim_token": safe_claim_token,
            "batch_size": max(1, min(int(batch_size), 100)),
            "lease_seconds": max(30, int(lease_seconds)),
            "max_attempts": max(1, int(max_attempts)),
        },
    )
    return [
        MetricsEventClaim(
            id=int(row["id"]),
            event_id=str(row["event_id"]),
            idempotency_key=str(row["idempotency_key"]),
            event_type=str(row["event_type"]),
            envelope=dict(row["envelope"]),
            attempts=int(row["attempts"]),
        )
        for row in result.mappings().all()
    ]


async def mark_metrics_events_published(
    db_session: AsyncSession,
    *,
    claim_token: str,
    item_ids: Iterable[int],
    http_status: int,
) -> int:
    ids = [int(item_id) for item_id in item_ids]
    if not ids:
        return 0
    result = await db_session.execute(
        text(
            """
            UPDATE orch_metrics_event_outbox
            SET status = 'published',
                published_at = NOW(),
                claimed_at = NULL,
                claim_token = NULL,
                last_http_status = :http_status,
                last_error = NULL,
                updated_at = NOW()
            WHERE id = ANY(CAST(:item_ids AS bigint[]))
              AND status = 'publishing'
              AND claim_token = CAST(:claim_token AS uuid)
            RETURNING id
            """
        ),
        {
            "claim_token": str(UUID(str(claim_token))),
            "item_ids": ids,
            "http_status": int(http_status),
        },
    )
    return len(result.fetchall())


async def mark_metrics_events_failed(
    db_session: AsyncSession,
    *,
    claim_token: str,
    item_ids: Iterable[int],
    retryable: bool,
    retry_delay_seconds: int,
    max_attempts: int,
    http_status: int | None,
    error: str,
) -> tuple[int, int]:
    ids = [int(item_id) for item_id in item_ids]
    if not ids:
        return 0, 0
    result = await db_session.execute(
        text(
            """
            UPDATE orch_metrics_event_outbox
            SET status = CASE
                    WHEN CAST(:retryable AS boolean) AND attempts < :max_attempts
                    THEN 'pending'
                    ELSE 'dead'
                END,
                next_attempt_at = CASE
                    WHEN CAST(:retryable AS boolean) AND attempts < :max_attempts
                    THEN NOW() + make_interval(secs => :retry_delay_seconds)
                    ELSE next_attempt_at
                END,
                claimed_at = NULL,
                claim_token = NULL,
                last_http_status = :http_status,
                last_error = :error,
                updated_at = NOW()
            WHERE id = ANY(CAST(:item_ids AS bigint[]))
              AND status = 'publishing'
              AND claim_token = CAST(:claim_token AS uuid)
            RETURNING status
            """
        ),
        {
            "claim_token": str(UUID(str(claim_token))),
            "item_ids": ids,
            "retryable": bool(retryable),
            "retry_delay_seconds": max(1, int(retry_delay_seconds)),
            "max_attempts": max(1, int(max_attempts)),
            "http_status": int(http_status) if http_status is not None else None,
            "error": str(error or "metrics_publish_failed")[:1000],
        },
    )
    statuses = [str(row[0]) for row in result.fetchall()]
    return statuses.count("pending"), statuses.count("dead")


def metrics_retry_delay_seconds(
    *,
    attempt: int,
    initial_seconds: int,
    maximum_seconds: int,
) -> int:
    exponent = max(0, min(int(attempt) - 1, 20))
    return min(maximum_seconds, initial_seconds * (2**exponent))


def publish_metrics_events_batch(
    *,
    events: list[dict[str, Any]],
    claim_token: str,
    settings: Settings,
) -> MetricsPublishResult:
    if not events:
        raise ValueError("metrics_events_batch_empty")
    if len(events) > 100:
        raise ValueError("metrics_events_batch_too_large")
    base_url = str(settings.orch_metrics_api_base_url or "").rstrip("/")
    api_key = str(settings.orch_metrics_api_key or "").strip()
    if not base_url or not api_key:
        raise RuntimeError("metrics_api_not_configured")
    request = Request(
        f"{base_url}/v1/events/ingest",
        data=json.dumps({"events": events}, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "X-Idempotency-Key": str(UUID(str(claim_token))),
        },
        method="POST",
    )
    try:
        with urlopen(  # noqa: S310 - endpoint is validated in Settings
            request,
            timeout=settings.orch_metrics_events_http_timeout_seconds,
        ) as response:
            status_code = int(response.status)
            response_text = response.read(1000).decode("utf-8", errors="replace")
    except HTTPError as exc:
        status_code = int(exc.code)
        response_text = exc.read(1000).decode("utf-8", errors="replace")
        return MetricsPublishResult(
            status_code=status_code,
            response_text=response_text,
            retryable=status_code == 429 or status_code >= 500,
            success=False,
        )
    except (URLError, TimeoutError, OSError) as exc:
        return MetricsPublishResult(
            status_code=None,
            response_text=type(exc).__name__,
            retryable=True,
            success=False,
        )
    return MetricsPublishResult(
        status_code=status_code,
        response_text=response_text,
        retryable=False,
        success=200 <= status_code < 300,
    )
