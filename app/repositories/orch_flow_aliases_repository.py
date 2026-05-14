from __future__ import annotations

from secrets import token_hex
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_ALIAS_HEX_LEN = 14
_CREATE_ALIAS_MAX_RETRIES = 8


async def fetch_active_flow_alias(
    db_session: AsyncSession,
    *,
    alias: str,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            """
            SELECT
                alias,
                workspace_uuid::text AS workspace_uuid,
                flow_uuid::text AS flow_uuid,
                is_active,
                created_at,
                updated_at
            FROM target.orch_flow_aliases
            WHERE alias = :alias
              AND is_active = TRUE
            LIMIT 1
            """
        ),
        {"alias": alias},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def fetch_flow_alias_by_workspace_flow(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    flow_uuid: str,
) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            """
            SELECT
                alias,
                workspace_uuid::text AS workspace_uuid,
                flow_uuid::text AS flow_uuid,
                is_active,
                created_at,
                updated_at
            FROM target.orch_flow_aliases
            WHERE workspace_uuid = CAST(:workspace_uuid AS uuid)
              AND flow_uuid = CAST(:flow_uuid AS uuid)
            LIMIT 1
            """
        ),
        {
            "workspace_uuid": workspace_uuid,
            "flow_uuid": flow_uuid,
        },
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def create_or_get_flow_alias(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    flow_uuid: str,
) -> dict[str, Any]:
    existing = await fetch_flow_alias_by_workspace_flow(
        db_session,
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
    )
    if existing is not None:
        if not bool(existing.get("is_active")):
            await db_session.execute(
                text(
                    """
                    UPDATE target.orch_flow_aliases
                    SET is_active = TRUE, updated_at = NOW()
                    WHERE workspace_uuid = CAST(:workspace_uuid AS uuid)
                      AND flow_uuid = CAST(:flow_uuid AS uuid)
                    """
                ),
                {
                    "workspace_uuid": workspace_uuid,
                    "flow_uuid": flow_uuid,
                },
            )
            existing["is_active"] = True
        return existing

    for _ in range(_CREATE_ALIAS_MAX_RETRIES):
        alias = token_hex(_ALIAS_HEX_LEN // 2)
        result = await db_session.execute(
            text(
                """
                INSERT INTO target.orch_flow_aliases (
                    alias,
                    workspace_uuid,
                    flow_uuid,
                    is_active,
                    created_at,
                    updated_at
                )
                VALUES (
                    :alias,
                    CAST(:workspace_uuid AS uuid),
                    CAST(:flow_uuid AS uuid),
                    TRUE,
                    NOW(),
                    NOW()
                )
                ON CONFLICT DO NOTHING
                RETURNING
                    alias,
                    workspace_uuid::text AS workspace_uuid,
                    flow_uuid::text AS flow_uuid,
                    is_active,
                    created_at,
                    updated_at
                """
            ),
            {
                "alias": alias,
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
            },
        )
        row = result.mappings().first()
        if row is not None:
            return dict(row)
        pair_row = await fetch_flow_alias_by_workspace_flow(
            db_session,
            workspace_uuid=workspace_uuid,
            flow_uuid=flow_uuid,
        )
        if pair_row is not None:
            return pair_row
        continue

    raise RuntimeError("Não foi possível gerar alias único após múltiplas tentativas.")
