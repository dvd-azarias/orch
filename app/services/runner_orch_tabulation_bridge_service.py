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
class RunnerBridgeBindResult:
    status: str
    accepted: bool
    idempotent: bool
    runner_session_id: str
    orch_session_id: int | None = None
    orch_session_uuid: str | None = None
    orch_flow_uuid: str | None = None
    resume_session_id: int | None = None


@dataclass(frozen=True)
class RunnerBridgeTabulationResult:
    status: str
    accepted: bool
    idempotent: bool
    runner_session_id: str
    event_key: str
    orch_session_id: int | None = None
    orch_session_uuid: str | None = None
    orch_flow_uuid: str | None = None
    resume_required: bool = False


async def _set_workspace_search_path(db_session: AsyncSession) -> None:
    safe_schema = get_current_workspace_schema().replace('"', '""')
    await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))


async def _apply_event_row(
    db_session: AsyncSession,
    *,
    event_row: dict[str, Any],
    link_row: dict[str, Any],
) -> RunnerBridgeTabulationResult:
    event_id = int(event_row["id"])
    runner_session_id = str(event_row["runner_session_id"])
    event_key = str(event_row["event_key"])
    event_runner_flow_uuid = str(event_row["runner_flow_uuid"])
    link_runner_flow_uuid = str(link_row["runner_flow_uuid"])
    orch_session_id = int(link_row["orch_session_id"])

    if event_runner_flow_uuid != link_runner_flow_uuid:
        await db_session.execute(
            text(
                """
                UPDATE orch_runner_tabulation_events
                SET
                    status = 'conflict',
                    orch_session_id = :orch_session_id,
                    last_error = 'runner_flow_uuid_mismatch',
                    updated_at = NOW()
                WHERE id = :event_id
                  AND status = 'pending_link'
                """
            ),
            {"event_id": event_id, "orch_session_id": orch_session_id},
        )
        return RunnerBridgeTabulationResult(
            status="conflict",
            accepted=False,
            idempotent=False,
            runner_session_id=runner_session_id,
            event_key=event_key,
            orch_session_id=orch_session_id,
            orch_session_uuid=str(link_row["orch_session_uuid"]),
            orch_flow_uuid=str(link_row["orch_flow_uuid"]),
        )

    callback_payload = event_row.get("callback_payload")
    if not isinstance(callback_payload, dict):
        callback_payload = {}
    persisted = await persist_callback_event_for_exact_session(
        db_session,
        session_id=orch_session_id,
        app_name="RunnerV5LiveBridge",
        event_name="callback",
        event_result="tabulation",
        event_data=callback_payload,
    )
    if persisted is None:
        await db_session.execute(
            text(
                """
                UPDATE orch_runner_tabulation_events
                SET
                    status = 'ignored',
                    orch_session_id = :orch_session_id,
                    last_error = 'orch_session_inactive',
                    updated_at = NOW()
                WHERE id = :event_id
                  AND status = 'pending_link'
                """
            ),
            {"event_id": event_id, "orch_session_id": orch_session_id},
        )
        return RunnerBridgeTabulationResult(
            status="ignored",
            accepted=False,
            idempotent=False,
            runner_session_id=runner_session_id,
            event_key=event_key,
            orch_session_id=orch_session_id,
            orch_session_uuid=str(link_row["orch_session_uuid"]),
            orch_flow_uuid=str(link_row["orch_flow_uuid"]),
        )

    await db_session.execute(
        text(
            """
            UPDATE orch_runner_tabulation_events
            SET
                status = 'applied',
                orch_session_id = :orch_session_id,
                applied_at = NOW(),
                last_error = NULL,
                updated_at = NOW()
            WHERE id = :event_id
              AND status = 'pending_link'
            """
        ),
        {"event_id": event_id, "orch_session_id": orch_session_id},
    )
    return RunnerBridgeTabulationResult(
        status="applied",
        accepted=True,
        idempotent=False,
        runner_session_id=runner_session_id,
        event_key=event_key,
        orch_session_id=persisted.id,
        orch_session_uuid=persisted.uuid,
        orch_flow_uuid=persisted.flow_uuid,
        resume_required=persisted.resume_required,
    )


async def _fetch_link(
    db_session: AsyncSession,
    *,
    runner_session_id: str,
) -> dict[str, Any] | None:
    row = (
        await db_session.execute(
            text(
                """
                SELECT
                    link.runner_session_id::text AS runner_session_id,
                    link.runner_flow_uuid::text AS runner_flow_uuid,
                    link.orch_session_id,
                    link.provider_context_message_id,
                    session.uuid::text AS orch_session_uuid,
                    session.flow_uuid::text AS orch_flow_uuid
                FROM orch_runner_session_links AS link
                JOIN orch_sessions AS session ON session.id = link.orch_session_id
                WHERE link.runner_session_id = CAST(:runner_session_id AS uuid)
                LIMIT 1
                """
            ),
            {"runner_session_id": runner_session_id},
        )
    ).mappings().first()
    return dict(row) if row is not None else None


async def bind_runner_session(
    db_session: AsyncSession,
    *,
    runner_session_id: str,
    runner_flow_uuid: str,
    provider_context_message_id: str,
) -> RunnerBridgeBindResult:
    await _set_workspace_search_path(db_session)
    await db_session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"runner_orch_bridge_session|{runner_session_id}"},
    )

    existing = (
        await db_session.execute(
            text(
                """
                SELECT
                    link.runner_session_id::text AS runner_session_id,
                    link.runner_flow_uuid::text AS runner_flow_uuid,
                    link.orch_session_id,
                    link.provider_context_message_id,
                    session.uuid::text AS orch_session_uuid,
                    session.flow_uuid::text AS orch_flow_uuid
                FROM orch_runner_session_links AS link
                JOIN orch_sessions AS session ON session.id = link.orch_session_id
                WHERE link.runner_session_id = CAST(:runner_session_id AS uuid)
                   OR link.provider_context_message_id = :provider_context_message_id
                ORDER BY link.created_at, link.runner_session_id
                LIMIT 2
                """
            ),
            {
                "runner_session_id": runner_session_id,
                "provider_context_message_id": provider_context_message_id,
            },
        )
    ).mappings().all()
    if existing:
        exact = next(
            (
                row
                for row in existing
                if str(row["runner_session_id"]) == runner_session_id
                and str(row["runner_flow_uuid"]) == runner_flow_uuid
                and str(row["provider_context_message_id"])
                == provider_context_message_id
            ),
            None,
        )
        if exact is None or len(existing) != 1:
            return RunnerBridgeBindResult(
                status="conflict",
                accepted=False,
                idempotent=False,
                runner_session_id=runner_session_id,
            )
        link_row = dict(exact)
        idempotent = True
    else:
        candidates = (
            await db_session.execute(
                text(
                    """
                    SELECT DISTINCT
                        session.id AS orch_session_id,
                        session.uuid::text AS orch_session_uuid,
                        session.flow_uuid::text AS orch_flow_uuid,
                        session.created_at
                    FROM orch_channel_events AS event
                    JOIN orch_sessions AS session ON session.id = event.session_id
                    WHERE event.channel = 'whatsapp'
                      AND event.event_id = :provider_context_message_id
                      AND session.unassigned_at IS NULL
                      AND session.ended_at IS NULL
                    ORDER BY session.created_at DESC, session.id DESC
                    LIMIT 2
                    """
                ),
                {"provider_context_message_id": provider_context_message_id},
            )
        ).mappings().all()
        if not candidates:
            return RunnerBridgeBindResult(
                status="not_found",
                accepted=False,
                idempotent=False,
                runner_session_id=runner_session_id,
            )
        if len(candidates) != 1:
            return RunnerBridgeBindResult(
                status="conflict",
                accepted=False,
                idempotent=False,
                runner_session_id=runner_session_id,
            )
        candidate = candidates[0]
        await db_session.execute(
            text(
                """
                INSERT INTO orch_runner_session_links (
                    runner_session_id,
                    runner_flow_uuid,
                    orch_session_id,
                    provider_context_message_id,
                    created_at,
                    updated_at
                ) VALUES (
                    CAST(:runner_session_id AS uuid),
                    CAST(:runner_flow_uuid AS uuid),
                    :orch_session_id,
                    :provider_context_message_id,
                    NOW(),
                    NOW()
                )
                """
            ),
            {
                "runner_session_id": runner_session_id,
                "runner_flow_uuid": runner_flow_uuid,
                "orch_session_id": int(candidate["orch_session_id"]),
                "provider_context_message_id": provider_context_message_id,
            },
        )
        link_row = {
            "runner_session_id": runner_session_id,
            "runner_flow_uuid": runner_flow_uuid,
            "orch_session_id": int(candidate["orch_session_id"]),
            "provider_context_message_id": provider_context_message_id,
            "orch_session_uuid": str(candidate["orch_session_uuid"]),
            "orch_flow_uuid": str(candidate["orch_flow_uuid"]),
        }
        idempotent = False

    pending = (
        await db_session.execute(
            text(
                """
                SELECT
                    id,
                    event_key,
                    runner_session_id::text AS runner_session_id,
                    runner_flow_uuid::text AS runner_flow_uuid,
                    callback_payload
                FROM orch_runner_tabulation_events
                WHERE runner_session_id = CAST(:runner_session_id AS uuid)
                  AND status = 'pending_link'
                ORDER BY created_at, id
                FOR UPDATE
                """
            ),
            {"runner_session_id": runner_session_id},
        )
    ).mappings().all()
    resume_session_id: int | None = None
    for row in pending:
        applied = await _apply_event_row(
            db_session,
            event_row=dict(row),
            link_row=link_row,
        )
        if applied.resume_required and applied.orch_session_id is not None:
            resume_session_id = applied.orch_session_id

    return RunnerBridgeBindResult(
        status="bound",
        accepted=True,
        idempotent=idempotent,
        runner_session_id=runner_session_id,
        orch_session_id=int(link_row["orch_session_id"]),
        orch_session_uuid=str(link_row["orch_session_uuid"]),
        orch_flow_uuid=str(link_row["orch_flow_uuid"]),
        resume_session_id=resume_session_id,
    )


async def register_runner_tabulation(
    db_session: AsyncSession,
    *,
    runner_session_id: str,
    runner_flow_uuid: str,
    event_key: str,
    callback_payload: dict[str, Any],
) -> RunnerBridgeTabulationResult:
    await _set_workspace_search_path(db_session)
    await db_session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"runner_orch_bridge_session|{runner_session_id}"},
    )
    existing = (
        await db_session.execute(
            text(
                """
                SELECT
                    id,
                    event_key,
                    runner_session_id::text AS runner_session_id,
                    runner_flow_uuid::text AS runner_flow_uuid,
                    orch_session_id,
                    callback_payload,
                    status
                FROM orch_runner_tabulation_events
                WHERE runner_session_id = CAST(:runner_session_id AS uuid)
                  AND event_key = :event_key
                LIMIT 1
                """
            ),
            {
                "runner_session_id": runner_session_id,
                "event_key": event_key,
            },
        )
    ).mappings().first()
    if existing is not None:
        same_event = bool(
            str(existing["runner_session_id"]) == runner_session_id
            and str(existing["runner_flow_uuid"]) == runner_flow_uuid
            and existing.get("callback_payload") == callback_payload
        )
        existing_status = str(existing["status"])
        link_row = (
            await _fetch_link(
                db_session,
                runner_session_id=runner_session_id,
            )
            if existing.get("orch_session_id") is not None
            else None
        )
        return RunnerBridgeTabulationResult(
            status=existing_status if same_event else "conflict",
            accepted=same_event and existing_status in {"pending_link", "applied"},
            idempotent=same_event,
            runner_session_id=runner_session_id,
            event_key=event_key,
            orch_session_id=(
                int(existing["orch_session_id"])
                if existing.get("orch_session_id") is not None
                else None
            ),
            orch_session_uuid=(
                str(link_row["orch_session_uuid"])
                if link_row is not None
                else None
            ),
            orch_flow_uuid=(
                str(link_row["orch_flow_uuid"])
                if link_row is not None
                else None
            ),
        )

    inserted = (
        await db_session.execute(
            text(
                """
                INSERT INTO orch_runner_tabulation_events (
                    event_key,
                    runner_session_id,
                    runner_flow_uuid,
                    callback_payload,
                    status,
                    created_at,
                    updated_at
                ) VALUES (
                    :event_key,
                    CAST(:runner_session_id AS uuid),
                    CAST(:runner_flow_uuid AS uuid),
                    CAST(:callback_payload AS jsonb),
                    'pending_link',
                    NOW(),
                    NOW()
                )
                RETURNING
                    id,
                    event_key,
                    runner_session_id::text AS runner_session_id,
                    runner_flow_uuid::text AS runner_flow_uuid,
                    callback_payload
                """
            ),
            {
                "event_key": event_key,
                "runner_session_id": runner_session_id,
                "runner_flow_uuid": runner_flow_uuid,
                "callback_payload": json.dumps(
                    callback_payload,
                    ensure_ascii=False,
                ),
            },
        )
    ).mappings().one()
    link_row = await _fetch_link(
        db_session,
        runner_session_id=runner_session_id,
    )
    if link_row is None:
        return RunnerBridgeTabulationResult(
            status="pending_link",
            accepted=True,
            idempotent=False,
            runner_session_id=runner_session_id,
            event_key=event_key,
        )
    return await _apply_event_row(
        db_session,
        event_row=dict(inserted),
        link_row=link_row,
    )
