from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.core.workspace import workspace_schema_from_uuid
from app.schemas.orch_observability import (
    JourneyFlowSummary,
    JourneySessionListResponse,
    JourneySessionTrace,
)
from app.services.journey_observability_service import (
    JourneyObservabilityNotFoundError,
    JourneyObservabilityValidationError,
    get_flow_journey_summary,
    get_session_journey_trace,
    list_flow_journey_sessions,
)
from app.services.journey_dashboard_websocket_service import (
    JourneyDashboardConnectionLimitError,
    consume_journey_dashboard_ticket,
    create_journey_dashboard_ticket,
    journey_dashboard_workspace_hub,
)
from app.services.journey_workspace_aggregation_service import (
    build_journey_workspace_view,
)
from app.services.journey_workspace_snapshot_service import (
    JourneyWorkspaceSnapshotCurrent,
    fetch_current_journey_workspace_snapshot,
    fetch_journey_workspace_snapshot_sequence,
)
from app.services.workspace_service import bind_workspace_context, ensure_active_workspace


router = APIRouter(prefix="/v1/orch", tags=["orch-observability"])
logger = get_logger(__name__)
_WS_VERSION = "1.0"
_WS_MAX_INPUT_BYTES = 16 * 1024
_WS_HEARTBEAT_SECONDS = 30
_WS_SEND_TIMEOUT_SECONDS = 5


def require_observability_reader(
    client_id: str | None = Header(default=None, alias="X-Orch-Observability-Client-Id"),
    client_secret: str | None = Header(default=None, alias="X-Orch-Observability-Client-Secret"),
) -> str:
    settings = get_settings()
    expected_id = str(settings.orch_observability_client_id or "").strip()
    expected_secret = str(settings.orch_observability_client_secret or "").strip()
    if not expected_id or not expected_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A consulta de jornadas ainda não foi habilitada neste ambiente.",
        )
    provided_id = str(client_id or "").strip()
    provided_secret = str(client_secret or "").strip()
    if not provided_id or not provided_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais de observabilidade ausentes.",
        )
    if not (
        secrets.compare_digest(provided_id, expected_id)
        and secrets.compare_digest(provided_secret, expected_secret)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais de observabilidade inválidas.",
        )
    return provided_id


def _is_statement_timeout(error: DBAPIError) -> bool:
    original = getattr(error, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return str(sqlstate or "") == "57014"


def _raise_observability_error(error: Exception) -> None:
    if isinstance(error, JourneyObservabilityValidationError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    if isinstance(error, JourneyObservabilityNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    if isinstance(error, DBAPIError) and _is_statement_timeout(error):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A consulta excedeu o limite seguro. Reduza o período e tente novamente.",
        ) from error
    raise error


async def _bind_active_workspace(db_session: AsyncSession, *, workspace_uuid: UUID) -> str:
    safe_workspace_uuid = str(workspace_uuid)
    # This must be the first SQL statement in the request transaction. PostgreSQL
    # then enforces the read-only contract for workspace validation and every
    # observability query that follows.
    await db_session.execute(text("SET TRANSACTION READ ONLY"))
    await ensure_active_workspace(db_session, workspace_uuid=safe_workspace_uuid)
    bind_workspace_context(safe_workspace_uuid)
    return safe_workspace_uuid


@router.get(
    "/{workspace_uuid}/observability/flows/{flow_uuid}/journey",
    response_model=JourneyFlowSummary,
    dependencies=[Depends(require_observability_reader)],
)
async def get_flow_journey(
    workspace_uuid: UUID,
    flow_uuid: UUID,
    period_from: datetime = Query(alias="from"),
    period_to: datetime = Query(alias="to"),
    revision_id: UUID | None = Query(default=None),
    db_session: AsyncSession = Depends(get_db_session),
) -> JourneyFlowSummary:
    await _bind_active_workspace(db_session, workspace_uuid=workspace_uuid)
    try:
        return await get_flow_journey_summary(
            db_session,
            flow_uuid=str(flow_uuid),
            period_from=period_from,
            period_to=period_to,
            revision_id=str(revision_id) if revision_id else None,
        )
    except Exception as error:
        _raise_observability_error(error)
        raise AssertionError("unreachable")


@router.get(
    "/{workspace_uuid}/observability/flows/{flow_uuid}/sessions",
    response_model=JourneySessionListResponse,
    dependencies=[Depends(require_observability_reader)],
)
async def get_flow_journey_sessions(
    workspace_uuid: UUID,
    flow_uuid: UUID,
    period_from: datetime = Query(alias="from"),
    period_to: datetime = Query(alias="to"),
    person_uuid: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1024),
    db_session: AsyncSession = Depends(get_db_session),
) -> JourneySessionListResponse:
    await _bind_active_workspace(db_session, workspace_uuid=workspace_uuid)
    try:
        return await list_flow_journey_sessions(
            db_session,
            flow_uuid=str(flow_uuid),
            period_from=period_from,
            period_to=period_to,
            person_uuid=str(person_uuid) if person_uuid else None,
            limit=limit,
            cursor=cursor,
        )
    except Exception as error:
        _raise_observability_error(error)
        raise AssertionError("unreachable")


@router.get(
    "/{workspace_uuid}/observability/flows/{flow_uuid}/sessions/{session_uuid}/trace",
    response_model=JourneySessionTrace,
    dependencies=[Depends(require_observability_reader)],
)
async def get_flow_session_journey_trace(
    workspace_uuid: UUID,
    flow_uuid: UUID,
    session_uuid: UUID,
    db_session: AsyncSession = Depends(get_db_session),
) -> JourneySessionTrace:
    await _bind_active_workspace(db_session, workspace_uuid=workspace_uuid)
    try:
        return await get_session_journey_trace(
            db_session,
            flow_uuid=str(flow_uuid),
            session_uuid=str(session_uuid),
        )
    except Exception as error:
        _raise_observability_error(error)
        raise AssertionError("unreachable")


@router.post(
    "/{workspace_uuid}/observability/journey-dashboard/ws-ticket",
    status_code=status.HTTP_201_CREATED,
)
async def create_journey_dashboard_ws_ticket(
    workspace_uuid: UUID,
    principal: str = Depends(require_observability_reader),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict:
    settings = get_settings()
    if not settings.orch_journey_dashboard_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A dashboard de jornadas não está habilitada neste ambiente.",
        )
    redis_url = str(settings.orch_journey_redis_url or "").strip()
    if not redis_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A conexão em tempo real não está disponível.",
        )
    safe_workspace_uuid = str(workspace_uuid)
    await db_session.execute(text("SET TRANSACTION READ ONLY"))
    workspace = await ensure_active_workspace(
        db_session,
        workspace_uuid=safe_workspace_uuid,
    )
    try:
        ticket = await create_journey_dashboard_ticket(
            redis_url=redis_url,
            workspace_uuid=safe_workspace_uuid,
            workspace_name=str(workspace.get("name") or safe_workspace_uuid),
            principal=principal,
            ttl_seconds=settings.orch_journey_ws_ticket_ttl_seconds,
        )
    except Exception as exc:
        logger.warning(
            "journey dashboard ticket creation failed",
            extra={
                "event": "orch.journey_dashboard.websocket.ticket_failed",
                "workspace_uuid": safe_workspace_uuid,
                "exception_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A conexão em tempo real não está disponível.",
        ) from exc
    return {
        "data": {
            "ticket": ticket,
            "expires_in_seconds": settings.orch_journey_ws_ticket_ttl_seconds,
        }
    }


async def _load_workspace_snapshot(
    workspace_uuid: str,
) -> JourneyWorkspaceSnapshotCurrent | None:
    safe_schema = workspace_schema_from_uuid(workspace_uuid).replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text("SET TRANSACTION READ ONLY"))
            await db_session.execute(
                text(f'SET LOCAL search_path TO "{safe_schema}"')
            )
            return await fetch_current_journey_workspace_snapshot(db_session)


async def _load_workspace_snapshot_sequence(workspace_uuid: str) -> int:
    safe_schema = workspace_schema_from_uuid(workspace_uuid).replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text("SET TRANSACTION READ ONLY"))
            await db_session.execute(
                text(f'SET LOCAL search_path TO "{safe_schema}"')
            )
            return await fetch_journey_workspace_snapshot_sequence(db_session)


async def _send_ws_json(websocket: WebSocket, payload: dict) -> None:
    await asyncio.wait_for(
        websocket.send_json(payload),
        timeout=_WS_SEND_TIMEOUT_SECONDS,
    )


async def _send_current_snapshot(
    websocket: WebSocket,
    *,
    workspace_uuid: str,
    reason: str,
) -> int:
    current = await _load_workspace_snapshot(workspace_uuid)
    if current is None:
        await _send_ws_json(
            websocket,
            {
                "type": "error",
                "code": "snapshot_unavailable",
                "message": "O primeiro snapshot ainda está sendo preparado.",
                "retryable": True,
            },
        )
        return 0
    frame = dict(current.payload)
    frame["meta"] = {**dict(frame.get("meta") or {}), "reason": reason}
    await _send_ws_json(websocket, frame)
    return current.snapshot_sequence


def _parse_view_filters(
    raw_filters: Any,
    *,
    current: JourneyWorkspaceSnapshotCurrent,
) -> tuple[dict[str, Any], datetime, datetime]:
    if not isinstance(raw_filters, dict):
        raise ValueError("invalid_filters")
    allowed_keys = {"flow_uuid", "revision_uuid", "channels", "period"}
    if set(raw_filters) - allowed_keys:
        raise ValueError("invalid_filters")

    payload = current.payload.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("snapshot_unavailable")
    catalog = payload.get("catalog")
    retention = payload.get("retention")
    if not isinstance(catalog, dict) or not isinstance(retention, dict):
        raise ValueError("snapshot_unavailable")

    flow_uuid = raw_filters.get("flow_uuid")
    revision_uuid = raw_filters.get("revision_uuid")
    safe_flow_uuid = str(UUID(str(flow_uuid))) if flow_uuid else None
    safe_revision_uuid = str(UUID(str(revision_uuid))) if revision_uuid else None
    known_flows = {
        str(item.get("uuid"))
        for item in catalog.get("flows", [])
        if isinstance(item, dict) and item.get("uuid")
    }
    known_revisions = {
        str(item.get("uuid")): str(item.get("flow_uuid"))
        for item in catalog.get("revisions", [])
        if isinstance(item, dict) and item.get("uuid")
    }
    if safe_flow_uuid and safe_flow_uuid not in known_flows:
        raise ValueError("invalid_filters")
    if safe_revision_uuid and safe_revision_uuid not in known_revisions:
        raise ValueError("invalid_filters")
    if (
        safe_flow_uuid
        and safe_revision_uuid
        and known_revisions[safe_revision_uuid] != safe_flow_uuid
    ):
        raise ValueError("invalid_filters")

    raw_channels = raw_filters.get("channels")
    if raw_channels is None:
        channels = list(catalog.get("channels") or [])
    elif isinstance(raw_channels, list) and len(raw_channels) <= 5:
        channels = sorted({str(item).strip().lower() for item in raw_channels})
    else:
        raise ValueError("invalid_filters")
    known_channels = set(catalog.get("channels") or [])
    if not channels or any(channel not in known_channels for channel in channels):
        raise ValueError("invalid_filters")

    raw_period = raw_filters.get("period")
    if not isinstance(raw_period, dict) or set(raw_period) != {
        "from",
        "to",
        "timezone",
    }:
        raise ValueError("invalid_filters")
    try:
        period_from_date = date.fromisoformat(str(raw_period["from"]))
        period_to_date = date.fromisoformat(str(raw_period["to"]))
        timezone_name = str(raw_period["timezone"])
        timezone_value = ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("invalid_filters") from exc
    if period_from_date > period_to_date:
        raise ValueError("invalid_filters")
    if (period_to_date - period_from_date).days >= 30:
        raise ValueError("period_unavailable")
    period_from = datetime.combine(
        period_from_date,
        time.min,
        timezone_value,
    ).astimezone(UTC)
    period_to = datetime.combine(
        period_to_date + timedelta(days=1),
        time.min,
        timezone_value,
    ).astimezone(UTC)
    now = datetime.now(UTC)
    period_to = min(period_to, now + timedelta(seconds=1))

    oldest_raw = str(retention.get("oldest_available_at") or "")
    try:
        oldest_available = datetime.fromisoformat(oldest_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("snapshot_unavailable") from exc
    # Coverage may start in the middle of the first day. The public filter is
    # intentionally date-based, so clamp that first bucket to the durable
    # coverage boundary instead of making every detailed view impossible until
    # the following day.
    period_from = max(period_from, oldest_available)
    if period_from >= period_to:
        raise ValueError("period_unavailable")

    normalized = {
        "flow_uuid": safe_flow_uuid,
        "revision_uuid": safe_revision_uuid,
        "channels": channels,
        "period": {
            "from": period_from_date.isoformat(),
            "to": period_to_date.isoformat(),
            "timezone": timezone_name,
        },
    }
    return normalized, period_from, period_to


async def _build_workspace_view_response(
    *,
    workspace_uuid: str,
    request_id: str,
    raw_filters: Any,
) -> dict:
    current = await _load_workspace_snapshot(workspace_uuid)
    if current is None:
        raise ValueError("snapshot_unavailable")
    filters, period_from, period_to = _parse_view_filters(
        raw_filters,
        current=current,
    )
    safe_schema = workspace_schema_from_uuid(workspace_uuid).replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text("SET TRANSACTION READ ONLY"))
            await db_session.execute(
                text(f'SET LOCAL search_path TO "{safe_schema}"')
            )
            await db_session.execute(text("SET LOCAL statement_timeout = '10s'"))
            view = await build_journey_workspace_view(
                db_session,
                period_from=period_from,
                period_to=period_to,
                flow_uuid=filters["flow_uuid"],
                revision_uuid=filters["revision_uuid"],
                channels=filters["channels"],
                timezone_name=filters["period"]["timezone"],
            )
    response = {
        "type": "orchestration_workspace_view",
        "request_id": request_id,
        "snapshot_sequence": current.snapshot_sequence,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "filters": filters,
        "view": view,
    }
    if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > 1024 * 1024:
        raise ValueError("view_busy")
    return response


async def _handle_authenticated_ws_message(
    websocket: WebSocket,
    *,
    workspace_uuid: str,
    raw_message: str,
) -> int | None:
    if len(raw_message.encode("utf-8")) > _WS_MAX_INPUT_BYTES:
        await websocket.close(code=1009)
        raise WebSocketDisconnect(code=1009)
    try:
        message = json.loads(raw_message)
    except json.JSONDecodeError:
        message = None
    if not isinstance(message, dict):
        await _send_ws_json(
            websocket,
            {
                "type": "error",
                "code": "invalid_message",
                "message": "A mensagem informada não é válida.",
                "retryable": False,
            },
        )
        return None
    message_type = str(message.get("type") or "")
    if message_type == "resync" and set(message) == {"type"}:
        return await _send_current_snapshot(
            websocket,
            workspace_uuid=workspace_uuid,
            reason="update",
        )
    if message_type != "orchestration_view_request" or set(message) != {
        "type",
        "request_id",
        "filters",
    }:
        await _send_ws_json(
            websocket,
            {
                "type": "error",
                "code": "invalid_message",
                "message": "A mensagem informada não é válida.",
                "retryable": False,
            },
        )
        return None
    request_id = str(message.get("request_id") or "")
    if not 1 <= len(request_id) <= 64:
        await _send_ws_json(
            websocket,
            {
                "type": "error",
                "code": "invalid_message",
                "message": "A mensagem informada não é válida.",
                "retryable": False,
            },
        )
        return None
    try:
        response = await _build_workspace_view_response(
            workspace_uuid=workspace_uuid,
            request_id=request_id,
            raw_filters=message.get("filters"),
        )
    except ValueError as exc:
        code = str(exc)
        if code not in {
            "invalid_filters",
            "period_unavailable",
            "view_busy",
            "snapshot_unavailable",
        }:
            code = "invalid_filters"
        await _send_ws_json(
            websocket,
            {
                "type": "error",
                "request_id": request_id,
                "code": code,
                "message": "Os filtros informados não estão disponíveis.",
                "retryable": code in {"view_busy", "snapshot_unavailable"},
            },
        )
        return None
    await _send_ws_json(websocket, response)
    return None


@router.websocket("/observability/journey-dashboard/ws")
async def journey_dashboard_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    settings = get_settings()
    redis_url = str(settings.orch_journey_redis_url or "").strip()
    if not settings.orch_journey_dashboard_enabled or not redis_url:
        await websocket.close(code=1013)
        return

    try:
        raw_auth = await asyncio.wait_for(websocket.receive_text(), timeout=5)
    except (TimeoutError, WebSocketDisconnect):
        await websocket.close(code=4401)
        return
    if len(raw_auth.encode("utf-8")) > _WS_MAX_INPUT_BYTES:
        await websocket.close(code=1009)
        return
    try:
        auth = json.loads(raw_auth)
    except json.JSONDecodeError:
        auth = None
    if (
        not isinstance(auth, dict)
        or set(auth) != {"type", "version", "ticket"}
        or auth.get("type") != "authenticate"
        or auth.get("version") != _WS_VERSION
    ):
        await websocket.close(code=4401)
        return
    try:
        ticket = await consume_journey_dashboard_ticket(
            redis_url=redis_url,
            ticket=str(auth.get("ticket") or ""),
        )
    except Exception:
        await websocket.close(code=4401)
        return
    if ticket is None:
        await websocket.close(code=4401)
        return

    try:
        notification_queue = await journey_dashboard_workspace_hub.register(
            redis_url=redis_url,
            workspace_uuid=ticket.workspace_uuid,
        )
    except JourneyDashboardConnectionLimitError:
        await websocket.close(code=4429)
        return
    except Exception:
        await websocket.close(code=1013)
        return

    receive_task: asyncio.Task[str] | None = None
    notification_task: asyncio.Task[int] | None = None
    last_sequence = 0
    try:
        await _send_ws_json(
            websocket,
            {
                "type": "authenticated",
                "version": _WS_VERSION,
                "workspace_uuid": ticket.workspace_uuid,
                "heartbeat_seconds": _WS_HEARTBEAT_SECONDS,
            },
        )
        last_sequence = await _send_current_snapshot(
            websocket,
            workspace_uuid=ticket.workspace_uuid,
            reason="initial",
        )
        receive_task = asyncio.create_task(websocket.receive_text())
        notification_task = asyncio.create_task(notification_queue.get())
        while True:
            done, _pending = await asyncio.wait(
                {receive_task, notification_task},
                timeout=_WS_HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                latest_sequence = await _load_workspace_snapshot_sequence(
                    ticket.workspace_uuid
                )
                if latest_sequence > last_sequence:
                    last_sequence = await _send_current_snapshot(
                        websocket,
                        workspace_uuid=ticket.workspace_uuid,
                        reason="update",
                    )
                    latest_sequence = max(latest_sequence, last_sequence)
                await _send_ws_json(
                    websocket,
                    {
                        "type": "heartbeat",
                        "workspace_uuid": ticket.workspace_uuid,
                        "latest_snapshot_sequence": latest_sequence,
                        "sent_at": datetime.now(UTC).isoformat().replace(
                            "+00:00", "Z"
                        ),
                    },
                )
                continue
            if notification_task in done:
                notified_sequence = notification_task.result()
                notification_task = asyncio.create_task(notification_queue.get())
                if notified_sequence > last_sequence:
                    last_sequence = await _send_current_snapshot(
                        websocket,
                        workspace_uuid=ticket.workspace_uuid,
                        reason="update",
                    )
            if receive_task in done:
                raw_message = receive_task.result()
                receive_task = asyncio.create_task(websocket.receive_text())
                replacement_sequence = await _handle_authenticated_ws_message(
                    websocket,
                    workspace_uuid=ticket.workspace_uuid,
                    raw_message=raw_message,
                )
                if replacement_sequence is not None:
                    last_sequence = max(last_sequence, replacement_sequence)
    except WebSocketDisconnect:
        pass
    except TimeoutError:
        try:
            await websocket.close(code=1013)
        except RuntimeError:
            pass
    except Exception as exc:
        logger.warning(
            "journey dashboard websocket failed",
            extra={
                "event": "orch.journey_dashboard.websocket.failed",
                "workspace_uuid": ticket.workspace_uuid,
                "exception_type": type(exc).__name__,
            },
        )
        try:
            await websocket.close(code=1011)
        except RuntimeError:
            pass
    finally:
        for task in (receive_task, notification_task):
            if task is not None and not task.done():
                task.cancel()
        await journey_dashboard_workspace_hub.unregister(
            workspace_uuid=ticket.workspace_uuid,
            queue=notification_queue,
        )
