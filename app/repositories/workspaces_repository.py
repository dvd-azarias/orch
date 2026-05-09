from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def fetch_active_workspace(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
) -> dict | None:
    result = await db_session.execute(
        text(
            """
            SELECT
                workspace_uuid::text AS workspace_uuid,
                name,
                provision_status
            FROM target.workspaces
            WHERE
                workspace_uuid = CAST(:workspace_uuid AS uuid)
                AND deleted_at IS NULL
            LIMIT 1
            """
        ),
        {"workspace_uuid": workspace_uuid},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def fetch_active_workspaces(
    db_session: AsyncSession,
) -> list[dict]:
    result = await db_session.execute(
        text(
            """
            SELECT
                workspace_uuid::text AS workspace_uuid,
                name,
                provision_status
            FROM target.workspaces
            WHERE deleted_at IS NULL
            ORDER BY created_at ASC
            """
        )
    )
    return [dict(row) for row in result.mappings().all()]
