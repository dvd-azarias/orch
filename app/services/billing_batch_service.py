from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from kombu import Connection, Exchange, Producer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_BILLING_PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_SECRET_URL_PATTERN = re.compile(r"([a-z][a-z0-9+.-]*://)([^\s/@:]+):([^\s/@]+)@", re.IGNORECASE)


@dataclass(frozen=True)
class AggregatedSnapshot:
    snapshot_id: str
    quantity: int


@dataclass(frozen=True)
class ReprocessRequest:
    request_id: str
    status: str
    created: bool
    billing_period: str


class InvalidBillingPayload(ValueError):
    pass


class UnroutableBillingMessage(RuntimeError):
    pass


class BillingIdempotencyConflict(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_billing_period(value: str) -> tuple[datetime, datetime]:
    normalized = str(value or "").strip()
    if not _BILLING_PERIOD_PATTERN.fullmatch(normalized):
        raise ValueError("billing_period deve usar o formato YYYY-MM.")
    start = datetime.strptime(normalized, "%Y-%m").replace(tzinfo=timezone.utc)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def sanitize_billing_error(exc: BaseException) -> str:
    raw = f"{type(exc).__name__}: {str(exc)}".strip()
    return _SECRET_URL_PATTERN.sub(r"\1***:***@", raw)[:1000]


def calculate_retry_delay_seconds(
    *,
    attempt_count: int,
    initial_seconds: int,
    maximum_seconds: int,
    jitter_seconds: int,
    random_value: float | None = None,
) -> float:
    exponent = max(0, min(int(attempt_count) - 1, 30))
    base = min(float(maximum_seconds), float(initial_seconds) * (2**exponent))
    jitter_ratio = random.random() if random_value is None else max(0.0, min(float(random_value), 1.0))
    return min(float(maximum_seconds), base + (float(jitter_seconds) * jitter_ratio))


def build_batch_snapshot_payload(
    *,
    snapshot_id: str,
    workspace_uuid: str,
    billing_period: date,
    snapshot_at: datetime,
    quantity: int,
    metric_code: str,
    service_code: str,
    application_code: str,
) -> dict[str, Any]:
    snapshot_utc = snapshot_at.astimezone(timezone.utc)
    return {
        "items": [
            {
                "unit": "event",
                "quantity": int(quantity),
                "metric_code": metric_code,
                "service_code": service_code,
            }
        ],
        "currency": "BRL",
        "correction": False,
        "snapshot_at": snapshot_utc.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "snapshot_id": snapshot_id,
        "billing_period": billing_period.strftime("%Y-%m"),
        "workspace_uuid": workspace_uuid,
        "application_code": application_code,
    }


def validate_batch_snapshot_payload(
    payload: dict[str, Any],
    *,
    snapshot_id: str,
    workspace_uuid: str,
    quantity: int,
    billing_period: str | None = None,
) -> None:
    if not isinstance(payload, dict):
        raise InvalidBillingPayload("payload não é um objeto JSON")
    if payload.get("snapshot_id") != snapshot_id:
        raise InvalidBillingPayload("snapshot_id divergente do registro persistido")
    if str(payload.get("workspace_uuid") or "") != workspace_uuid:
        raise InvalidBillingPayload("workspace_uuid divergente do registro persistido")
    payload_period = str(payload.get("billing_period") or "")
    if not _BILLING_PERIOD_PATTERN.fullmatch(payload_period):
        raise InvalidBillingPayload("billing_period inválido")
    if billing_period is not None and payload_period != billing_period:
        raise InvalidBillingPayload("billing_period divergente do registro persistido")
    snapshot_at = str(payload.get("snapshot_at") or "")
    if not snapshot_at.endswith("Z"):
        raise InvalidBillingPayload("snapshot_at deve estar em UTC com sufixo Z")
    try:
        datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidBillingPayload("snapshot_at inválido") from exc
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise InvalidBillingPayload("items deve conter exatamente um item")
    item = items[0]
    if item.get("unit") != "event" or item.get("quantity") != quantity or quantity <= 0:
        raise InvalidBillingPayload("quantity/unit divergente do snapshot persistido")
    for key in ("metric_code", "service_code"):
        if not str(item.get(key) or "").strip():
            raise InvalidBillingPayload(f"{key} ausente")
    if payload.get("currency") != "BRL" or payload.get("correction") is not False:
        raise InvalidBillingPayload("currency/correction inválidos")
    if not str(payload.get("application_code") or "").strip():
        raise InvalidBillingPayload("application_code ausente")


async def record_billing_event(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    session_id: int,
    settings: Settings | None = None,
) -> bool:
    effective_settings = settings or get_settings()
    if not effective_settings.orch_billing_enabled:
        return False
    result = await db_session.execute(
        text(
            """
            INSERT INTO orch_billing_events (
                workspace_uuid,
                source_session_id,
                source_session_uuid,
                billing_period,
                occurred_at,
                metric_code,
                service_code
            )
            SELECT
                CAST(:workspace_uuid AS uuid),
                session_row.id,
                session_row.uuid,
                date_trunc('month', session_row.created_at AT TIME ZONE 'UTC')::date,
                session_row.created_at,
                :metric_code,
                :service_code
            FROM orch_sessions AS session_row
            WHERE session_row.id = :session_id
            ON CONFLICT (workspace_uuid, source_session_uuid, billing_period, metric_code)
            DO NOTHING
            RETURNING id
            """
        ),
        {
            "workspace_uuid": workspace_uuid,
            "session_id": int(session_id),
            "metric_code": effective_settings.billing_metric_code,
            "service_code": effective_settings.billing_service_code,
        },
    )
    return result.scalar_one_or_none() is not None


async def try_record_billing_event(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    session_id: int,
    settings: Settings | None = None,
) -> None:
    try:
        async with db_session.begin_nested():
            await record_billing_event(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=session_id,
                settings=settings,
            )
    except Exception as exc:
        logger.warning(
            "billing event persistence failed",
            extra={
                "event": "orch.billing.event.persistence_failed",
                "workspace_uuid": workspace_uuid,
                "session_id": int(session_id),
                "exception_type": type(exc).__name__,
            },
        )


async def reconcile_billing_events(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    period_start: datetime,
    period_end: datetime,
    settings: Settings,
) -> int:
    result = await db_session.execute(
        text(
            """
            INSERT INTO orch_billing_events (
                workspace_uuid,
                source_session_id,
                source_session_uuid,
                billing_period,
                occurred_at,
                metric_code,
                service_code
            )
            SELECT
                CAST(:workspace_uuid AS uuid),
                session_row.id,
                session_row.uuid,
                date_trunc('month', session_row.created_at AT TIME ZONE 'UTC')::date,
                session_row.created_at,
                :metric_code,
                :service_code
            FROM orch_sessions AS session_row
            WHERE session_row.created_at >= :period_start
              AND session_row.created_at < :period_end
            ON CONFLICT (workspace_uuid, source_session_uuid, billing_period, metric_code)
            DO NOTHING
            RETURNING id
            """
        ),
        {
            "workspace_uuid": workspace_uuid,
            "period_start": period_start,
            "period_end": period_end,
            "metric_code": settings.billing_metric_code,
            "service_code": settings.billing_service_code,
        },
    )
    return len(result.fetchall())


async def aggregate_next_billing_snapshot(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    settings: Settings,
    now: datetime | None = None,
) -> AggregatedSnapshot | None:
    group_result = await db_session.execute(
        text(
            """
            SELECT billing_period, metric_code, service_code
            FROM orch_billing_events
            WHERE status = 'pending'
              AND workspace_uuid = CAST(:workspace_uuid AS uuid)
            ORDER BY occurred_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        ),
        {"workspace_uuid": workspace_uuid},
    )
    group = group_result.mappings().first()
    if group is None:
        return None

    events_result = await db_session.execute(
        text(
            """
            SELECT id
            FROM orch_billing_events
            WHERE status = 'pending'
              AND workspace_uuid = CAST(:workspace_uuid AS uuid)
              AND billing_period = :billing_period
              AND metric_code = :metric_code
              AND service_code = :service_code
            ORDER BY occurred_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT :batch_size
            """
        ),
        {
            "workspace_uuid": workspace_uuid,
            "billing_period": group["billing_period"],
            "metric_code": group["metric_code"],
            "service_code": group["service_code"],
            "batch_size": settings.billing_batch_size,
        },
    )
    event_ids = [int(row["id"]) for row in events_result.mappings().all()]
    if not event_ids:
        return None

    billing_period = group["billing_period"]
    if isinstance(billing_period, datetime):
        billing_period = billing_period.date()
    if not isinstance(billing_period, date):
        raise RuntimeError("billing_period persistido em formato inesperado")
    snapshot_at = (now or utc_now()).astimezone(timezone.utc)
    snapshot_id = f"orch_usage_{billing_period.strftime('%Y%m')}_{workspace_uuid}_{uuid4()}"
    payload = build_batch_snapshot_payload(
        snapshot_id=snapshot_id,
        workspace_uuid=workspace_uuid,
        billing_period=billing_period,
        snapshot_at=snapshot_at,
        quantity=len(event_ids),
        metric_code=str(group["metric_code"]),
        service_code=str(group["service_code"]),
        application_code=settings.billing_application_code,
    )
    await db_session.execute(
        text(
            """
            INSERT INTO orch_billing_snapshots (
                snapshot_id, workspace_uuid, billing_period, snapshot_at, payload, quantity
            )
            VALUES (
                :snapshot_id,
                CAST(:workspace_uuid AS uuid),
                :billing_period,
                :snapshot_at,
                CAST(:payload AS jsonb),
                :quantity
            )
            """
        ),
        {
            "snapshot_id": snapshot_id,
            "workspace_uuid": workspace_uuid,
            "billing_period": billing_period,
            "snapshot_at": snapshot_at,
            "payload": json.dumps(payload, ensure_ascii=False),
            "quantity": len(event_ids),
        },
    )
    update_result = await db_session.execute(
        text(
            """
            UPDATE orch_billing_events
            SET status = 'batched', snapshot_id = :snapshot_id, updated_at = NOW()
            WHERE id = ANY(CAST(:event_ids AS bigint[]))
              AND status = 'pending'
            RETURNING id
            """
        ),
        {"snapshot_id": snapshot_id, "event_ids": event_ids},
    )
    updated_count = len(update_result.fetchall())
    if updated_count != len(event_ids):
        raise RuntimeError("quantidade de eventos alterada durante a agregação")
    return AggregatedSnapshot(snapshot_id=snapshot_id, quantity=len(event_ids))


async def claim_due_billing_snapshots(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    batch_size: int,
    lease_seconds: int,
    claim_token: str,
) -> list[dict[str, Any]]:
    await db_session.execute(
        text(
            """
            UPDATE orch_billing_snapshots
            SET
                status = 'failed',
                next_attempt_at = NOW(),
                claim_token = NULL,
                claimed_at = NULL,
                last_error = 'processing lease expired',
                updated_at = NOW()
            WHERE status = 'processing'
              AND claimed_at < NOW() - make_interval(secs => CAST(:lease_seconds AS int))
            """
        ),
        {"lease_seconds": int(lease_seconds)},
    )
    result = await db_session.execute(
        text(
            """
            WITH candidates AS (
                SELECT snapshot_id
                FROM orch_billing_snapshots
                WHERE workspace_uuid = CAST(:workspace_uuid AS uuid)
                  AND status IN ('pending', 'failed')
                  AND next_attempt_at <= NOW()
                ORDER BY next_attempt_at, created_at, snapshot_id
                FOR UPDATE SKIP LOCKED
                LIMIT :batch_size
            )
            UPDATE orch_billing_snapshots AS snapshot
            SET
                status = 'processing',
                attempt_count = snapshot.attempt_count + 1,
                last_attempt_at = NOW(),
                claimed_at = NOW(),
                claim_token = CAST(:claim_token AS uuid),
                updated_at = NOW()
            FROM candidates
            WHERE snapshot.snapshot_id = candidates.snapshot_id
            RETURNING
                snapshot.snapshot_id,
                snapshot.workspace_uuid::text AS workspace_uuid,
                snapshot.payload,
                snapshot.quantity,
                to_char(snapshot.billing_period, 'YYYY-MM') AS billing_period,
                snapshot.attempt_count,
                snapshot.claim_token::text AS claim_token
            """
        ),
        {
            "workspace_uuid": workspace_uuid,
            "batch_size": max(1, int(batch_size)),
            "claim_token": claim_token,
        },
    )
    return [dict(row) for row in result.mappings().all()]


async def mark_billing_snapshot_sent(
    db_session: AsyncSession,
    *,
    snapshot_id: str,
    claim_token: str,
) -> bool:
    result = await db_session.execute(
        text(
            """
            UPDATE orch_billing_snapshots
            SET
                status = CASE WHEN reprocess_requested THEN 'pending' ELSE 'sent' END,
                next_attempt_at = CASE WHEN reprocess_requested THEN NOW() ELSE next_attempt_at END,
                sent_at = NOW(),
                last_error = NULL,
                claim_token = NULL,
                claimed_at = NULL,
                reprocess_requested = FALSE,
                updated_at = NOW()
            WHERE snapshot_id = :snapshot_id
              AND status = 'processing'
              AND claim_token = CAST(:claim_token AS uuid)
            RETURNING snapshot_id
            """
        ),
        {"snapshot_id": snapshot_id, "claim_token": claim_token},
    )
    if result.scalar_one_or_none() is None:
        return False
    await db_session.execute(
        text(
            """
            UPDATE orch_billing_events
            SET status = 'sent', updated_at = NOW()
            WHERE snapshot_id = :snapshot_id
              AND status = 'batched'
            """
        ),
        {"snapshot_id": snapshot_id},
    )
    return True


async def mark_billing_snapshot_failed(
    db_session: AsyncSession,
    *,
    snapshot_id: str,
    claim_token: str,
    error: str,
    next_attempt_at: datetime,
) -> bool:
    result = await db_session.execute(
        text(
            """
            UPDATE orch_billing_snapshots
            SET
                status = 'failed',
                next_attempt_at = :next_attempt_at,
                last_error = :last_error,
                claim_token = NULL,
                claimed_at = NULL,
                updated_at = NOW()
            WHERE snapshot_id = :snapshot_id
              AND status = 'processing'
              AND claim_token = CAST(:claim_token AS uuid)
            RETURNING snapshot_id
            """
        ),
        {
            "snapshot_id": snapshot_id,
            "claim_token": claim_token,
            "last_error": error[:1000],
            "next_attempt_at": next_attempt_at,
        },
    )
    return result.scalar_one_or_none() is not None


async def mark_billing_snapshot_blocked(
    db_session: AsyncSession,
    *,
    snapshot_id: str,
    claim_token: str,
    error: str,
) -> bool:
    result = await db_session.execute(
        text(
            """
            UPDATE orch_billing_snapshots
            SET
                status = 'blocked',
                last_error = :last_error,
                claim_token = NULL,
                claimed_at = NULL,
                updated_at = NOW()
            WHERE snapshot_id = :snapshot_id
              AND status = 'processing'
              AND claim_token = CAST(:claim_token AS uuid)
            RETURNING snapshot_id
            """
        ),
        {"snapshot_id": snapshot_id, "claim_token": claim_token, "last_error": error[:1000]},
    )
    return result.scalar_one_or_none() is not None


def publish_batch_billing_snapshot(*, snapshot: dict[str, Any], settings: Settings) -> None:
    if not settings.billing_rabbitmq_url:
        raise RuntimeError("BILLING_RABBITMQ_URL não configurada")
    snapshot_id = str(snapshot["snapshot_id"])
    returned: list[tuple[Any, ...]] = []

    def _on_return(*args: Any) -> None:
        returned.append(args)

    exchange = Exchange(settings.billing_exchange, type="topic", durable=True)
    with Connection(
        settings.billing_rabbitmq_url,
        connect_timeout=settings.billing_publish_confirm_timeout_seconds,
        transport_options={"confirm_publish": True},
    ) as connection:
        with connection.channel() as channel:
            Producer(channel, on_return=_on_return).publish(
                snapshot,
                exchange=exchange,
                routing_key=settings.billing_routing_key,
                declare=[exchange],
                serializer="json",
                delivery_mode=2,
                mandatory=True,
                message_id=snapshot_id,
                headers={
                    "messageId": snapshot_id,
                    "source_application": str(snapshot["application_code"]),
                    "schema_version": "v1",
                    "workspace_uuid": str(snapshot["workspace_uuid"]),
                },
                retry=False,
                timeout=settings.billing_publish_confirm_timeout_seconds,
                confirm_timeout=settings.billing_publish_confirm_timeout_seconds,
            )
    if returned:
        raise UnroutableBillingMessage("RabbitMQ devolveu a mensagem como não roteável")


async def create_billing_reprocess_request(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    billing_period: str,
    idempotency_key: str,
    requested_by: str,
    reason: str,
) -> ReprocessRequest:
    period_start, _ = parse_billing_period(billing_period)
    request_id = str(uuid4())
    insert_result = await db_session.execute(
        text(
            """
            INSERT INTO orch_billing_reprocess_requests (
                request_id, idempotency_key, workspace_uuid, billing_period, requested_by, reason
            )
            VALUES (
                CAST(:request_id AS uuid),
                CAST(:idempotency_key AS uuid),
                CAST(:workspace_uuid AS uuid),
                :billing_period,
                :requested_by,
                :reason
            )
            ON CONFLICT (workspace_uuid, idempotency_key) DO NOTHING
            RETURNING request_id::text AS request_id, status, billing_period
            """
        ),
        {
            "request_id": request_id,
            "idempotency_key": str(UUID(idempotency_key)),
            "workspace_uuid": workspace_uuid,
            "billing_period": period_start.date(),
            "requested_by": requested_by,
            "reason": reason,
        },
    )
    inserted = insert_result.mappings().first()
    if inserted is not None:
        return ReprocessRequest(
            request_id=str(inserted["request_id"]),
            status=str(inserted["status"]),
            created=True,
            billing_period=inserted["billing_period"].strftime("%Y-%m"),
        )
    existing_result = await db_session.execute(
        text(
            """
            SELECT request_id::text AS request_id, status, billing_period, requested_by, reason
            FROM orch_billing_reprocess_requests
            WHERE workspace_uuid = CAST(:workspace_uuid AS uuid)
              AND idempotency_key = CAST(:idempotency_key AS uuid)
            """
        ),
        {"workspace_uuid": workspace_uuid, "idempotency_key": str(UUID(idempotency_key))},
    )
    existing = existing_result.mappings().one()
    if (
        existing["billing_period"] != period_start.date()
        or str(existing["requested_by"]) != requested_by
        or str(existing["reason"]) != reason
    ):
        raise BillingIdempotencyConflict(
            "Idempotency-Key já foi usada com billing_period, requested_by ou reason diferente."
        )
    return ReprocessRequest(
        request_id=str(existing["request_id"]),
        status=str(existing["status"]),
        created=False,
        billing_period=existing["billing_period"].strftime("%Y-%m"),
    )


async def recover_and_list_billing_reprocess_requests(
    db_session: AsyncSession,
    *,
    lease_seconds: int,
    limit: int,
) -> list[str]:
    await db_session.execute(
        text(
            """
            UPDATE orch_billing_reprocess_requests
            SET status = 'accepted', error = 'reprocess lease expired',
                last_enqueued_at = NULL, updated_at = NOW()
            WHERE status = 'running'
              AND updated_at < NOW() - make_interval(secs => CAST(:lease_seconds AS int))
            """
        ),
        {"lease_seconds": int(lease_seconds)},
    )
    result = await db_session.execute(
        text(
            """
            WITH due AS (
                SELECT request_id
                FROM orch_billing_reprocess_requests
                WHERE status = 'accepted'
                  AND (
                      last_enqueued_at IS NULL
                      OR last_enqueued_at < NOW() - make_interval(secs => CAST(:lease_seconds AS int))
                  )
                ORDER BY created_at, request_id
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            UPDATE orch_billing_reprocess_requests AS requests
            SET last_enqueued_at = NOW(), updated_at = NOW()
            FROM due
            WHERE requests.request_id = due.request_id
            RETURNING requests.request_id::text AS request_id
            """
        ),
        {"lease_seconds": int(lease_seconds), "limit": max(1, int(limit))},
    )
    return [str(row["request_id"]) for row in result.mappings().all()]


async def clear_billing_reprocess_enqueue_reservation(
    db_session: AsyncSession,
    *,
    request_id: str,
) -> None:
    await db_session.execute(
        text(
            """
            UPDATE orch_billing_reprocess_requests
            SET last_enqueued_at = NULL, updated_at = NOW()
            WHERE request_id = CAST(:request_id AS uuid)
              AND status = 'accepted'
            """
        ),
        {"request_id": request_id},
    )


async def mark_billing_reprocess_enqueued(
    db_session: AsyncSession,
    *,
    request_id: str,
) -> None:
    await db_session.execute(
        text(
            """
            UPDATE orch_billing_reprocess_requests
            SET last_enqueued_at = NOW(), updated_at = NOW()
            WHERE request_id = CAST(:request_id AS uuid)
              AND status = 'accepted'
            """
        ),
        {"request_id": request_id},
    )


async def claim_billing_reprocess_request(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    request_id: str,
) -> bool:
    result = await db_session.execute(
        text(
            """
            UPDATE orch_billing_reprocess_requests
            SET status = 'running', started_at = COALESCE(started_at, NOW()), error = NULL, updated_at = NOW()
            WHERE request_id = CAST(:request_id AS uuid)
              AND workspace_uuid = CAST(:workspace_uuid AS uuid)
              AND status = 'accepted'
            RETURNING request_id
            """
        ),
        {"request_id": request_id, "workspace_uuid": workspace_uuid},
    )
    return result.scalar_one_or_none() is not None


async def process_billing_reprocess_request(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    request_id: str,
    settings: Settings,
) -> dict[str, int | bool] | None:
    request_result = await db_session.execute(
        text(
            """
            SELECT billing_period
            FROM orch_billing_reprocess_requests
            WHERE request_id = CAST(:request_id AS uuid)
              AND workspace_uuid = CAST(:workspace_uuid AS uuid)
              AND status = 'running'
            FOR UPDATE
            """
        ),
        {"request_id": request_id, "workspace_uuid": workspace_uuid},
    )
    billing_period = request_result.scalar_one_or_none()
    if billing_period is None:
        return None
    if isinstance(billing_period, datetime):
        billing_period = billing_period.date()
    period_start = datetime.combine(billing_period, datetime.min.time(), tzinfo=timezone.utc)
    if billing_period.month == 12:
        next_period = billing_period.replace(year=billing_period.year + 1, month=1)
    else:
        next_period = billing_period.replace(month=billing_period.month + 1)
    period_end = datetime.combine(next_period, datetime.min.time(), tzinfo=timezone.utc)

    chunk_result = await db_session.execute(
        text(
            """
            WITH candidates AS MATERIALIZED (
                SELECT id, uuid, created_at
                FROM orch_sessions
                WHERE created_at >= :period_start
                  AND created_at < :period_end
                  AND id > COALESCE((
                      SELECT cursor_session_id
                      FROM orch_billing_reprocess_requests
                      WHERE request_id = CAST(:request_id AS uuid)
                  ), 0)
                ORDER BY id
                LIMIT :candidate_limit
            ),
            selected AS MATERIALIZED (
                SELECT id, uuid, created_at
                FROM candidates
                ORDER BY id
                LIMIT :chunk_size
            ),
            inserted AS (
                INSERT INTO orch_billing_events (
                    workspace_uuid, source_session_id, source_session_uuid,
                    billing_period, occurred_at, metric_code, service_code
                )
                SELECT
                    CAST(:workspace_uuid AS uuid), id, uuid,
                    date_trunc('month', created_at AT TIME ZONE 'UTC')::date,
                    created_at, :metric_code, :service_code
                FROM selected
                ON CONFLICT (workspace_uuid, source_session_uuid, billing_period, metric_code)
                DO NOTHING
                RETURNING id
            )
            SELECT
                (SELECT COUNT(*) FROM selected) AS source_sessions,
                COALESCE((SELECT MAX(id) FROM selected), 0) AS cursor_session_id,
                (SELECT COUNT(*) FROM inserted) AS events_created,
                (SELECT COUNT(*) FROM candidates) > :chunk_size AS has_more
            """
        ),
        {
            "request_id": request_id,
            "workspace_uuid": workspace_uuid,
            "period_start": period_start,
            "period_end": period_end,
            "metric_code": settings.billing_metric_code,
            "service_code": settings.billing_service_code,
            "chunk_size": settings.billing_reprocess_chunk_size,
            "candidate_limit": settings.billing_reprocess_chunk_size + 1,
        },
    )
    chunk = chunk_result.mappings().one()
    source_sessions = int(chunk["source_sessions"] or 0)
    events_created = int(chunk["events_created"] or 0)
    cursor_session_id = int(chunk["cursor_session_id"] or 0)
    has_more = bool(chunk["has_more"])
    if source_sessions:
        await db_session.execute(
            text(
                """
                UPDATE orch_billing_reprocess_requests
                SET source_sessions = source_sessions + :source_sessions,
                    events_created = events_created + :events_created,
                    cursor_session_id = :cursor_session_id,
                    updated_at = NOW()
                WHERE request_id = CAST(:request_id AS uuid)
                  AND status = 'running'
                """
            ),
            {
                "request_id": request_id,
                "source_sessions": source_sessions,
                "events_created": events_created,
                "cursor_session_id": cursor_session_id,
            },
        )
    if has_more:
        return {
            "source_sessions": source_sessions,
            "events_created": events_created,
            "snapshots_requeued": 0,
            "processing_deferred": 0,
            "completed": False,
        }
    requeue_result = await db_session.execute(
        text(
            """
            WITH requeued AS (
                UPDATE orch_billing_snapshots
                SET status = 'pending', next_attempt_at = NOW(), claim_token = NULL,
                    claimed_at = NULL, last_error = NULL,
                    reprocess_count = reprocess_count + 1, updated_at = NOW()
                WHERE workspace_uuid = CAST(:workspace_uuid AS uuid)
                  AND billing_period = :billing_period
                  AND status <> 'processing'
                RETURNING 1
            )
            SELECT COUNT(*) FROM requeued
            """
        ),
        {"workspace_uuid": workspace_uuid, "billing_period": billing_period},
    )
    snapshots_requeued = int(requeue_result.scalar_one() or 0)
    deferred_result = await db_session.execute(
        text(
            """
            WITH deferred AS (
                UPDATE orch_billing_snapshots
                SET reprocess_requested = TRUE, reprocess_count = reprocess_count + 1, updated_at = NOW()
                WHERE workspace_uuid = CAST(:workspace_uuid AS uuid)
                  AND billing_period = :billing_period
                  AND status = 'processing'
                RETURNING 1
            )
            SELECT COUNT(*) FROM deferred
            """
        ),
        {"workspace_uuid": workspace_uuid, "billing_period": billing_period},
    )
    processing_deferred = int(deferred_result.scalar_one() or 0)
    await db_session.execute(
        text(
            """
            UPDATE orch_billing_reprocess_requests
            SET
                status = 'completed',
                snapshots_requeued = :snapshots_requeued,
                processing_deferred = :processing_deferred,
                completed_at = NOW(),
                updated_at = NOW()
            WHERE request_id = CAST(:request_id AS uuid)
              AND status = 'running'
            """
        ),
        {
            "request_id": request_id,
            "snapshots_requeued": snapshots_requeued,
            "processing_deferred": processing_deferred,
        },
    )
    return {
        "source_sessions": source_sessions,
        "events_created": events_created,
        "snapshots_requeued": snapshots_requeued,
        "processing_deferred": processing_deferred,
        "completed": True,
    }


async def mark_billing_reprocess_failed(
    db_session: AsyncSession,
    *,
    request_id: str,
    error: str,
) -> None:
    await db_session.execute(
        text(
            """
            UPDATE orch_billing_reprocess_requests
            SET status = 'failed', error = :error, completed_at = NOW(), updated_at = NOW()
            WHERE request_id = CAST(:request_id AS uuid)
              AND status IN ('accepted', 'running')
            """
        ),
        {"request_id": request_id, "error": error[:1000]},
    )


async def release_billing_reprocess_for_scan(
    db_session: AsyncSession,
    *,
    request_id: str,
    error: str,
) -> None:
    await db_session.execute(
        text(
            """
            UPDATE orch_billing_reprocess_requests
            SET status = 'accepted', last_enqueued_at = NULL,
                error = :error, updated_at = NOW()
            WHERE request_id = CAST(:request_id AS uuid)
              AND status = 'running'
            """
        ),
        {"request_id": request_id, "error": error[:1000]},
    )


async def get_billing_status(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    billing_period: str,
) -> dict[str, Any]:
    period_start, _ = parse_billing_period(billing_period)
    event_result = await db_session.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                COUNT(*) FILTER (WHERE status = 'batched') AS batched,
                COUNT(*) FILTER (WHERE status = 'sent') AS sent,
                MIN(created_at) FILTER (WHERE status = 'pending') AS oldest_pending_at
            FROM orch_billing_events
            WHERE workspace_uuid = CAST(:workspace_uuid AS uuid)
              AND billing_period = :billing_period
            """
        ),
        {"workspace_uuid": workspace_uuid, "billing_period": period_start.date()},
    )
    snapshot_result = await db_session.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                COUNT(*) FILTER (WHERE status = 'processing') AS processing,
                COUNT(*) FILTER (WHERE status = 'sent') AS sent,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                COUNT(*) FILTER (WHERE status = 'blocked') AS blocked,
                COALESCE(SUM(quantity) FILTER (WHERE status = 'sent'), 0) AS quantity_sent,
                MIN(created_at) FILTER (WHERE status IN ('pending', 'failed')) AS oldest_pending_at,
                COALESCE(MAX(attempt_count), 0) AS max_attempt_count
            FROM orch_billing_snapshots
            WHERE workspace_uuid = CAST(:workspace_uuid AS uuid)
              AND billing_period = :billing_period
            """
        ),
        {"workspace_uuid": workspace_uuid, "billing_period": period_start.date()},
    )
    events = dict(event_result.mappings().one())
    snapshots = dict(snapshot_result.mappings().one())
    oldest_values = [value for value in (events.get("oldest_pending_at"), snapshots.get("oldest_pending_at")) if value]
    return {
        "workspace_uuid": workspace_uuid,
        "billing_period": billing_period,
        "events": {key: int(events.get(key) or 0) for key in ("pending", "batched", "sent")},
        "snapshots": {
            key: int(snapshots.get(key) or 0)
            for key in ("pending", "processing", "sent", "failed", "blocked")
        },
        "quantity_sent": int(snapshots.get("quantity_sent") or 0),
        "oldest_pending_at": min(oldest_values) if oldest_values else None,
        "max_attempt_count": int(snapshots.get("max_attempt_count") or 0),
    }
