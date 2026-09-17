from __future__ import annotations

import secrets
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db_session
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
from app.services.workspace_service import bind_workspace_context, ensure_active_workspace


router = APIRouter(prefix="/v1/orch", tags=["orch-observability"])


def require_observability_reader(
    client_id: str | None = Header(default=None, alias="X-Orch-Observability-Client-Id"),
    client_secret: str | None = Header(default=None, alias="X-Orch-Observability-Client-Secret"),
) -> None:
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
