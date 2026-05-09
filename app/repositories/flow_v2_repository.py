from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _normalize_definition(raw_definition: Any) -> dict[str, Any]:
    if isinstance(raw_definition, dict):
        return raw_definition
    if isinstance(raw_definition, str):
        try:
            parsed = json.loads(raw_definition)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def fetch_flow_row(db_session: AsyncSession, *, flow_uuid: str) -> dict[str, Any] | None:
    result = await db_session.execute(
        text(
            """
            SELECT
                id::text AS id,
                slug,
                display_name,
                status,
                current_revision_id::text AS current_revision_id,
                draft_revision_id::text AS draft_revision_id,
                is_active
            FROM flow_v2
            WHERE id = CAST(:flow_uuid AS uuid)
            LIMIT 1
            """
        ),
        {"flow_uuid": flow_uuid},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def fetch_selected_revision(db_session: AsyncSession, *, flow_id: str) -> dict[str, Any] | None:
    published_result = await db_session.execute(
        text(
            """
            SELECT
                id::text AS id,
                flow_id::text AS flow_id,
                version,
                definition,
                is_draft,
                published_at,
                'published'::text AS selection_mode
            FROM flow_v2_revision
            WHERE flow_id = CAST(:flow_id AS uuid)
              AND published_at IS NOT NULL
            ORDER BY version DESC
            LIMIT 1
            """
        ),
        {"flow_id": flow_id},
    )
    published = published_result.mappings().first()
    if published is not None:
        row = dict(published)
        row["definition"] = _normalize_definition(row.get("definition"))
        return row

    draft_result = await db_session.execute(
        text(
            """
            SELECT
                id::text AS id,
                flow_id::text AS flow_id,
                version,
                definition,
                is_draft,
                published_at,
                'draft'::text AS selection_mode
            FROM flow_v2_revision
            WHERE flow_id = CAST(:flow_id AS uuid)
              AND is_draft = TRUE
            ORDER BY version DESC
            LIMIT 1
            """
        ),
        {"flow_id": flow_id},
    )
    draft = draft_result.mappings().first()
    if draft is None:
        return None

    row = dict(draft)
    row["definition"] = _normalize_definition(row.get("definition"))
    return row
