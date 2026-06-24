from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.request_context import get_request_id
from app.core.workspace import get_current_workspace_schema
from app.repositories.flow_v2_repository import fetch_flow_row, fetch_selected_revision
from app.repositories.orch_sessions_repository import (
    WHATSAPP_STATUS_COLUMNS,
    fetch_latest_session_by_flow_entity_address,
    persist_callback_event_for_active_entity,
    persist_run_flow_event_for_active_entity_address,
    persist_run_flow_event_for_recent_entity_address,
)
from app.schemas.orch import OrchTriggerAccepted
from app.services.alarm_service import persist_alarm
from app.services.channel_event_service import persist_channel_events
from app.services.discarded_event_service import persist_discarded_event
from app.services.session_extractor import extract_session_fields
from app.services.session_service import SessionPersistResponse, persist_session
from app.services.workflow_engine import component_kind
from app.services.workflow_m2_service import WorkflowExecutionError, execute_workflow_m2_for_session
from app.services.workflow_runtime_service import WorkflowBootstrapError, bootstrap_workflow_for_session
from app.tasks.workflow_tasks import advance_session_task

logger = get_logger(__name__)
_CELERY_ENQUEUE_TIMEOUT_SECONDS = 3.0


def _is_dialer_hangup_event(payload: dict[str, Any]) -> bool:
    hangup = payload.get("hangup")
    return isinstance(hangup, dict) and str(hangup.get("Event", "")).strip().lower() == "hangup"


def _extract_whatsapp_status_name(payload: dict[str, Any]) -> str | None:
    if payload.get("object") != "whatsapp_business_account":
        return None
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            statuses = value.get("statuses")
            if not isinstance(statuses, list):
                continue
            for item in statuses:
                if not isinstance(item, dict):
                    continue
                raw = item.get("status")
                if raw is None:
                    continue
                status = str(raw).strip().lower()
                if status:
                    return status
    return None


async def _resolve_single_send_with_dialer_ref(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
) -> str | None:
    flow_row = await fetch_flow_row(db_session, flow_uuid=flow_uuid)
    if flow_row is None:
        return None

    selected_revision = await fetch_selected_revision(db_session, flow_id=str(flow_row["id"]))
    if selected_revision is None:
        return None

    definition = selected_revision.get("definition")
    if not isinstance(definition, dict):
        return None

    components = definition.get("components")
    if not isinstance(components, list):
        return None

    send_card_refs: list[str] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        if component_kind(component) != "send_with_dialer":
            continue
        ref_id = str(component.get("ref_id") or component.get("uuid") or component.get("id") or "").strip()
        if ref_id and ref_id not in send_card_refs:
            send_card_refs.append(ref_id)

    if len(send_card_refs) != 1:
        return None
    return send_card_refs[0]


async def _should_discard_whatsapp_event(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    extracted: Any,
    payload: dict[str, Any],
) -> tuple[bool, str | None]:
    status_name = _extract_whatsapp_status_name(payload)
    status_column = WHATSAPP_STATUS_COLUMNS.get(status_name or "")
    if status_column is None:
        return False, None

    safe_schema = get_current_workspace_schema().replace('"', '""')
    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        latest = await fetch_latest_session_by_flow_entity_address(
            db_session,
            flow_uuid=flow_uuid,
            entity_type=str(extracted.entity_type),
            entity_address=str(extracted.entity_address),
        )
    if latest is None:
        return False, None

    if latest.get(status_column) is not None:
        return True, "whatsapp_status_already_processed"
    return False, None


def m2_alarm_from_stopped_reason(stopped_reason: str) -> tuple[str, str, str] | None:
    if stopped_reason.startswith("component_not_supported"):
        return (
            "warning",
            "workflow_m2_component_not_supported",
            "Componente encontrado fora do escopo implementado do M2.",
        )
    if stopped_reason == "component_not_found":
        return (
            "error",
            "workflow_m2_component_not_found",
            "Componente de fluxo não encontrado na definição durante execução M2.",
        )
    if stopped_reason == "max_steps_reached":
        return (
            "warning",
            "workflow_m2_max_steps_reached",
            "Execução M2 interrompida por limite de passos.",
        )
    if stopped_reason == "loop_guard_repeat_limit":
        return (
            "error",
            "workflow_m2_loop_guard_repeat_limit",
            "Execução M2 interrompida por proteção de loop infinito.",
        )
    if stopped_reason == "session_execution_locked":
        return (
            "warning",
            "workflow_m2_session_execution_locked",
            "Execução M2 ignorada por lock de sessão em andamento.",
        )
    if stopped_reason == "frozen_wait_active":
        return (
            "warning",
            "workflow_m2_frozen_wait_active",
            "Execução M2 aguardando término de janela de espera (frozen_until).",
        )
    if stopped_reason in {"flow_not_found", "revision_not_found", "session_not_found", "no_next_card"}:
        return (
            "warning",
            f"workflow_m2_{stopped_reason}",
            "Execução M2 não avançou por pré-condição de fluxo/sessão.",
        )
    return None


async def process_single_payload(
    *,
    safe_workspace_uuid: str | None,
    workspace_schema: str,
    flow_uuid: str,
    payload: dict[str, Any],
    db_session: AsyncSession,
    app_name: str,
) -> OrchTriggerAccepted:
    extracted = None
    try:
        extracted = extract_session_fields(app_name, payload)
        is_callback_event = (
            app_name == "GenericApp"
            and str(payload.get("event_name", "")).strip().lower() == "callback"
        )
        is_run_flow_hangup_event = app_name == "DialerApp" and _is_dialer_hangup_event(payload)

        if is_run_flow_hangup_event:
            hangup_persisted = await persist_run_flow_event_for_active_entity_address(
                db_session,
                flow_uuid=flow_uuid,
                app_name=app_name,
                entity_address=extracted.entity_address,
                payload=payload,
                extracted=extracted.model_dump(),
                event_name="hangup",
                event_result="hangup",
                event_data={
                    "uniqueid": payload.get("uniqueid"),
                    "hangup": payload.get("hangup") if isinstance(payload.get("hangup"), dict) else {},
                    "makecall": payload.get("makecall") if isinstance(payload.get("makecall"), dict) else {},
                },
            )
            if hangup_persisted is None:
                resume_card_uuid = await _resolve_single_send_with_dialer_ref(
                    db_session,
                    flow_uuid=flow_uuid,
                )
                if resume_card_uuid is not None:
                    settings = get_settings()
                    hangup_persisted = await persist_run_flow_event_for_recent_entity_address(
                        db_session,
                        flow_uuid=flow_uuid,
                        app_name=app_name,
                        entity_address=extracted.entity_address,
                        payload=payload,
                        extracted=extracted.model_dump(),
                        event_name="hangup",
                        event_result="hangup",
                        resume_card_uuid=resume_card_uuid,
                        correlation_window_hours=settings.workflow_dialer_event_correlation_window_hours,
                        event_data={
                            "uniqueid": payload.get("uniqueid"),
                            "hangup": payload.get("hangup") if isinstance(payload.get("hangup"), dict) else {},
                            "makecall": payload.get("makecall") if isinstance(payload.get("makecall"), dict) else {},
                        },
                    )
            if hangup_persisted is None:
                await persist_discarded_event(
                    db_session,
                    flow_uuid=flow_uuid,
                    app_name=app_name,
                    entity=extracted.entity,
                    entity_type=extracted.entity_type,
                    entity_address=extracted.entity_address,
                    entity_session_id=extracted.entity_session_id,
                    discard_reason="run_flow_hangup_session_not_found_by_address",
                    payload=payload,
                )
                return OrchTriggerAccepted(
                    status="ignored",
                    accepted=False,
                    flow_uuid=flow_uuid,
                    app=app_name,
                    persistence="ignored",
                    extracted=extracted,
                    session_id=0,
                    session_uuid="",
                    session_state=0,
                    session_created=False,
                    workflow_execution={
                        "mode": "async",
                        "enqueued": False,
                        "reason": "run_flow_hangup_session_not_found_by_address",
                    },
                )
            persisted = SessionPersistResponse(
                session_id=hangup_persisted.id,
                session_uuid=hangup_persisted.uuid,
                session_state=hangup_persisted.state,
                session_created=False,
            )
        elif is_callback_event:
            callback_persisted = await persist_callback_event_for_active_entity(
                db_session,
                flow_uuid=flow_uuid,
                app_name=app_name,
                entity=extracted.entity,
                payload=payload,
                extracted=extracted.model_dump(),
            )
            if callback_persisted is None:
                await persist_discarded_event(
                    db_session,
                    flow_uuid=flow_uuid,
                    app_name=app_name,
                    entity=extracted.entity,
                    entity_type=extracted.entity_type,
                    entity_address=extracted.entity_address,
                    entity_session_id=extracted.entity_session_id,
                    discard_reason="callback_session_not_found",
                    payload=payload,
                )
                return OrchTriggerAccepted(
                    status="ignored",
                    accepted=False,
                    flow_uuid=flow_uuid,
                    app=app_name,
                    persistence="ignored",
                    extracted=extracted,
                    session_id=0,
                    session_uuid="",
                    session_state=0,
                    session_created=False,
                    workflow_execution={
                        "mode": "async",
                        "enqueued": False,
                        "reason": "callback_session_not_found",
                    },
                )
            persisted = SessionPersistResponse(
                session_id=callback_persisted.id,
                session_uuid=callback_persisted.uuid,
                session_state=callback_persisted.state,
                session_created=False,
            )
        else:
            if app_name == "WhatsApp":
                should_discard, discard_reason = await _should_discard_whatsapp_event(
                    db_session,
                    flow_uuid=flow_uuid,
                    extracted=extracted,
                    payload=payload,
                )
                if should_discard:
                    await persist_discarded_event(
                        db_session,
                        flow_uuid=flow_uuid,
                        app_name=app_name,
                        entity=extracted.entity,
                        entity_type=extracted.entity_type,
                        entity_address=extracted.entity_address,
                        entity_session_id=extracted.entity_session_id,
                        discard_reason=discard_reason or "whatsapp_discarded",
                        payload=payload,
                    )
                    return OrchTriggerAccepted(
                        status="ignored",
                        accepted=False,
                        flow_uuid=flow_uuid,
                        app=app_name,
                        persistence="ignored",
                        extracted=extracted,
                        session_id=0,
                        session_uuid="",
                        session_state=0,
                        session_created=False,
                        workflow_execution={
                            "mode": "async",
                            "enqueued": False,
                            "reason": discard_reason,
                        },
                    )
            persisted = await persist_session(
                db_session,
                flow_uuid=flow_uuid,
                app_name=app_name,
                extracted=extracted.model_dump(),
                payload=payload,
            )
    except HTTPException as exc:
        alarm_level = "warning" if exc.status_code < 500 else "error"
        await persist_alarm(
            db_session,
            level=alarm_level,
            code="trigger_orch_http_exception",
            message=str(exc.detail),
            details={"status_code": exc.status_code},
            flow_uuid=flow_uuid,
            app_name=app_name,
            entity=(extracted.entity if extracted else None),
            entity_type=(extracted.entity_type if extracted else None),
            entity_address=(extracted.entity_address if extracted else None),
        )
        raise
    except Exception as exc:
        await persist_alarm(
            db_session,
            level="error",
            code="trigger_orch_unhandled_exception",
            message="Erro inesperado no processamento do trigger.",
            details={"exception": str(type(exc).__name__)},
            flow_uuid=flow_uuid,
            app_name=app_name,
            entity=(extracted.entity if extracted else None),
            entity_type=(extracted.entity_type if extracted else None),
            entity_address=(extracted.entity_address if extracted else None),
        )
        raise

    logger.info(
        "orch trigger accepted",
        extra={
            "event": "orch.trigger.accepted",
            "request_id": get_request_id(),
            "workspace_uuid": safe_workspace_uuid,
            "workspace_schema": workspace_schema,
            "flow_uuid": flow_uuid,
            "app": app_name,
            "entity": extracted.entity,
            "session_id": persisted.session_id,
            "session_uuid": persisted.session_uuid,
        },
    )

    persisted_channel_events = await persist_channel_events(
        db_session,
        session_id=persisted.session_id,
        flow_uuid=flow_uuid,
        app_name=app_name,
        payload=payload,
    )
    if persisted_channel_events > 0:
        logger.info(
            "channel events persisted",
            extra={
                "event": "orch.channel_events.persisted",
                "workspace_uuid": safe_workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": persisted.session_id,
                "app": app_name,
                "count": persisted_channel_events,
            },
        )

    workflow_bootstrap = None
    workflow_execution = None
    try:
        workflow_result = await bootstrap_workflow_for_session(
            db_session,
            flow_uuid=flow_uuid,
            session_id=persisted.session_id,
            payload=payload,
        )
        workflow_bootstrap = {
            "enabled": workflow_result.enabled,
            "loaded": workflow_result.loaded,
            "reason": workflow_result.reason,
            "flow_id": workflow_result.flow_id,
            "revision_id": workflow_result.revision_id,
            "revision_version": workflow_result.revision_version,
            "revision_mode": workflow_result.revision_mode,
            "next_card_uuid": workflow_result.next_card_uuid,
        }
        if workflow_result.enabled and not workflow_result.loaded:
            await persist_alarm(
                db_session,
                level="warning",
                code="workflow_bootstrap_not_loaded",
                message="Bootstrap do workflow não carregado nesta requisição.",
                details=workflow_bootstrap,
                flow_uuid=flow_uuid,
                app_name=app_name,
                entity=extracted.entity,
                entity_type=extracted.entity_type,
                entity_address=extracted.entity_address,
                session_uuid=persisted.session_uuid,
            )
    except WorkflowBootstrapError as exc:
        await persist_alarm(
            db_session,
            level="error",
            code=f"workflow_bootstrap_{exc.code}",
            message=exc.message,
            details={},
            flow_uuid=flow_uuid,
            app_name=app_name,
            entity=extracted.entity,
            entity_type=extracted.entity_type,
            entity_address=extracted.entity_address,
            session_uuid=persisted.session_uuid,
        )
    except Exception:
        await persist_alarm(
            db_session,
            level="error",
            code="workflow_bootstrap_unhandled_exception",
            message="Falha inesperada no bootstrap do workflow.",
            details={},
            flow_uuid=flow_uuid,
            app_name=app_name,
            entity=extracted.entity,
            entity_type=extracted.entity_type,
            entity_address=extracted.entity_address,
            session_uuid=persisted.session_uuid,
        )

    if workflow_bootstrap and workflow_bootstrap.get("loaded"):
        settings = get_settings()
        if settings.celery_enabled:
            enqueue_workspace_uuid = safe_workspace_uuid

            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        lambda: (
                            advance_session_task.delay(
                                workspace_uuid=enqueue_workspace_uuid,
                                flow_uuid=flow_uuid,
                                session_id=persisted.session_id,
                            )
                            if enqueue_workspace_uuid is not None
                            else advance_session_task.delay(
                                flow_uuid=flow_uuid,
                                session_id=persisted.session_id,
                            )
                        )
                    ),
                    timeout=_CELERY_ENQUEUE_TIMEOUT_SECONDS,
                )
                workflow_execution = {
                    "mode": "async",
                    "enqueued": True,
                    "dispatcher": "celery",
                    "workspace_uuid": enqueue_workspace_uuid,
                }
            except TypeError:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            lambda: advance_session_task.delay(
                                flow_uuid=flow_uuid,
                                session_id=persisted.session_id,
                            )
                        ),
                        timeout=_CELERY_ENQUEUE_TIMEOUT_SECONDS,
                    )
                    workflow_execution = {
                        "mode": "async",
                        "enqueued": True,
                        "dispatcher": "celery",
                        "workspace_uuid": safe_workspace_uuid,
                    }
                except Exception as exc:
                    await persist_alarm(
                        db_session,
                        level="error",
                        code="workflow_m2_enqueue_failed",
                        message="Falha ao enfileirar execução assíncrona do workflow.",
                        details={
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                        },
                        flow_uuid=flow_uuid,
                        app_name=app_name,
                        entity=extracted.entity,
                        entity_type=extracted.entity_type,
                        entity_address=extracted.entity_address,
                        session_uuid=persisted.session_uuid,
                    )
                    workflow_execution = {
                        "mode": "async",
                        "enqueued": False,
                        "dispatcher": "celery",
                        "workspace_uuid": safe_workspace_uuid,
                    }
            except Exception as exc:
                await persist_alarm(
                    db_session,
                    level="error",
                    code="workflow_m2_enqueue_failed",
                    message="Falha ao enfileirar execução assíncrona do workflow.",
                    details={
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    },
                    flow_uuid=flow_uuid,
                    app_name=app_name,
                    entity=extracted.entity,
                    entity_type=extracted.entity_type,
                    entity_address=extracted.entity_address,
                    session_uuid=persisted.session_uuid,
                )
                workflow_execution = {
                    "mode": "async",
                    "enqueued": False,
                    "dispatcher": "celery",
                    "workspace_uuid": safe_workspace_uuid,
                }
        else:
            try:
                execution_result = await execute_workflow_m2_for_session(
                    db_session,
                    flow_uuid=flow_uuid,
                    session_id=persisted.session_id,
                )
                workflow_execution = {
                    "enabled": execution_result.enabled,
                    "executed_steps": execution_result.executed_steps,
                    "stopped_reason": execution_result.stopped_reason,
                    "last_card_uuid": execution_result.last_card_uuid,
                    "next_card_uuid": execution_result.next_card_uuid,
                    "mode": "inline_fallback",
                }
                m2_alarm = (
                    m2_alarm_from_stopped_reason(execution_result.stopped_reason)
                    if execution_result.enabled
                    else None
                )
                if m2_alarm is not None:
                    level, code, message = m2_alarm
                    await persist_alarm(
                        db_session,
                        level=level,
                        code=code,
                        message=message,
                        details=workflow_execution,
                        flow_uuid=flow_uuid,
                        app_name=app_name,
                        entity=extracted.entity,
                        entity_type=extracted.entity_type,
                        entity_address=extracted.entity_address,
                        session_uuid=persisted.session_uuid,
                    )
            except WorkflowExecutionError as exc:
                await persist_alarm(
                    db_session,
                    level="error",
                    code=f"workflow_m2_{exc.code}",
                    message=exc.message,
                    details={},
                    flow_uuid=flow_uuid,
                    app_name=app_name,
                    entity=extracted.entity,
                    entity_type=extracted.entity_type,
                    entity_address=extracted.entity_address,
                    session_uuid=persisted.session_uuid,
                )
            except Exception as exc:
                logger.exception(
                    "workflow m2 unhandled exception",
                    extra={
                        "event": "orch.workflow.m2.unhandled_exception",
                        "request_id": get_request_id(),
                        "flow_uuid": flow_uuid,
                        "session_id": persisted.session_id,
                        "session_uuid": persisted.session_uuid,
                        "exception_type": type(exc).__name__,
                    },
                )
                await persist_alarm(
                    db_session,
                    level="error",
                    code="workflow_m2_unhandled_exception",
                    message="Falha inesperada na execução M2 do workflow.",
                    details={
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    },
                    flow_uuid=flow_uuid,
                    app_name=app_name,
                    entity=extracted.entity,
                    entity_type=extracted.entity_type,
                    entity_address=extracted.entity_address,
                    session_uuid=persisted.session_uuid,
                )

    return OrchTriggerAccepted(
        status="accepted",
        accepted=True,
        flow_uuid=flow_uuid,
        app=app_name,
        persistence="saved",
        extracted=extracted,
        session_id=persisted.session_id,
        session_uuid=persisted.session_uuid,
        session_state=persisted.session_state,
        session_created=persisted.session_created,
        workflow_bootstrap=workflow_bootstrap,
        workflow_execution=workflow_execution,
    )
