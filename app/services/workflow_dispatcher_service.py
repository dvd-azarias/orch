from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.workspace import get_current_workspace_schema
from app.repositories.orch_sessions_repository import (
    claim_pending_sessions_for_dispatch,
    mark_session_finished,
    set_session_state,
)
from app.services.workflow_m2_service import execute_workflow_m2_for_session
from app.services.workflow_runtime_service import bootstrap_workflow_for_session

logger = get_logger(__name__)
RESUMABLE_STOP_REASONS = {
    "scheduled_wait",
    "frozen_wait_active",
    "max_steps_reached",
}
BLOCKING_RUNNING_STOP_REASONS = {
    "blocked_send_with_whatsapp",
    "blocked_process_whatsapp_response",
    "blocked_send_with_dialer",
    "blocked_process_dialer_response",
    "blocked_run_flow",
}
FINAL_STOP_REASONS = {
    "finished_by_component",
    "end_of_branch",
    "no_next_card",
}
FATAL_NON_RESUMABLE_STOP_REASONS = {
    "flow_not_found",
    "revision_not_found",
    "session_not_found",
    "component_not_found",
    "loop_guard_repeat_limit",
}


async def claim_pending_sessions(
    db_session: AsyncSession,
) -> list[dict[str, str | int]]:
    settings = get_settings()
    safe_schema = get_current_workspace_schema().replace('"', '""')
    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        return await claim_pending_sessions_for_dispatch(
            db_session,
            limit=settings.celery_dispatch_batch_size,
        )


async def dispatch_pending_sessions(
    db_session: AsyncSession,
) -> list[dict[str, str | int]]:
    claimed = await claim_pending_sessions(db_session)
    logger.info(
        "workflow dispatcher claimed sessions",
        extra={
            "event": "orch.workflow.dispatcher.claimed",
            "claimed_count": len(claimed),
        },
    )
    return claimed


async def advance_session_once(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    session_id: int,
) -> str:
    settings = get_settings()
    safe_schema = get_current_workspace_schema().replace('"', '""')
    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        await bootstrap_workflow_for_session(
            db_session,
            flow_uuid=flow_uuid,
            session_id=session_id,
            payload={},
        )
        result = await execute_workflow_m2_for_session(
            db_session,
            flow_uuid=flow_uuid,
            session_id=session_id,
        )

        stopped_reason = result.stopped_reason
        if stopped_reason in FINAL_STOP_REASONS:
            if stopped_reason in {"end_of_branch", "no_next_card"}:
                await mark_session_finished(
                    db_session,
                    session_id=session_id,
                )
            return stopped_reason

        if stopped_reason in RESUMABLE_STOP_REASONS:
            await set_session_state(
                db_session,
                session_id=session_id,
                state=0,
                only_if_not_finished=True,
            )
            return stopped_reason

        if stopped_reason in BLOCKING_RUNNING_STOP_REASONS:
            await set_session_state(
                db_session,
                session_id=session_id,
                state=1,
                only_if_not_finished=True,
            )
            return stopped_reason

        if stopped_reason == "session_execution_locked":
            return stopped_reason

        logger.warning(
            "workflow session stopped in non-resumable reason",
            extra={
                "event": "orch.workflow.session.non_resumable_stop",
                "flow_uuid": flow_uuid,
                "session_id": session_id,
                "stopped_reason": stopped_reason,
            },
        )
        if stopped_reason in FATAL_NON_RESUMABLE_STOP_REASONS or stopped_reason.startswith("component_not_supported"):
            await mark_session_finished(
                db_session,
                session_id=session_id,
            )
            return stopped_reason
        return result.stopped_reason
