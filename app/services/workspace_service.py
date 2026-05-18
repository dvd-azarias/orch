from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import set_workspace_context
from app.core.workspace import normalize_workspace_uuid, workspace_schema_from_uuid
from app.repositories.workspaces_repository import fetch_active_workspace, fetch_active_workspaces


async def ensure_active_workspace(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
) -> dict:
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    row = await fetch_active_workspace(db_session, workspace_uuid=safe_workspace_uuid)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace não encontrado ou inativo.",
        )
    if str(row.get("provision_status") or "").strip().lower() != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace ainda não finalizado para uso.",
        )
    return row


async def ensure_workspace_ready_for_orch_migrate(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
) -> dict:
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    row = await fetch_active_workspace(db_session, workspace_uuid=safe_workspace_uuid)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace não encontrado ou inativo.",
        )

    provision_status = str(row.get("provision_status") or "").strip().lower()
    provision_step = str(row.get("provision_step") or "").strip().lower()
    is_eligible = provision_status == "completed" or (
        provision_status == "running" and provision_step == "orch_migrate"
    )
    if not is_eligible:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace ainda não elegível para migrate do ORCH.",
        )
    return row


def bind_workspace_context(workspace_uuid: str) -> tuple[str, str]:
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    schema = workspace_schema_from_uuid(safe_workspace_uuid)
    set_workspace_context(
        workspace_uuid=safe_workspace_uuid,
        workspace_schema=schema,
    )
    return safe_workspace_uuid, schema


async def list_completed_workspaces(
    db_session: AsyncSession,
) -> list[dict]:
    rows = await fetch_active_workspaces(db_session)
    return [
        row
        for row in rows
        if str(row.get("provision_status") or "").strip().lower() == "completed"
    ]
