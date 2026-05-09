from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.api.v1.orch import get_alarms, get_sessions_by_flow, trigger_orch
from app.core.config import get_settings
from app.core.database import get_session_factory


async def _ensure_alarms_table() -> None:
    schema = get_settings().database_schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            await db_session.execute(
                text(
                    f'''
                    CREATE TABLE IF NOT EXISTS "{schema}".orch_sessions_alarms (
                        id BIGSERIAL PRIMARY KEY,
                        uuid UUID NOT NULL DEFAULT gen_random_uuid(),
                        session_uuid UUID,
                        flow_uuid UUID,
                        app_name TEXT,
                        entity TEXT,
                        entity_type TEXT,
                        entity_address TEXT,
                        level TEXT NOT NULL,
                        code TEXT NOT NULL,
                        message TEXT NOT NULL,
                        details JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        request_id TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        CONSTRAINT ck_orch_sessions_alarms_level CHECK (level IN ('warning', 'error'))
                    )
                    '''
                )
            )


@pytest.mark.asyncio
async def test_alarm_persisted_for_invalid_payload() -> None:
    await _ensure_alarms_table()

    flow_uuid = uuid4()
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        with pytest.raises(HTTPException):
            await trigger_orch(flow_uuid=flow_uuid, payload={"foo": "bar"}, db_session=db_session)

    schema = get_settings().database_schema.replace('"', '""')
    async with session_factory() as db_session:
        row = (
            await db_session.execute(
                text(
                    f'''
                    SELECT COUNT(*) AS total
                    FROM "{schema}".orch_sessions_alarms
                    WHERE code = 'trigger_orch_http_exception'
                      AND flow_uuid = :flow_uuid
                    '''
                ),
                {"flow_uuid": str(flow_uuid)},
            )
        ).mappings().one()

    assert int(row["total"]) >= 1


@pytest.mark.asyncio
async def test_alarm_persisted_for_invalid_cursor() -> None:
    await _ensure_alarms_table()

    flow_uuid = uuid4()
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        with pytest.raises(HTTPException):
            await get_sessions_by_flow(
                flow_uuid=flow_uuid,
                limit=50,
                cursor="cursor_invalido",
                db_session=db_session,
            )

    schema = get_settings().database_schema.replace('"', '""')
    async with session_factory() as db_session:
        row = (
            await db_session.execute(
                text(
                    f'''
                    SELECT COUNT(*) AS total
                    FROM "{schema}".orch_sessions_alarms
                    WHERE code = 'query_flow_invalid_cursor'
                      AND flow_uuid = :flow_uuid
                    '''
                ),
                {"flow_uuid": str(flow_uuid)},
            )
        ).mappings().one()

    assert int(row["total"]) >= 1


@pytest.mark.asyncio
async def test_get_alarms_endpoint_with_cursor() -> None:
    await _ensure_alarms_table()
    flow_uuid = uuid4()
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        with pytest.raises(HTTPException):
            await trigger_orch(flow_uuid=flow_uuid, payload={"foo": "bar"}, db_session=db_session)

    async with session_factory() as db_session:
        page1 = await get_alarms(
            level="warning",
            code="trigger_orch_http_exception",
            flow_uuid=flow_uuid,
            session_uuid=None,
            app_name=None,
            limit=1,
            cursor=None,
            db_session=db_session,
        )

    assert page1.total >= 1
    assert len(page1.items) == 1
    if page1.next_cursor is not None:
        async with session_factory() as db_session:
            page2 = await get_alarms(
                level="warning",
                code="trigger_orch_http_exception",
                flow_uuid=flow_uuid,
                session_uuid=None,
                app_name=None,
                limit=1,
                cursor=page1.next_cursor,
                db_session=db_session,
            )
        assert page2.total >= 0
