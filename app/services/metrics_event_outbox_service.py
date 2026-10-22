from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings


_METRICS_CHANNEL_ALIASES = {
    "voice": "VOZ",
    "voz": "VOZ",
    "telefone": "VOZ",
    "phone": "VOZ",
    "pbx": "VOZ",
    "bot": "PLAYGROUND",
    "playground": "PLAYGROUND",
    "custom": "WEBHOOK",
}
_METRICS_ALLOWED_CHANNELS = {
    "VOZ",
    "WHATSAPP",
    "EMAIL",
    "SMS",
    "RCS",
    "WEBCHAT",
    "TELEGRAM",
    "PLAYGROUND",
    "WEBHOOK",
}


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


def _metrics_event_timestamp(value: Any) -> tuple[str, int]:
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if parsed is None or parsed.tzinfo is None:
        parsed = datetime.now(timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return _utc_iso(parsed), int(parsed.timestamp() * 1000)


def _normalize_metrics_channel(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in (
            "channel_type",
            "channel.type",
            "type",
            "channel",
            "provider",
            "mode",
        ):
            normalized = _normalize_metrics_channel(value.get(key))
            if normalized:
                return normalized
        return None
    channel = str(value).strip()
    if not channel:
        return None
    pattern = re.compile(
        r'''["']?(?:channel(?:[_\.\s]?type)?|type)["']?\s*[:=]\s*'''
        r'''["']?([A-Za-z_]+)["']?''',
        re.IGNORECASE,
    )
    extracted = None
    for match in pattern.finditer(channel):
        candidate = match.group(1).strip()
        if candidate.lower() not in {"none", "null", "undefined"}:
            extracted = candidate
            break
    if extracted:
        channel = extracted
    elif channel.startswith("{") and channel.endswith("}"):
        return None
    normalized = _METRICS_CHANNEL_ALIASES.get(channel.lower(), channel.upper())
    return normalized if normalized in _METRICS_ALLOWED_CHANNELS else None


def _canonicalize_custom_metric_event(
    event: dict[str, Any],
    *,
    settings: Settings,
) -> dict[str, Any]:
    """Mirror Target Core's canonical event envelope and final channel guard."""

    canonical = dict(event)
    context = canonical.get("context")
    context = dict(context) if isinstance(context, dict) else {}
    payload = canonical.get("payload")
    payload = dict(payload) if isinstance(payload, dict) else {}

    workspace_id = canonical.get("workspace_id") or context.get("workspace_id")
    if workspace_id is not None and str(workspace_id).strip():
        workspace_text = str(workspace_id).strip()
        canonical["workspace_id"] = workspace_text
        context["workspace_id"] = workspace_text
    context["mode"] = "orchestration"
    context["flow_type"] = "orchestration"

    normalized_channel = _normalize_metrics_channel(context.get("channel"))
    if not normalized_channel:
        for payload_key in ("channel", "entry_point", "from_channel", "to_channel"):
            normalized_channel = _normalize_metrics_channel(payload.get(payload_key))
            if normalized_channel:
                break
    if not normalized_channel:
        normalized_channel = _normalize_metrics_channel(canonical.get("channel"))
    context["channel"] = normalized_channel or "WEBHOOK"

    canonical["context"] = context
    canonical["payload"] = payload
    if not isinstance(canonical.get("event_id"), str) or not canonical[
        "event_id"
    ].strip():
        canonical["event_id"] = str(uuid4())

    timestamp, timestamp_ms = _metrics_event_timestamp(canonical.get("timestamp"))
    canonical["timestamp"] = timestamp
    # `emit_metrics` no Target Core is born without source; seu canonicalizador
    # sempre produz este valor. A origem `orch` pertence ao stream generico e
    # nao deve vazar para o recurso dedicado deste card.
    canonical["source"] = "flow-executor"

    billing = canonical.get("billing")
    billing = dict(billing) if isinstance(billing, dict) else {}
    billing.setdefault("tokens", 0)
    billing.setdefault("units", [])
    canonical["billing"] = billing

    interaction_id = context.get("interaction_id")
    trace = canonical.get("trace")
    trace = dict(trace) if isinstance(trace, dict) else {}
    if interaction_id and not trace.get("correlation_id"):
        trace["correlation_id"] = f"corr_{interaction_id}"
    if not trace.get("idempotency_key"):
        trace["idempotency_key"] = (
            f"{canonical.get('workspace_id')}:{interaction_id}:"
            f"{canonical.get('event_type')}:{timestamp_ms}"
        )
    canonical["trace"] = trace

    labels = canonical.get("labels")
    labels = dict(labels) if isinstance(labels, dict) else {}
    labels.setdefault("environment", settings.metrics_environment or "production")
    if settings.metrics_tier:
        labels.setdefault("tier", settings.metrics_tier)
    canonical["labels"] = labels
    return canonical


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


def publish_custom_metric_event(
    *,
    event: dict[str, Any],
    settings: Settings,
) -> MetricsPublishResult:
    """Deliver `emit_metrics` through the same contract as Target Core.

    The regular orchestration stream is a batch API.  Custom metrics are a
    distinct Metrics resource: Target Core posts the event itself, one at a
    time, under the `/api` base path.  Keep this adapter separate so changing
    that card contract cannot alter the established generic-event publisher.
    """
    base_url = str(settings.orch_metrics_api_base_url or "").rstrip("/")
    api_key = str(settings.orch_metrics_api_key or "").strip()
    if not base_url or not api_key:
        raise RuntimeError("metrics_api_not_configured")
    metrics_base_url = base_url if base_url.endswith("/api") else f"{base_url}/api"
    # Target Core completes the canonical envelope in its dispatcher and then
    # sanitizes the channel immediately before the singular POST.  Reproduce
    # both steps here; `OTHER`, for example, is not accepted by Metrics and is
    # intentionally mapped to the same WEBHOOK fallback used by Target Core.
    request_body = _canonicalize_custom_metric_event(event, settings=settings)
    request = Request(
        f"{metrics_base_url}/v1/events/ingest",
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": api_key,
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
