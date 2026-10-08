from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workspace import get_current_workspace_schema
from app.repositories.orch_sessions_repository import (
    persist_callback_event_for_exact_session,
)


@dataclass(frozen=True)
class LiveTabulationResult:
    status: str
    accepted: bool
    idempotent: bool
    idempotency_key: str
    orch_session_id: int | None = None
    orch_session_uuid: str | None = None
    orch_flow_uuid: str | None = None
    resume_required: bool = False


def build_live_tabulation_payloads(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove Live lifecycle authority and build the ORCH callback data."""

    request_payload = dict(payload)
    request_payload.pop("ends_session", None)
    request_payload.pop("source", None)

    callback_payload = dict(request_payload)
    callback_payload["source"] = "atendimento_live_direct"
    disposition_code = str(callback_payload.get("disposition_code") or "").strip()
    if disposition_code:
        callback_payload["outcome"] = disposition_code.lower()
    return request_payload, callback_payload


async def _set_workspace_search_path(db_session: AsyncSession) -> None:
    safe_schema = get_current_workspace_schema().replace('"', '""')
    await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))


async def register_live_tabulation(
    db_session: AsyncSession,
    *,
    session_uuid: str,
    flow_uuid: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
    callback_payload: dict[str, Any],
) -> LiveTabulationResult:
    """Persist one direct Live tabulation for one exact ORCH session."""

    await _set_workspace_search_path(db_session)
    await db_session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"live_orch_tabulation_session|{session_uuid}"},
    )
    session_row = (
        await db_session.execute(
            text(
                """
                SELECT
                    id,
                    uuid::text AS uuid,
                    flow_uuid::text AS flow_uuid
                FROM orch_sessions
                WHERE uuid = CAST(:session_uuid AS uuid)
                  AND flow_uuid = CAST(:flow_uuid AS uuid)
                LIMIT 1
                """
            ),
            {
                "session_uuid": session_uuid,
                "flow_uuid": flow_uuid,
            },
        )
    ).mappings().first()
    if session_row is None:
        return LiveTabulationResult(
            status="not_found",
            accepted=False,
            idempotent=False,
            idempotency_key=idempotency_key,
            orch_session_uuid=session_uuid,
            orch_flow_uuid=flow_uuid,
        )

    orch_session_id = int(session_row["id"])
    existing = (
        await db_session.execute(
            text(
                """
                SELECT
                    id,
                    idempotency_key,
                    orch_session_id,
                    orch_session_uuid::text AS orch_session_uuid,
                    flow_uuid::text AS flow_uuid,
                    request_payload,
                    status,
                    resume_required
                FROM orch_live_tabulation_events
                WHERE orch_session_uuid = CAST(:session_uuid AS uuid)
                  AND idempotency_key = :idempotency_key
                LIMIT 1
                """
            ),
            {
                "session_uuid": session_uuid,
                "idempotency_key": idempotency_key,
            },
        )
    ).mappings().first()
    if existing is not None:
        same_event = bool(
            str(existing["flow_uuid"]) == flow_uuid
            and existing.get("request_payload") == request_payload
        )
        existing_status = str(existing["status"])
        return LiveTabulationResult(
            status=existing_status if same_event else "conflict",
            accepted=same_event and existing_status == "applied",
            idempotent=same_event,
            idempotency_key=idempotency_key,
            orch_session_id=orch_session_id,
            orch_session_uuid=session_uuid,
            orch_flow_uuid=flow_uuid,
            resume_required=(
                bool(existing.get("resume_required")) if same_event else False
            ),
        )

    persisted = await persist_callback_event_for_exact_session(
        db_session,
        session_id=orch_session_id,
        app_name="AtendimentoLiveDirect",
        event_name="callback",
        event_result="tabulation",
        event_data=callback_payload,
    )
    event_status = "applied" if persisted is not None else "ignored"
    resume_required = bool(persisted.resume_required) if persisted is not None else False
    await db_session.execute(
        text(
            """
            INSERT INTO orch_live_tabulation_events (
                idempotency_key,
                orch_session_id,
                orch_session_uuid,
                flow_uuid,
                request_payload,
                callback_payload,
                status,
                resume_required,
                applied_at,
                last_error,
                created_at,
                updated_at
            ) VALUES (
                :idempotency_key,
                :orch_session_id,
                CAST(:session_uuid AS uuid),
                CAST(:flow_uuid AS uuid),
                CAST(:request_payload AS jsonb),
                CAST(:callback_payload AS jsonb),
                :status,
                :resume_required,
                CASE WHEN :status = 'applied' THEN NOW() ELSE NULL END,
                CASE WHEN :status = 'ignored' THEN 'orch_session_inactive' ELSE NULL END,
                NOW(),
                NOW()
            )
            """
        ),
        {
            "idempotency_key": idempotency_key,
            "orch_session_id": orch_session_id,
            "session_uuid": session_uuid,
            "flow_uuid": flow_uuid,
            "request_payload": json.dumps(request_payload, ensure_ascii=False),
            "callback_payload": json.dumps(callback_payload, ensure_ascii=False),
            "status": event_status,
            "resume_required": resume_required,
        },
    )
    return LiveTabulationResult(
        status=event_status,
        accepted=persisted is not None,
        idempotent=False,
        idempotency_key=idempotency_key,
        orch_session_id=orch_session_id,
        orch_session_uuid=session_uuid,
        orch_flow_uuid=flow_uuid,
        resume_required=resume_required,
    )
