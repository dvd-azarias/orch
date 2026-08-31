from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.core.billing_celery_app import billing_celery_app
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.core.workspace import workspace_schema_from_uuid
from app.repositories.workspaces_repository import fetch_active_workspace
from app.services.billing_batch_service import (
    InvalidBillingPayload,
    aggregate_next_billing_snapshot,
    calculate_retry_delay_seconds,
    claim_billing_reprocess_request,
    claim_due_billing_snapshots,
    clear_billing_reprocess_enqueue_reservation,
    mark_billing_reprocess_failed,
    mark_billing_snapshot_blocked,
    mark_billing_snapshot_failed,
    mark_billing_snapshot_sent,
    process_billing_reprocess_request,
    publish_batch_billing_snapshot,
    reconcile_billing_events,
    recover_and_list_billing_reprocess_requests,
    release_billing_reprocess_for_scan,
    sanitize_billing_error,
    utc_now,
    validate_batch_snapshot_payload,
)
from app.services.workspace_service import bind_workspace_context, list_completed_workspaces

logger = get_logger(__name__)


async def _completed_workspace_uuids(db_session: Any) -> list[str]:
    workspaces = await list_completed_workspaces(db_session)
    if db_session.in_transaction():
        await db_session.commit()
    return [str(item["workspace_uuid"]) for item in workspaces]


async def _set_workspace_search_path(db_session: Any, workspace_uuid: str) -> None:
    safe_schema = workspace_schema_from_uuid(workspace_uuid).replace('"', '""')
    await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))


@billing_celery_app.task(name="app.tasks.billing.aggregate", ignore_result=True, acks_late=True)
def aggregate_billing_task() -> dict[str, int]:
    return asyncio.run(_aggregate_billing_task())


async def _aggregate_billing_task() -> dict[str, int]:
    settings = get_settings()
    if not settings.orch_billing_enabled:
        return {"snapshots_created": 0, "events_batched": 0}
    snapshots_created = 0
    events_batched = 0
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        for workspace_uuid in await _completed_workspace_uuids(db_session):
            bind_workspace_context(workspace_uuid)
            try:
                while True:
                    async with db_session.begin():
                        await _set_workspace_search_path(db_session, workspace_uuid)
                        aggregated = await aggregate_next_billing_snapshot(
                            db_session,
                            workspace_uuid=workspace_uuid,
                            settings=settings,
                        )
                    if aggregated is None:
                        break
                    snapshots_created += 1
                    events_batched += aggregated.quantity
            except Exception:
                logger.exception(
                    "billing aggregate failed for workspace; continuing",
                    extra={
                        "event": "orch.billing.aggregate.workspace_failed",
                        "workspace_uuid": workspace_uuid,
                    },
                )
    if snapshots_created:
        # The periodic retry scanner remains the fallback. Publishing here avoids
        # adding another scan interval after the five-minute flush tick.
        try:
            await _publish_due_billing_task()
        except Exception:
            logger.exception(
                "billing immediate publish after aggregate failed; retry scanner will recover",
                extra={"event": "orch.billing.aggregate.immediate_publish_failed"},
            )
    return {"snapshots_created": snapshots_created, "events_batched": events_batched}


@billing_celery_app.task(name="app.tasks.billing.publish_due", ignore_result=True, acks_late=True)
def publish_due_billing_task() -> dict[str, int]:
    return asyncio.run(_publish_due_billing_task())


async def _publish_due_billing_task() -> dict[str, int]:
    settings = get_settings()
    if not settings.orch_billing_enabled:
        return {"sent": 0, "failed": 0, "blocked": 0, "stale_claims": 0}
    sent = 0
    failed = 0
    blocked = 0
    stale_claims = 0
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        for workspace_uuid in await _completed_workspace_uuids(db_session):
            bind_workspace_context(workspace_uuid)
            claim_token = str(uuid4())
            try:
                async with db_session.begin():
                    await _set_workspace_search_path(db_session, workspace_uuid)
                    snapshots = await claim_due_billing_snapshots(
                        db_session,
                        workspace_uuid=workspace_uuid,
                        batch_size=settings.billing_publish_claim_batch_size,
                        lease_seconds=settings.billing_processing_lease_seconds,
                        claim_token=claim_token,
                    )
            except Exception:
                logger.exception(
                    "billing publish claim failed for workspace; continuing",
                    extra={
                        "event": "orch.billing.publish.workspace_failed",
                        "workspace_uuid": workspace_uuid,
                    },
                )
                continue
            for item in snapshots:
                snapshot_id = str(item["snapshot_id"])
                item_claim_token = str(item["claim_token"])
                try:
                    raw_payload = item["payload"]
                    if not isinstance(raw_payload, dict):
                        raise InvalidBillingPayload("payload persistido deve ser um objeto JSON")
                    payload = dict(raw_payload)
                    validate_batch_snapshot_payload(
                        payload,
                        snapshot_id=snapshot_id,
                        workspace_uuid=workspace_uuid,
                        quantity=int(item["quantity"]),
                        billing_period=str(item["billing_period"]),
                    )
                except InvalidBillingPayload as exc:
                    async with db_session.begin():
                        await _set_workspace_search_path(db_session, workspace_uuid)
                        changed = await mark_billing_snapshot_blocked(
                            db_session,
                            snapshot_id=snapshot_id,
                            claim_token=item_claim_token,
                            error=sanitize_billing_error(exc),
                        )
                    blocked += int(changed)
                    stale_claims += int(not changed)
                    logger.error(
                        "billing snapshot blocked",
                        extra={
                            "event": "orch.billing.snapshot.blocked",
                            "workspace_uuid": workspace_uuid,
                            "snapshot_id": snapshot_id,
                            "reason": str(exc),
                        },
                    )
                    continue
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            publish_batch_billing_snapshot,
                            snapshot=payload,
                            settings=settings,
                        ),
                        timeout=settings.billing_publish_confirm_timeout_seconds + 1,
                    )
                except Exception as exc:
                    delay = calculate_retry_delay_seconds(
                        attempt_count=int(item["attempt_count"]),
                        initial_seconds=settings.billing_retry_initial_seconds,
                        maximum_seconds=settings.billing_retry_max_seconds,
                        jitter_seconds=settings.billing_retry_jitter_seconds,
                    )
                    async with db_session.begin():
                        await _set_workspace_search_path(db_session, workspace_uuid)
                        changed = await mark_billing_snapshot_failed(
                            db_session,
                            snapshot_id=snapshot_id,
                            claim_token=item_claim_token,
                            error=sanitize_billing_error(exc),
                            next_attempt_at=utc_now() + timedelta(seconds=delay),
                        )
                    failed += int(changed)
                    stale_claims += int(not changed)
                    logger.warning(
                        "billing snapshot publication failed",
                        extra={
                            "event": "orch.billing.snapshot.failed",
                            "workspace_uuid": workspace_uuid,
                            "snapshot_id": snapshot_id,
                            "attempt_count": int(item["attempt_count"]),
                            "retry_in_seconds": delay,
                            "exception_type": type(exc).__name__,
                        },
                    )
                    continue
                async with db_session.begin():
                    await _set_workspace_search_path(db_session, workspace_uuid)
                    changed = await mark_billing_snapshot_sent(
                        db_session,
                        snapshot_id=snapshot_id,
                        claim_token=item_claim_token,
                    )
                sent += int(changed)
                stale_claims += int(not changed)
                if changed:
                    logger.info(
                        "billing snapshot broker-confirmed",
                        extra={
                            "event": "orch.billing.snapshot.sent",
                            "workspace_uuid": workspace_uuid,
                            "snapshot_id": snapshot_id,
                            "quantity": int(item["quantity"]),
                            "exchange": settings.billing_exchange,
                            "routing_key": settings.billing_routing_key,
                        },
                    )
    return {"sent": sent, "failed": failed, "blocked": blocked, "stale_claims": stale_claims}


@billing_celery_app.task(name="app.tasks.billing.reconcile", ignore_result=True, acks_late=True)
def reconcile_billing_task() -> dict[str, int]:
    return asyncio.run(_reconcile_billing_task())


async def _reconcile_billing_task() -> dict[str, int]:
    settings = get_settings()
    if not settings.orch_billing_enabled:
        return {"events_created": 0}
    period_end = utc_now()
    period_start = period_end - timedelta(hours=settings.billing_reconcile_lookback_hours)
    events_created = 0
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        for workspace_uuid in await _completed_workspace_uuids(db_session):
            bind_workspace_context(workspace_uuid)
            try:
                async with db_session.begin():
                    await _set_workspace_search_path(db_session, workspace_uuid)
                    events_created += await reconcile_billing_events(
                        db_session,
                        workspace_uuid=workspace_uuid,
                        period_start=period_start,
                        period_end=period_end,
                        settings=settings,
                    )
            except Exception:
                logger.exception(
                    "billing reconcile failed for workspace; continuing",
                    extra={
                        "event": "orch.billing.reconcile.workspace_failed",
                        "workspace_uuid": workspace_uuid,
                    },
                )
    return {"events_created": events_created}


@billing_celery_app.task(name="app.tasks.billing.scan_reprocess", ignore_result=True, acks_late=True)
def scan_billing_reprocess_task() -> dict[str, int]:
    return asyncio.run(_scan_billing_reprocess_task())


async def _scan_billing_reprocess_task() -> dict[str, int]:
    settings = get_settings()
    if not settings.orch_billing_enabled:
        return {"enqueued": 0, "enqueue_failed": 0}
    enqueued = 0
    enqueue_failed = 0
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        for workspace_uuid in await _completed_workspace_uuids(db_session):
            bind_workspace_context(workspace_uuid)
            try:
                async with db_session.begin():
                    await _set_workspace_search_path(db_session, workspace_uuid)
                    request_ids = await recover_and_list_billing_reprocess_requests(
                        db_session,
                        lease_seconds=settings.billing_reprocess_lease_seconds,
                        limit=settings.billing_publish_claim_batch_size,
                    )
            except Exception:
                logger.exception(
                    "billing reprocess scan failed for workspace; continuing",
                    extra={
                        "event": "orch.billing.reprocess.scan_workspace_failed",
                        "workspace_uuid": workspace_uuid,
                    },
                )
                continue
            for request_id in request_ids:
                try:
                    billing_reprocess_task.apply_async(
                        kwargs={"workspace_uuid": workspace_uuid, "request_id": request_id},
                        queue=settings.celery_billing_queue,
                        routing_key=settings.celery_billing_queue,
                    )
                    enqueued += 1
                except Exception as exc:
                    enqueue_failed += 1
                    async with db_session.begin():
                        await _set_workspace_search_path(db_session, workspace_uuid)
                        await clear_billing_reprocess_enqueue_reservation(
                            db_session,
                            request_id=request_id,
                        )
                    logger.warning(
                        "billing reprocess enqueue failed",
                        extra={
                            "event": "orch.billing.reprocess.enqueue_failed",
                            "workspace_uuid": workspace_uuid,
                            "request_id": request_id,
                            "exception_type": type(exc).__name__,
                        },
                    )
    return {"enqueued": enqueued, "enqueue_failed": enqueue_failed}


@billing_celery_app.task(name="app.tasks.billing.reprocess", ignore_result=True, acks_late=True)
def billing_reprocess_task(
    *, workspace_uuid: str, request_id: str, continuation: bool = False
) -> dict[str, int | bool] | None:
    return asyncio.run(
        _billing_reprocess_task(
            workspace_uuid=workspace_uuid,
            request_id=request_id,
            continuation=continuation,
        )
    )


async def _billing_reprocess_task(
    *, workspace_uuid: str, request_id: str, continuation: bool = False
) -> dict[str, int | bool] | None:
    settings = get_settings()
    if not settings.orch_billing_enabled:
        return None
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        workspace = await fetch_active_workspace(db_session, workspace_uuid=workspace_uuid)
        if db_session.in_transaction():
            await db_session.commit()
        if workspace is None or str(workspace.get("provision_status") or "").lower() != "completed":
            return None
        bind_workspace_context(workspace_uuid)
        try:
            if not continuation:
                async with db_session.begin():
                    await _set_workspace_search_path(db_session, workspace_uuid)
                    claimed = await claim_billing_reprocess_request(
                        db_session,
                        workspace_uuid=workspace_uuid,
                        request_id=request_id,
                    )
                if not claimed:
                    return None
            async with db_session.begin():
                await _set_workspace_search_path(db_session, workspace_uuid)
                result = await process_billing_reprocess_request(
                    db_session,
                    workspace_uuid=workspace_uuid,
                    request_id=request_id,
                    settings=settings,
                )
            if result is not None and not bool(result.get("completed", True)):
                try:
                    billing_reprocess_task.apply_async(
                        kwargs={
                            "workspace_uuid": workspace_uuid,
                            "request_id": request_id,
                            "continuation": True,
                        },
                        queue=settings.celery_billing_queue,
                        routing_key=settings.celery_billing_queue,
                    )
                except Exception as exc:
                    async with db_session.begin():
                        await _set_workspace_search_path(db_session, workspace_uuid)
                        await release_billing_reprocess_for_scan(
                            db_session,
                            request_id=request_id,
                            error=sanitize_billing_error(exc),
                        )
                    logger.warning(
                        "billing reprocess continuation enqueue failed; scanner will recover",
                        extra={
                            "event": "orch.billing.reprocess.continuation_enqueue_failed",
                            "workspace_uuid": workspace_uuid,
                            "request_id": request_id,
                        },
                    )
            return result
        except Exception as exc:
            async with db_session.begin():
                await _set_workspace_search_path(db_session, workspace_uuid)
                await mark_billing_reprocess_failed(
                    db_session,
                    request_id=request_id,
                    error=sanitize_billing_error(exc),
                )
            logger.exception(
                "billing reprocess failed",
                extra={
                    "event": "orch.billing.reprocess.failed",
                    "workspace_uuid": workspace_uuid,
                    "request_id": request_id,
                },
            )
            return None
