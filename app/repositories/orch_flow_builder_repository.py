from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workspace import get_current_workspace_schema


class FlowBuilderSessionNotFoundError(LookupError):
    pass


class FlowBuilderVersionConflictError(RuntimeError):
    pass


def _safe_schema() -> str:
    return get_current_workspace_schema().replace('"', '""')


async def create_flow_builder_session(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    intent: str,
    flow_uuid: str | None,
    created_by: str,
    initial_message: str | None,
) -> dict[str, Any]:
    schema = _safe_schema()
    result = await db_session.execute(
        text(
            f"""
            INSERT INTO "{schema}".orch_flow_builder_sessions (
                workspace_uuid,
                mode,
                intent,
                status,
                flow_uuid,
                created_by
            )
            VALUES (
                CAST(:workspace_uuid AS uuid),
                'orchestration',
                :intent,
                'planning',
                CAST(:flow_uuid AS uuid),
                :created_by
            )
            RETURNING *
            """
        ),
        {
            "workspace_uuid": workspace_uuid,
            "intent": intent,
            "flow_uuid": flow_uuid,
            "created_by": created_by,
        },
    )
    row = dict(result.mappings().one())
    if initial_message:
        await _insert_message(
            db_session,
            schema=schema,
            session_id=str(row["id"]),
            role="user",
            content=initial_message,
            attachment_metadata={},
            structured_payload={},
        )
    return row


async def _insert_message(
    db_session: AsyncSession,
    *,
    schema: str,
    session_id: str,
    role: str,
    content: str,
    attachment_metadata: dict[str, Any],
    structured_payload: dict[str, Any],
) -> dict[str, Any]:
    result = await db_session.execute(
        text(
            f"""
            INSERT INTO "{schema}".orch_flow_builder_messages (
                session_id,
                sequence,
                role,
                content,
                attachment_metadata,
                structured_payload
            )
            SELECT
                CAST(:session_id AS uuid),
                COALESCE(MAX(sequence), 0) + 1,
                :role,
                :content,
                CAST(:attachment_metadata AS jsonb),
                CAST(:structured_payload AS jsonb)
            FROM "{schema}".orch_flow_builder_messages
            WHERE session_id = CAST(:session_id AS uuid)
            RETURNING *
            """
        ),
        {
            "session_id": session_id,
            "role": role,
            "content": content,
            "attachment_metadata": json.dumps(attachment_metadata, ensure_ascii=False),
            "structured_payload": json.dumps(structured_payload, ensure_ascii=False),
        },
    )
    return dict(result.mappings().one())


async def append_flow_builder_message(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    session_id: str,
    role: str,
    content: str,
    attachment_metadata: dict[str, Any] | None = None,
    structured_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema = _safe_schema()
    locked = await db_session.execute(
        text(
            f"""
            SELECT id
            FROM "{schema}".orch_flow_builder_sessions
            WHERE id = CAST(:session_id AS uuid)
              AND workspace_uuid = CAST(:workspace_uuid AS uuid)
            FOR UPDATE
            """
        ),
        {"session_id": session_id, "workspace_uuid": workspace_uuid},
    )
    if locked.scalar_one_or_none() is None:
        raise FlowBuilderSessionNotFoundError(session_id)
    message = await _insert_message(
        db_session,
        schema=schema,
        session_id=session_id,
        role=role,
        content=content,
        attachment_metadata=attachment_metadata or {},
        structured_payload=structured_payload or {},
    )
    await db_session.execute(
        text(
            f"""
            UPDATE "{schema}".orch_flow_builder_sessions
            SET version = version + 1,
                updated_at = NOW()
            WHERE id = CAST(:session_id AS uuid)
            """
        ),
        {"session_id": session_id},
    )
    return message


async def fetch_flow_builder_session(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    session_id: str,
) -> dict[str, Any]:
    schema = _safe_schema()
    result = await db_session.execute(
        text(
            f"""
            SELECT *
            FROM "{schema}".orch_flow_builder_sessions
            WHERE id = CAST(:session_id AS uuid)
              AND workspace_uuid = CAST(:workspace_uuid AS uuid)
            LIMIT 1
            """
        ),
        {"session_id": session_id, "workspace_uuid": workspace_uuid},
    )
    row = result.mappings().first()
    if row is None:
        raise FlowBuilderSessionNotFoundError(session_id)
    payload = dict(row)
    messages = await db_session.execute(
        text(
            f"""
            SELECT *
            FROM "{schema}".orch_flow_builder_messages
            WHERE session_id = CAST(:session_id AS uuid)
            ORDER BY sequence ASC
            """
        ),
        {"session_id": session_id},
    )
    payload["messages"] = [dict(item) for item in messages.mappings().all()]
    return payload


async def store_flow_builder_compilation(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    session_id: str,
    expected_version: int,
    plan: dict[str, Any],
    compiled_definition: dict[str, Any] | None,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    schema = _safe_schema()
    status = "ready" if compiled_definition is not None else "planning"
    result = await db_session.execute(
        text(
            f"""
            UPDATE "{schema}".orch_flow_builder_sessions
            SET plan = CAST(:plan AS jsonb),
                compiled_definition = CAST(:compiled_definition AS jsonb),
                issues = CAST(:issues AS jsonb),
                status = :status,
                version = version + 1,
                updated_at = NOW()
            WHERE id = CAST(:session_id AS uuid)
              AND workspace_uuid = CAST(:workspace_uuid AS uuid)
              AND version = :expected_version
            RETURNING *
            """
        ),
        {
            "session_id": session_id,
            "workspace_uuid": workspace_uuid,
            "expected_version": expected_version,
            "plan": json.dumps(plan, ensure_ascii=False),
            "compiled_definition": (
                json.dumps(compiled_definition, ensure_ascii=False)
                if compiled_definition is not None
                else None
            ),
            "issues": json.dumps(issues, ensure_ascii=False),
            "status": status,
        },
    )
    row = result.mappings().first()
    if row is not None:
        return dict(row)

    exists = await db_session.execute(
        text(
            f"""
            SELECT version
            FROM "{schema}".orch_flow_builder_sessions
            WHERE id = CAST(:session_id AS uuid)
              AND workspace_uuid = CAST(:workspace_uuid AS uuid)
            """
        ),
        {"session_id": session_id, "workspace_uuid": workspace_uuid},
    )
    if exists.scalar_one_or_none() is None:
        raise FlowBuilderSessionNotFoundError(session_id)
    raise FlowBuilderVersionConflictError(session_id)


async def apply_flow_builder_assistant_turn(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    session_id: str,
    expected_version: int,
    user_content: str,
    assistant_content: str,
    assistant_payload: dict[str, Any],
    plan: dict[str, Any],
    compiled_definition: dict[str, Any] | None,
    issues: list[dict[str, Any]],
) -> None:
    schema = _safe_schema()
    locked = await db_session.execute(
        text(
            f"""
            SELECT version
            FROM "{schema}".orch_flow_builder_sessions
            WHERE id = CAST(:session_id AS uuid)
              AND workspace_uuid = CAST(:workspace_uuid AS uuid)
            FOR UPDATE
            """
        ),
        {"session_id": session_id, "workspace_uuid": workspace_uuid},
    )
    current_version = locked.scalar_one_or_none()
    if current_version is None:
        raise FlowBuilderSessionNotFoundError(session_id)
    if int(current_version) != expected_version:
        raise FlowBuilderVersionConflictError(session_id)

    await _insert_message(
        db_session,
        schema=schema,
        session_id=session_id,
        role="user",
        content=user_content,
        attachment_metadata={},
        structured_payload={},
    )
    await _insert_message(
        db_session,
        schema=schema,
        session_id=session_id,
        role="assistant",
        content=assistant_content,
        attachment_metadata={},
        structured_payload=assistant_payload,
    )

    next_status = "ready" if compiled_definition is not None else "planning"
    await db_session.execute(
        text(
            f"""
            UPDATE "{schema}".orch_flow_builder_sessions
            SET plan = CAST(:plan AS jsonb),
                compiled_definition = CAST(:compiled_definition AS jsonb),
                issues = CAST(:issues AS jsonb),
                status = :status,
                version = version + 1,
                updated_at = NOW()
            WHERE id = CAST(:session_id AS uuid)
              AND workspace_uuid = CAST(:workspace_uuid AS uuid)
            """
        ),
        {
            "session_id": session_id,
            "workspace_uuid": workspace_uuid,
            "plan": json.dumps(plan, ensure_ascii=False),
            "compiled_definition": (
                json.dumps(compiled_definition, ensure_ascii=False)
                if compiled_definition is not None
                else None
            ),
            "issues": json.dumps(issues, ensure_ascii=False),
            "status": next_status,
        },
    )


async def lock_flow_builder_session_for_draft(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    session_id: str,
) -> dict[str, Any]:
    schema = _safe_schema()
    result = await db_session.execute(
        text(
            f"""
            SELECT *
            FROM "{schema}".orch_flow_builder_sessions
            WHERE id = CAST(:session_id AS uuid)
              AND workspace_uuid = CAST(:workspace_uuid AS uuid)
            FOR UPDATE
            """
        ),
        {"session_id": session_id, "workspace_uuid": workspace_uuid},
    )
    row = result.mappings().first()
    if row is None:
        raise FlowBuilderSessionNotFoundError(session_id)
    return dict(row)


async def store_flow_builder_draft_result(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    session_id: str,
    expected_version: int,
    flow_uuid: str,
    draft_checksum: str,
) -> None:
    schema = _safe_schema()
    result = await db_session.execute(
        text(
            f"""
            UPDATE "{schema}".orch_flow_builder_sessions
            SET flow_uuid = CAST(:flow_uuid AS uuid),
                draft_checksum = :draft_checksum,
                status = 'saved',
                version = version + 1,
                updated_at = NOW()
            WHERE id = CAST(:session_id AS uuid)
              AND workspace_uuid = CAST(:workspace_uuid AS uuid)
              AND version = :expected_version
              AND status = 'ready'
              AND flow_uuid IS NULL
            RETURNING id
            """
        ),
        {
            "session_id": session_id,
            "workspace_uuid": workspace_uuid,
            "expected_version": expected_version,
            "flow_uuid": flow_uuid,
            "draft_checksum": draft_checksum,
        },
    )
    if result.scalar_one_or_none() is None:
        raise FlowBuilderVersionConflictError(session_id)
