from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_engine, get_session_factory
from app.repositories.flow_v2_repository import fetch_revision_by_id
from app.repositories.orch_sessions_repository import ensure_session_workflow_revision_pin


@pytest.fixture(autouse=True)
async def _dispose_connections_after_temporary_schema() -> AsyncIterator[None]:
    yield
    await get_engine().dispose()


@pytest.mark.asyncio
async def test_revision_pin_is_atomic_and_does_not_replace_existing_pin() -> None:
    flow_uuid = str(uuid4())
    first_revision_id = str(uuid4())
    second_revision_id = str(uuid4())
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_sessions (
                        id BIGINT PRIMARY KEY,
                        flow_uuid UUID NOT NULL,
                        runtime_variables JSONB NULL,
                        updated_at TIMESTAMPTZ NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (id, flow_uuid, runtime_variables, updated_at)
                        VALUES (
                            7001,
                            CAST(:flow_uuid AS uuid),
                            CAST(:runtime_variables AS jsonb),
                            NOW()
                        )
                    """
                ),
                {
                    "flow_uuid": flow_uuid,
                    "runtime_variables": json.dumps(
                        {
                            "other": {"preserved": True},
                            "workflow_v2": {"next_card_cursor": "card-1"},
                        }
                    ),
                },
            )

            first_runtime = await ensure_session_workflow_revision_pin(
                db_session,
                session_id=7001,
                flow_uuid=flow_uuid,
                revision_patch={
                    "revision_id": first_revision_id,
                    "revision_version": 7,
                    "revision_mode": "published",
                },
            )
            second_runtime = await ensure_session_workflow_revision_pin(
                db_session,
                session_id=7001,
                flow_uuid=flow_uuid,
                revision_patch={
                    "revision_id": second_revision_id,
                    "revision_version": 8,
                    "revision_mode": "published",
                },
            )

            assert first_runtime is not None
            assert second_runtime is not None
            assert first_runtime["other"] == {"preserved": True}
            assert first_runtime["workflow_v2"]["next_card_cursor"] == "card-1"
            assert first_runtime["workflow_v2"]["revision_id"] == first_revision_id
            assert second_runtime["workflow_v2"]["revision_id"] == first_revision_id
            assert second_runtime["workflow_v2"]["revision_version"] == 7


@pytest.mark.asyncio
async def test_fetch_revision_by_id_requires_same_flow() -> None:
    flow_uuid = str(uuid4())
    other_flow_uuid = str(uuid4())
    revision_id = str(uuid4())
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE flow_v2_revision (
                        id UUID PRIMARY KEY,
                        flow_id UUID NOT NULL,
                        version INTEGER NOT NULL,
                        definition JSONB NOT NULL,
                        is_draft BOOLEAN NOT NULL,
                        published_at TIMESTAMPTZ NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO flow_v2_revision (
                        id,
                        flow_id,
                        version,
                        definition,
                        is_draft,
                        published_at
                    )
                    VALUES (
                        CAST(:revision_id AS uuid),
                        CAST(:flow_uuid AS uuid),
                        7,
                        '{"components":[]}'::jsonb,
                        FALSE,
                        NOW()
                    )
                    """
                ),
                {
                    "revision_id": revision_id,
                    "flow_uuid": flow_uuid,
                },
            )

            matching = await fetch_revision_by_id(
                db_session,
                flow_id=flow_uuid,
                revision_id=revision_id,
            )
            mismatched = await fetch_revision_by_id(
                db_session,
                flow_id=other_flow_uuid,
                revision_id=revision_id,
            )

            assert matching is not None
            assert matching["id"] == revision_id
            assert matching["version"] == 7
            assert matching["selection_mode"] == "published"
            assert mismatched is None
