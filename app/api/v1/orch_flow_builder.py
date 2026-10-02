from __future__ import annotations

import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.repositories.orch_flow_builder_repository import (
    FlowBuilderSessionNotFoundError,
    FlowBuilderVersionConflictError,
    apply_flow_builder_assistant_turn,
    append_flow_builder_message,
    create_flow_builder_session,
    fetch_flow_builder_session,
    lock_flow_builder_session_for_draft,
    store_flow_builder_draft_result,
    store_flow_builder_compilation,
)
from app.repositories.workspaces_repository import fetch_workspace_otima_billing_api_key
from app.schemas.orch_flow_builder import (
    FlowBuilderAssistRequest,
    FlowBuilderAssistResponse,
    FlowBuilderCompileRequest,
    FlowBuilderCompileResponse,
    FlowBuilderDraftCreateRequest,
    FlowBuilderDraftCreateResponse,
    FlowBuilderImageExtraction,
    FlowBuilderMessageCreateRequest,
    FlowBuilderPlannerOutcome,
    FlowBuilderPlannerQuestion,
    FlowBuilderSession,
    FlowBuilderSessionCreateRequest,
)
from app.services.flow_builder_compiler import (
    build_flow_builder_preview,
    compile_orchestration_flow,
)
from app.services.flow_builder_image import (
    FlowBuilderImageError,
    extraction_as_planner_text,
    extract_flow_builder_image,
    validate_flow_builder_image,
)
from app.services.flow_builder_planner import (
    FlowBuilderPlannerError,
    plan_flow_builder_turn,
)
from app.services.flow_builder_target_client import (
    FlowBuilderTargetError,
    create_orchestration_draft,
    fetch_orchestration_catalog,
)
from app.services.workspace_service import bind_workspace_context, ensure_active_workspace


router = APIRouter(prefix="/v1/orch", tags=["orch-flow-builder"])
logger = get_logger(__name__)


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
    elif error.status_code in {400, 409, 422}:
        http_status = error.status_code
    else:
        http_status = status.HTTP_502_BAD_GATEWAY
    raise HTTPException(status_code=http_status, detail=error.message) from error


def _raise_planner_error(error: FlowBuilderPlannerError) -> None:
    if error.code == "planner_unavailable":
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        http_status = status.HTTP_502_BAD_GATEWAY
    raise HTTPException(status_code=http_status, detail=error.message) from error


def _raise_image_error(error: FlowBuilderImageError) -> None:
    if error.code in {"image_extraction_unavailable", "image_transport_unavailable"}:
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    elif error.code.startswith("image_extraction_"):
        http_status = status.HTTP_502_BAD_GATEWAY
    else:
        http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
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
    "/{workspace_uuid}/flow-builder/sessions/{session_id}/assist",
    response_model=FlowBuilderAssistResponse,
    dependencies=[Depends(require_flow_builder_client)],
)
async def assist_builder_session(
    workspace_uuid: UUID,
    session_id: UUID,
    payload: FlowBuilderAssistRequest,
    actor: str = Header(..., alias="X-Orch-Flow-Builder-Actor", min_length=1, max_length=255),
    db_session: AsyncSession = Depends(get_db_session),
) -> FlowBuilderAssistResponse:
    safe_actor = _normalize_actor(actor)
    safe_workspace_uuid = await _bind_builder_workspace(
        db_session,
        workspace_uuid=workspace_uuid,
    )
    try:
        current = await fetch_flow_builder_session(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            session_id=str(session_id),
        )
    except Exception as error:
        _raise_repository_error(error)
        raise AssertionError("unreachable")
    if int(current["version"]) != payload.expected_version:
        _raise_repository_error(FlowBuilderVersionConflictError(str(session_id)))
    if current.get("status") == "saved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="O rascunho desta sessão já foi criado e não pode ser replanejado.",
        )

    workspace_api_key = await fetch_workspace_otima_billing_api_key(
        db_session,
        workspace_uuid=safe_workspace_uuid,
    )
    await db_session.commit()
    settings = get_settings()
    image_extraction: FlowBuilderImageExtraction | None = None
    user_attachment_metadata: dict = {}
    user_structured_payload: dict = {}
    planner_content = payload.content.strip()
    user_message_content = planner_content
    try:
        if payload.image is not None:
            validated_image = validate_flow_builder_image(payload.image)
            payload.image.data_base64 = ""
            user_attachment_metadata = validated_image.metadata
            image_extraction = await extract_flow_builder_image(
                image=validated_image,
                instruction=payload.content,
                model=settings.orch_flow_builder_llm_model,
                workspace_uuid=safe_workspace_uuid,
                workspace_api_key=workspace_api_key,
                timeout_seconds=settings.orch_flow_builder_llm_timeout_seconds,
            )
            planner_content = extraction_as_planner_text(
                image_extraction,
                instruction=payload.content,
                filename=validated_image.filename,
            )
            user_message_content = "\n".join(
                value
                for value in (
                    payload.content.strip(),
                    f"Diagrama anexado: {validated_image.filename}",
                )
                if value
            )
            user_structured_payload = {
                "image_extraction": image_extraction.model_dump(mode="json")
            }
            del validated_image

        if image_extraction is not None and image_extraction.ambiguities:
            outcome = FlowBuilderPlannerOutcome(
                status="needs_input",
                assistant_message=(
                    "Analisei o diagrama, mas preciso confirmar os trechos ambíguos "
                    "antes de montar a prévia."
                ),
                questions=[
                    FlowBuilderPlannerQuestion(
                        key=f"image_ambiguity_{index + 1}",
                        question=ambiguity,
                        parameter_path=f"image.ambiguities.{index}",
                    )
                    for index, ambiguity in enumerate(image_extraction.ambiguities[:5])
                ],
                assumptions=image_extraction.assumptions,
                plan=None,
            )
            catalog: list[dict] = []
        else:
            catalog = await fetch_orchestration_catalog(
                workspace_uuid=safe_workspace_uuid,
                actor=safe_actor,
            )
            outcome = await plan_flow_builder_turn(
                session_id=session_id,
                messages=current.get("messages") or [],
                new_content=planner_content,
                current_plan=current.get("plan") or {},
                component_catalog=catalog,
                model=settings.orch_flow_builder_llm_model,
                workspace_uuid=safe_workspace_uuid,
                workspace_api_key=workspace_api_key,
                timeout_seconds=settings.orch_flow_builder_llm_timeout_seconds,
            )
    except FlowBuilderTargetError as error:
        logger.warning(
            "Flow Builder não conseguiu carregar o catálogo do Target Core",
            extra={
                "event": "flow_builder_catalog_failed",
                "session_id": str(session_id),
                "status_code": error.status_code,
            },
        )
        _raise_target_error(error)
        raise AssertionError("unreachable")
    except FlowBuilderPlannerError as error:
        logger.warning(
            "Flow Builder recebeu falha segura do planejador: %s",
            error.code,
            extra={
                "event": "flow_builder_planner_failed",
                "session_id": str(session_id),
            },
        )
        _raise_planner_error(error)
        raise AssertionError("unreachable")
    except FlowBuilderImageError as error:
        logger.warning(
            "Flow Builder recusou imagem: %s",
            error.code,
            extra={
                "event": "flow_builder_image_rejected",
                "session_id": str(session_id),
            },
        )
        _raise_image_error(error)
        raise AssertionError("unreachable")

    compilation = None
    preview = None
    plan_payload = current.get("plan") or {}
    issues: list[dict] = []
    if outcome.plan is not None:
        compilation = compile_orchestration_flow(outcome.plan, component_catalog=catalog)
        preview = build_flow_builder_preview(outcome.plan, compilation=compilation)
        plan_payload = outcome.plan.model_dump(mode="json")
        issues = [issue.model_dump(mode="json") for issue in compilation.issues]
        if not compilation.valid:
            error_issues = [issue for issue in compilation.issues if issue.severity == "error"]
            outcome = FlowBuilderPlannerOutcome(
                status="needs_input",
                assistant_message=(
                    "Encontrei pendências de configuração antes de liberar a prévia. "
                    "Revise os pontos indicados."
                ),
                questions=[
                    FlowBuilderPlannerQuestion(
                        key=f"validation_{index + 1}",
                        question=issue.message,
                        parameter_path=issue.path,
                    )
                    for index, issue in enumerate(error_issues[:10])
                ],
                assumptions=outcome.assumptions,
                plan=outcome.plan,
            )

    compiled_definition = (
        compilation.definition
        if outcome.status == "preview_ready" and compilation is not None and compilation.valid
        else None
    )
    assistant_payload = outcome.model_dump(mode="json", exclude={"plan"})
    try:
        await apply_flow_builder_assistant_turn(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            session_id=str(session_id),
            expected_version=payload.expected_version,
            user_content=user_message_content,
            assistant_content=outcome.assistant_message,
            assistant_payload={"planner_outcome": assistant_payload},
            plan=plan_payload,
            compiled_definition=compiled_definition,
            issues=issues,
            user_attachment_metadata=user_attachment_metadata,
            user_structured_payload=user_structured_payload,
        )
        record = await fetch_flow_builder_session(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            session_id=str(session_id),
        )
    except Exception as error:
        _raise_repository_error(error)
        raise AssertionError("unreachable")
    logger.info(
        "Turno do Flow Builder persistido com status %s",
        outcome.status,
        extra={
            "event": "flow_builder_assist_completed",
            "session_id": str(session_id),
        },
    )
    return FlowBuilderAssistResponse(
        session=FlowBuilderSession.model_validate(record),
        outcome=outcome,
        compilation=compilation,
        preview=preview,
        image_extraction=image_extraction,
    )


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


@router.post(
    "/{workspace_uuid}/flow-builder/sessions/{session_id}/draft",
    status_code=status.HTTP_201_CREATED,
    response_model=FlowBuilderDraftCreateResponse,
    dependencies=[Depends(require_flow_builder_client)],
)
async def create_builder_draft(
    workspace_uuid: UUID,
    session_id: UUID,
    payload: FlowBuilderDraftCreateRequest,
    actor: str = Header(..., alias="X-Orch-Flow-Builder-Actor", min_length=1, max_length=255),
    db_session: AsyncSession = Depends(get_db_session),
) -> FlowBuilderDraftCreateResponse:
    safe_actor = _normalize_actor(actor)
    safe_workspace_uuid = await _bind_builder_workspace(
        db_session,
        workspace_uuid=workspace_uuid,
    )
    try:
        locked = await lock_flow_builder_session_for_draft(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            session_id=str(session_id),
        )
    except Exception as error:
        _raise_repository_error(error)
        raise AssertionError("unreachable")

    existing_flow_uuid = locked.get("flow_uuid")
    existing_checksum = str(locked.get("draft_checksum") or "").strip()
    if existing_flow_uuid is not None and existing_checksum:
        record = await fetch_flow_builder_session(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            session_id=str(session_id),
        )
        response = FlowBuilderDraftCreateResponse(
            session=FlowBuilderSession.model_validate(record),
            flow_uuid=existing_flow_uuid,
            draft_checksum=existing_checksum,
            already_created=True,
        )
        logger.info(
            "Rascunho idempotente do Flow Builder já existia",
            extra={
                "event": "flow_builder_draft_reused",
                "session_id": str(session_id),
                "flow_uuid": str(existing_flow_uuid),
            },
        )
        return response
    if int(locked["version"]) != payload.expected_version:
        _raise_repository_error(FlowBuilderVersionConflictError(str(session_id)))
    definition = locked.get("compiled_definition")
    if locked.get("status") != "ready" or not isinstance(definition, dict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A conversa ainda possui pendências; gere uma prévia válida antes do rascunho.",
        )

    try:
        target_draft = await create_orchestration_draft(
            workspace_uuid=safe_workspace_uuid,
            actor=safe_actor,
            builder_session_id=str(session_id),
            definition=definition,
        )
        await store_flow_builder_draft_result(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            session_id=str(session_id),
            expected_version=payload.expected_version,
            flow_uuid=str(target_draft.flow_uuid),
            draft_checksum=target_draft.draft_checksum,
        )
        record = await fetch_flow_builder_session(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            session_id=str(session_id),
        )
    except FlowBuilderTargetError as error:
        logger.warning(
            "Flow Builder não conseguiu criar rascunho no Target Core",
            extra={
                "event": "flow_builder_draft_failed",
                "session_id": str(session_id),
                "status_code": error.status_code,
            },
        )
        _raise_target_error(error)
        raise AssertionError("unreachable")
    except Exception as error:
        _raise_repository_error(error)
        raise AssertionError("unreachable")
    response = FlowBuilderDraftCreateResponse(
        session=FlowBuilderSession.model_validate(record),
        flow_uuid=target_draft.flow_uuid,
        draft_checksum=target_draft.draft_checksum,
        already_created=False,
    )
    logger.info(
        "Rascunho do Flow Builder criado no Target Core",
        extra={
            "event": "flow_builder_draft_created",
            "session_id": str(session_id),
            "flow_uuid": str(target_draft.flow_uuid),
        },
    )
    return response
