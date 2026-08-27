from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.repositories.orch_channel_events_repository import (
    fetch_next_pending_channel_event,
    has_pending_channel_events,
    mark_channel_event_processed,
    mark_whatsapp_messages_processed_by_ids,
)
from app.repositories.orch_sessions_repository import (
    fetch_session_workflow_state,
    replace_session_workflow_state,
)
from app.services.switch_bot_flow_service import (
    SwitchBotFlowError,
    clear_runner_token_cache,
    deliver_meta_payload,
    extract_meta_message_ids,
    is_meta_user_message_payload,
    resolve_runner_token,
)
from app.services.workspace_service import bind_workspace_context


logger = get_logger(__name__)
# Share the workflow lock namespace so callback, relay delivery and the M2
# executor cannot replace the same runtime_variables snapshot concurrently.
_SWITCH_BOT_FLOW_LOCK_CLASS_ID = 92021


@celery_app.task(name="app.tasks.switch_bot_flow.process_handoff", ignore_result=True)
def process_switch_bot_flow_task(*, workspace_uuid: str, flow_uuid: str, session_id: int) -> None:
    asyncio.run(
        _process_switch_bot_flow_task(
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
            session_id=session_id,
        )
    )


def _workflow_switch_state(runtime_variables: dict[str, Any]) -> dict[str, Any] | None:
    workflow_meta = runtime_variables.get("workflow_v2")
    if not isinstance(workflow_meta, dict):
        return None
    state = workflow_meta.get("switch_bot_flow")
    return state if isinstance(state, dict) else None


def _deliver(
    *,
    workspace_uuid: str,
    target_flow_uuid: str,
    payload: dict[str, Any],
) -> Any:
    runner_token = resolve_runner_token(
        workspace_uuid=workspace_uuid,
        target_flow_uuid=target_flow_uuid,
    )
    try:
        return deliver_meta_payload(runner_token=runner_token, payload=payload)
    except SwitchBotFlowError as exc:
        if exc.status_code not in {401, 403}:
            raise
        clear_runner_token_cache(
            workspace_uuid=workspace_uuid,
            target_flow_uuid=target_flow_uuid,
        )
        refreshed_runner_token = resolve_runner_token(
            workspace_uuid=workspace_uuid,
            target_flow_uuid=target_flow_uuid,
        )
        return deliver_meta_payload(runner_token=refreshed_runner_token, payload=payload)


async def _next_user_message_event(
    db_session: Any,
    *,
    session_id: int,
) -> dict[str, Any] | None:
    for _ in range(100):
        event = await fetch_next_pending_channel_event(
            db_session,
            session_id=session_id,
            channel="whatsapp",
        )
        if event is None:
            return None
        event_type = str(event.get("event_type") or "").strip().lower()
        payload = event.get("payload")
        if event_type.startswith("message") and is_meta_user_message_payload(payload):
            return event
        await mark_channel_event_processed(
            db_session,
            event_row_id=int(event["id"]),
            session_id=session_id,
            channel="whatsapp",
            discard_reason="switch_bot_flow_non_user_event",
        )
    return None


async def _process_switch_bot_flow_task(*, workspace_uuid: str, flow_uuid: str, session_id: int) -> None:
    _safe_workspace_uuid, workspace_schema = bind_workspace_context(workspace_uuid)
    safe_schema = workspace_schema.replace('"', '""')
    session_factory = get_session_factory()
    should_advance_workflow = False
    should_requeue_handoff = False
    outcome = "ignored"

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            lock_result = await db_session.execute(
                text("SELECT pg_try_advisory_xact_lock(:class_id, :object_id) AS locked"),
                {"class_id": _SWITCH_BOT_FLOW_LOCK_CLASS_ID, "object_id": int(session_id)},
            )
            if not bool(lock_result.scalar_one()):
                return

            session_state = await fetch_session_workflow_state(db_session, session_id=session_id)
            if session_state is None or str(session_state.get("flow_uuid") or "") != str(flow_uuid):
                return
            runtime_variables = session_state.get("runtime_variables")
            if not isinstance(runtime_variables, dict):
                return
            handoff = _workflow_switch_state(runtime_variables)
            if not isinstance(handoff, dict):
                return

            handoff_status = str(handoff.get("status") or "").strip().lower()
            if handoff_status in {"completed", "failed"}:
                return

            payload: dict[str, Any] | None = None
            event_row_id: int | None = None
            if handoff_status == "pending_delivery":
                pending_payload = handoff.get("pending_payload")
                if isinstance(pending_payload, dict) and is_meta_user_message_payload(pending_payload):
                    payload = pending_payload

            if payload is None:
                pending_event = await _next_user_message_event(db_session, session_id=session_id)
                if isinstance(pending_event, dict):
                    pending_payload = pending_event.get("payload")
                    if isinstance(pending_payload, dict):
                        payload = pending_payload
                        event_row_id = int(pending_event["id"])

            if payload is None:
                handoff["status"] = "waiting_message" if not handoff.get("target_session_id") else "active"
                await replace_session_workflow_state(
                    db_session,
                    session_id=session_id,
                    runtime_variables=runtime_variables,
                    last_card_uuid=session_state.get("last_card_uuid"),
                    next_card_uuid=session_state.get("next_card_uuid"),
                )
                return

            target_flow_uuid = str(handoff.get("target_flow_uuid") or "").strip()
            if not target_flow_uuid:
                handoff["status"] = "failed"
                handoff["last_error"] = {
                    "code": "switch_bot_flow_missing_target_flow",
                    "message": "Handoff sem flow de destino persistido.",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                should_advance_workflow = True
                outcome = "failed"
            else:
                handoff["status"] = "opening" if not handoff.get("target_session_id") else "active"
                try:
                    delivery_result = await asyncio.to_thread(
                        _deliver,
                        workspace_uuid=workspace_uuid,
                        target_flow_uuid=target_flow_uuid,
                        payload=payload,
                    )
                    delivered_session_id = delivery_result.session_id or str(
                        handoff.get("target_session_id") or ""
                    ).strip()
                    if not delivered_session_id:
                        raise SwitchBotFlowError(
                            "switch_bot_flow_runner_session_id_missing",
                            "Runner v5 não devolveu session_id na abertura do handoff.",
                            status_code=delivery_result.status_code,
                        )
                    previous_session_id = str(handoff.get("target_session_id") or "").strip()
                    if previous_session_id and previous_session_id != delivered_session_id:
                        raise SwitchBotFlowError(
                            "switch_bot_flow_runner_session_mismatch",
                            "Runner v5 devolveu session_id diferente para o handoff ativo.",
                            status_code=delivery_result.status_code,
                        )

                    message_ids = extract_meta_message_ids(payload)
                    handoff.update(
                        {
                            "status": "active",
                            "target_session_id": delivered_session_id,
                            "target_execution_kind": delivery_result.response_body.get("execution_kind"),
                            "target_revision_id": delivery_result.response_body.get("revision_id"),
                            "target_revision_version": delivery_result.response_body.get("revision_version"),
                            "last_forwarded_event_id": message_ids[-1] if message_ids else None,
                            "last_delivery_status_code": delivery_result.status_code,
                            "last_delivery_attempts": delivery_result.attempts,
                            "last_forwarded_at": datetime.now(timezone.utc).isoformat(),
                            "last_error": None,
                        }
                    )
                    handoff.pop("pending_payload", None)
                    handoff.pop("pending_message_ids", None)
                    if event_row_id is not None:
                        await mark_channel_event_processed(
                            db_session,
                            event_row_id=event_row_id,
                            session_id=session_id,
                            channel="whatsapp",
                        )
                    await mark_whatsapp_messages_processed_by_ids(
                        db_session,
                        session_id=session_id,
                        message_ids=message_ids,
                    )
                    should_requeue_handoff = await has_pending_channel_events(
                        db_session,
                        session_id=session_id,
                        channel="whatsapp",
                    )
                    outcome = "delivered"
                except SwitchBotFlowError as exc:
                    handoff["status"] = "failed"
                    handoff["completed_at"] = datetime.now(timezone.utc).isoformat()
                    handoff["last_error"] = {
                        "code": exc.code,
                        "message": exc.message,
                        "status_code": exc.status_code,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    handoff.pop("pending_payload", None)
                    handoff.pop("pending_message_ids", None)
                    if event_row_id is not None:
                        await mark_channel_event_processed(
                            db_session,
                            event_row_id=event_row_id,
                            session_id=session_id,
                            channel="whatsapp",
                            discard_reason="switch_bot_flow_delivery_failed",
                        )
                    should_advance_workflow = True
                    outcome = "failed"

            await replace_session_workflow_state(
                db_session,
                session_id=session_id,
                runtime_variables=runtime_variables,
                last_card_uuid=session_state.get("last_card_uuid"),
                next_card_uuid=session_state.get("next_card_uuid"),
            )

    settings = get_settings()
    if should_advance_workflow:
        from app.tasks.workflow_tasks import advance_session_task

        advance_session_task.apply_async(
            kwargs={
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
            },
            queue=settings.celery_execute_queue,
            routing_key=settings.celery_execute_queue,
        )
    elif should_requeue_handoff:
        process_switch_bot_flow_task.apply_async(
            kwargs={
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": session_id,
            },
            queue=settings.celery_switch_bot_flow_queue,
            routing_key=settings.celery_switch_bot_flow_queue,
        )

    logger.info(
        "switch_bot_flow handoff processed",
        extra={
            "event": "orch.switch_bot_flow.processed",
            "workspace_uuid": workspace_uuid,
            "flow_uuid": flow_uuid,
            "session_id": session_id,
            "outcome": outcome,
        },
    )
