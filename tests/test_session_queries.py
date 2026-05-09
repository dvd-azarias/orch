from __future__ import annotations

from uuid import uuid4

import pytest

from app.api.v1.orch import trigger_orch
from app.core.database import get_session_factory
from app.services.session_query_service import (
    get_session_by_uuid,
    list_sessions_by_entity,
    list_sessions_by_flow_uuid,
)


@pytest.mark.asyncio
async def test_query_sessions_by_flow_and_uuid() -> None:
    flow_uuid = uuid4()
    payload = {"external_id": "query-flow-001"}

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        created = await trigger_orch(flow_uuid=flow_uuid, payload=payload, db_session=db_session)

    async with session_factory() as db_session:
        by_flow = await list_sessions_by_flow_uuid(db_session, flow_uuid=str(flow_uuid), limit=50, cursor=None)

    assert len(by_flow.items) >= 1
    assert by_flow.items[0]["flow_uuid"] == str(flow_uuid)

    async with session_factory() as db_session:
        by_uuid = await get_session_by_uuid(db_session, session_uuid=created.session_uuid)

    assert by_uuid is not None
    assert by_uuid["uuid"] == created.session_uuid
    assert by_uuid["entity"] == "query-flow-001"


@pytest.mark.asyncio
async def test_query_sessions_by_entity_filters() -> None:
    flow_uuid = uuid4()
    payload = {"external_id": "query-entity-001"}

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        await trigger_orch(flow_uuid=flow_uuid, payload=payload, db_session=db_session)

    async with session_factory() as db_session:
        filtered = await list_sessions_by_entity(
            db_session,
            entity="query-entity-001",
            entity_type="api_request",
            entity_address="query-entity-001",
            limit=20,
            cursor=None,
        )

    assert len(filtered.items) >= 1
    assert filtered.items[0]["entity"] == "query-entity-001"
    assert filtered.items[0]["entity_type"] == "api_request"


@pytest.mark.asyncio
async def test_query_sessions_by_flow_with_cursor_pagination() -> None:
    flow_uuid = uuid4()
    payloads = [
        {"external_id": "flow-page-001"},
        {"external_id": "flow-page-002"},
        {"external_id": "flow-page-003"},
    ]

    session_factory = get_session_factory()
    for payload in payloads:
        async with session_factory() as db_session:
            await trigger_orch(flow_uuid=flow_uuid, payload=payload, db_session=db_session)

    async with session_factory() as db_session:
        page1 = await list_sessions_by_flow_uuid(
            db_session,
            flow_uuid=str(flow_uuid),
            limit=2,
            cursor=None,
        )
        page2 = await list_sessions_by_flow_uuid(
            db_session,
            flow_uuid=str(flow_uuid),
            limit=2,
            cursor=page1.next_cursor,
        )

    assert len(page1.items) == 2
    assert page1.next_cursor is not None
    assert len(page2.items) >= 1
    assert page2.items[0]["id"] not in {item["id"] for item in page1.items}


@pytest.mark.asyncio
async def test_query_sessions_by_entity_with_cursor_pagination() -> None:
    entity = "entity-cursor-001"
    flows = [uuid4(), uuid4(), uuid4()]
    payload = {"external_id": entity}

    session_factory = get_session_factory()
    for flow_uuid in flows:
        async with session_factory() as db_session:
            await trigger_orch(flow_uuid=flow_uuid, payload=payload, db_session=db_session)

    async with session_factory() as db_session:
        page1 = await list_sessions_by_entity(
            db_session,
            entity=entity,
            entity_type="api_request",
            entity_address=entity,
            limit=2,
            cursor=None,
        )
        page2 = await list_sessions_by_entity(
            db_session,
            entity=entity,
            entity_type="api_request",
            entity_address=entity,
            limit=2,
            cursor=page1.next_cursor,
        )

    assert len(page1.items) == 2
    assert page1.next_cursor is not None
    assert len(page2.items) >= 1
    assert page2.items[0]["id"] not in {item["id"] for item in page1.items}
