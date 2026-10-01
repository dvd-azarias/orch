from __future__ import annotations

import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.repositories.orch_flow_builder_repository import (
    FlowBuilderSessionNotFoundError,
    FlowBuilderVersionConflictError,
    append_flow_builder_message,
    create_flow_builder_session,
    fetch_flow_builder_session,
    store_flow_builder_compilation,
)
from app.schemas.orch_flow_builder import (
    FlowBuilderCompileRequest,
    FlowBuilderCompileResponse,
    FlowBuilderMessageCreateRequest,
    FlowBuilderSession,
    FlowBuilderSessionCreateRequest,
)
from app.services.flow_builder_compiler import compile_orchestration_flow
from app.services.flow_builder_target_client import (
    FlowBuilderTargetError,
    fetch_orchestration_catalog,
)
from app.services.workspace_service import bind_workspace_context, ensure_active_workspace


router = APIRouter(prefix="/v1/orch", tags=["orch-flow-builder"])


def require_flow_builder_client(
    client_id: str | None = Header(default=None, alias="X-Orch-Flow-Builder-Client-Id"),
    client_secret: str | None = Header(default=None, alias="X-Orch-Flow-Builder-Client-Secret"),
) -> str:
    settings = get_settings()
    if not settings.orch_flow_builder_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="O criador de fluxos do ORCH ainda não foi habilitado neste ambiente.",
        )
    expected_id = str(settings.orch_flow_builder_client_id or "").strip()
    expected_secret = str(settings.orch_flow_builder_client_secret or "").strip()
    if not expected_id or not expected_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="As credenciais internas do criador de fluxos não estão configuradas.",
        )
    provided_id = str(client_id or "").strip()
    provided_secret = str(client_secret or "").strip()
    if not provided_id or not provided_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais do criador de fluxos ausentes.",
        )
    if not (
        secrets.compare_digest(provided_id, expected_id)
        and secrets.compare_digest(provided_secret, expected_secret)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais do criador de fluxos inválidas.",
        )
    return provided_id


def _ensure_workspace_allowed(workspace_uuid: str, settings: Settings) -> None:
    allowed = {str(UUID(value)) for value in settings.orch_flow_builder_workspace_allowlist}
    if workspace_uuid not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="O criador de fluxos não está habilitado para este workspace.",
        )


def _normalize_actor(actor: str) -> str:
    normalized = str(actor or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Identidade do usuário responsável pelo builder é obrigatória.",
        )
    return normalized


async def _bind_builder_workspace(
    db_session: AsyncSession,
    *,
    workspace_uuid: UUID,
) -> str:
    safe_workspace_uuid, _schema = bind_workspace_context(str(workspace_uuid))
    _ensure_workspace_allowed(safe_workspace_uuid, get_settings())
    await ensure_active_workspace(db_session, workspace_uuid=safe_workspace_uuid)
    return safe_workspace_uuid


def _raise_repository_error(error: Exception) -> None:
    if isinstance(error, FlowBuilderSessionNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sessão do criador de fluxos não encontrada.",
        ) from error
    if isinstance(error, FlowBuilderVersionConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A sessão foi alterada. Recarregue-a antes de compilar novamente.",
        ) from error
    raise error


def _raise_target_error(error: FlowBuilderTargetError) -> None:
    if error.code in {"target_core_not_configured", "target_core_unreachable"}:
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        http_status = status.HTTP_502_BAD_GATEWAY
    raise HTTPException(status_code=http_status, detail=error.message) from error


@router.post(
    "/{workspace_uuid}/flow-builder/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=FlowBuilderSession,
    dependencies=[Depends(require_flow_builder_client)],
)
async def create_builder_session(
    workspace_uuid: UUID,
    payload: FlowBuilderSessionCreateRequest,
    actor: str = Header(..., alias="X-Orch-Flow-Builder-Actor", min_length=1, max_length=255),
    db_session: AsyncSession = Depends(get_db_session),
) -> FlowBuilderSession:
    safe_actor = _normalize_actor(actor)
    safe_workspace_uuid = await _bind_builder_workspace(
        db_session,
        workspace_uuid=workspace_uuid,
    )
    if payload.intent != "create":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A edição com IA será habilitada em um gate posterior.",
        )
    created = await create_flow_builder_session(
        db_session,
        workspace_uuid=safe_workspace_uuid,
        intent=payload.intent,
        flow_uuid=None,
        created_by=safe_actor,
        initial_message=payload.initial_message,
    )
    record = await fetch_flow_builder_session(
        db_session,
        workspace_uuid=safe_workspace_uuid,
        session_id=str(created["id"]),
    )
    return FlowBuilderSession.model_validate(record)


@router.get(
    "/{workspace_uuid}/flow-builder/sessions/{session_id}",
    response_model=FlowBuilderSession,
    dependencies=[Depends(require_flow_builder_client)],
)
async def get_builder_session(
    workspace_uuid: UUID,
    session_id: UUID,
    db_session: AsyncSession = Depends(get_db_session),
) -> FlowBuilderSession:
    if payload.attachment_metadata:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Anexos serão habilitados no gate específico de entrada por imagem.",
        )
    safe_workspace_uuid = await _bind_builder_workspace(
        db_session,
        workspace_uuid=workspace_uuid,
    )
    try:
        record = await fetch_flow_builder_session(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            session_id=str(session_id),
        )
    except Exception as error:
        _raise_repository_error(error)
        raise AssertionError("unreachable")
    return FlowBuilderSession.model_validate(record)


@router.post(
    "/{workspace_uuid}/flow-builder/sessions/{session_id}/messages",
    response_model=FlowBuilderSession,
    dependencies=[Depends(require_flow_builder_client)],
)
async def append_builder_message(
    workspace_uuid: UUID,
    session_id: UUID,
    payload: FlowBuilderMessageCreateRequest,
    db_session: AsyncSession = Depends(get_db_session),
) -> FlowBuilderSession:
    safe_workspace_uuid = await _bind_builder_workspace(
        db_session,
        workspace_uuid=workspace_uuid,
    )
    try:
        await append_flow_builder_message(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            session_id=str(session_id),
            role="user",
            content=payload.content,
            attachment_metadata=payload.attachment_metadata,
        )
        record = await fetch_flow_builder_session(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            session_id=str(session_id),
        )
    except Exception as error:
        _raise_repository_error(error)
        raise AssertionError("unreachable")
    return FlowBuilderSession.model_validate(record)


@router.post(
    "/{workspace_uuid}/flow-builder/sessions/{session_id}/compile",
    response_model=FlowBuilderCompileResponse,
    dependencies=[Depends(require_flow_builder_client)],
)
async def compile_builder_session(
    workspace_uuid: UUID,
    session_id: UUID,
    payload: FlowBuilderCompileRequest,
    actor: str = Header(..., alias="X-Orch-Flow-Builder-Actor", min_length=1, max_length=255),
    db_session: AsyncSession = Depends(get_db_session),
) -> FlowBuilderCompileResponse:
    safe_actor = _normalize_actor(actor)
    safe_workspace_uuid = await _bind_builder_workspace(
        db_session,
        workspace_uuid=workspace_uuid,
    )
    try:
        await fetch_flow_builder_session(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            session_id=str(session_id),
        )
    except Exception as error:
        _raise_repository_error(error)
        raise AssertionError("unreachable")

    # Do not hold the workspace transaction open while calling Target Core.
    await db_session.commit()
    try:
        catalog = await fetch_orchestration_catalog(
            workspace_uuid=safe_workspace_uuid,
            actor=safe_actor,
        )
    except FlowBuilderTargetError as error:
        _raise_target_error(error)
        raise AssertionError("unreachable")

    compilation = compile_orchestration_flow(
        payload.plan,
        component_catalog=catalog,
    )
    try:
        await store_flow_builder_compilation(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            session_id=str(session_id),
            expected_version=payload.expected_version,
            plan=payload.plan.model_dump(mode="json"),
            compiled_definition=compilation.definition,
            issues=[issue.model_dump(mode="json") for issue in compilation.issues],
        )
        record = await fetch_flow_builder_session(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            session_id=str(session_id),
        )
    except Exception as error:
        _raise_repository_error(error)
        raise AssertionError("unreachable")
    return FlowBuilderCompileResponse(
        session=FlowBuilderSession.model_validate(record),
        compilation=compilation,
    )
