from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.api.v1.orch import trigger_orch
from app.core.config import get_settings
from app.core.database import get_session_factory


@pytest.mark.asyncio
async def test_whatsapp_terminal_before_non_terminal_reuses_same_finished_session() -> None:
    flow_uuid = uuid4()

    payload_read = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": "5511922222222"}],
                            "statuses": [
                                {
                                    "status": "read",
                                    "timestamp": "1778240350",
                                    "recipient_id": "5511922222222",
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    payload_sent = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": "5511922222222"}],
                            "statuses": [
                                {
                                    "status": "sent",
                                    "timestamp": "1778238932",
                                    "recipient_id": "5511922222222",
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    session_factory = get_session_factory()

    async with session_factory() as s1:
        first = await trigger_orch(flow_uuid=flow_uuid, payload=payload_read, db_session=s1)

    async with session_factory() as s2:
        second = await trigger_orch(flow_uuid=flow_uuid, payload=payload_sent, db_session=s2)

    schema = get_settings().database_schema.replace('"', '""')
    async with session_factory() as db_session:
        row = (
            await db_session.execute(
                text(
                    f'''
                    SELECT COUNT(*) AS total,
                           MAX(state) AS max_state,
                           BOOL_OR(ended_at IS NOT NULL) AS has_ended,
                           BOOL_OR(whatsapp_read_at IS NOT NULL) AS has_read,
                           BOOL_OR(whatsapp_sent_at IS NOT NULL) AS has_sent
                    FROM "{schema}".orch_sessions
                    WHERE flow_uuid = :flow_uuid
                      AND entity = :entity
                      AND entity_type = 'person'
                      AND entity_address = :entity
                    '''
                ),
                {"flow_uuid": str(flow_uuid), "entity": "5511922222222"},
            )
        ).mappings().one()

    assert first.session_id == second.session_id
    assert int(row["total"]) == 1
    assert int(row["max_state"]) == 3
    assert bool(row["has_ended"]) is True
    assert bool(row["has_read"]) is True
    assert bool(row["has_sent"]) is True


@pytest.mark.asyncio
async def test_dialer_terminal_before_started_reuses_same_finished_session() -> None:
    flow_uuid = uuid4()

    payload_hangup = {
        "uniqueid": "GW01-444.1",
        "hangup": {
            "Event": "Hangup",
            "Disposition": "BUSY",
            "Cause": "17",
            "DialerHangupCause": "17",
            "CdrMailingData": "{'phone': '5511975620806'}",
            "Uniqueid": "GW01-444.1",
            "Linkedid": "GW01-444.1",
            "EndTime": "2026-05-09 01:49:05",
        },
        "makecall": {
            "Event": "DialBegin",
            "DialString": "trunk-sbc-router106/5511975620806",
            "DestUniqueid": "GW01-444.1",
        },
    }

    payload_started = {
        "uniqueid": "GW01-444.1",
        "makecall": {
            "Event": "DialBegin",
            "DialString": "trunk-sbc-router106/5511975620806",
            "DestUniqueid": "GW01-444.1",
        },
    }

    session_factory = get_session_factory()

    async with session_factory() as s1:
        first = await trigger_orch(flow_uuid=flow_uuid, payload=payload_hangup, db_session=s1)

    async with session_factory() as s2:
        second = await trigger_orch(flow_uuid=flow_uuid, payload=payload_started, db_session=s2)

    schema = get_settings().database_schema.replace('"', '""')
    async with session_factory() as db_session:
        row = (
            await db_session.execute(
                text(
                    f'''
                    SELECT COUNT(*) AS total,
                           MAX(state) AS max_state,
                           MIN(state) AS min_state,
                           BOOL_OR(dialer_busy_at IS NOT NULL) AS has_busy,
                           BOOL_OR(ended_at IS NOT NULL) AS has_ended
                    FROM "{schema}".orch_sessions
                    WHERE flow_uuid = :flow_uuid
                      AND entity_address = :entity_address
                      AND entity_session_id = :entity_session_id
                    '''
                ),
                {
                    "flow_uuid": str(flow_uuid),
                    "entity_address": "5511975620806",
                    "entity_session_id": "GW01-444.1",
                },
            )
        ).mappings().one()

    assert first.session_id == second.session_id
    assert int(row["total"]) == 1
    assert int(row["max_state"]) == 3
    assert int(row["min_state"]) == 3
    assert bool(row["has_busy"]) is True
    assert bool(row["has_ended"]) is True


@pytest.mark.asyncio
async def test_generic_finished_session_with_same_external_id_creates_new_session() -> None:
    flow_uuid = uuid4()
    payload = {"external_id": "generic-test-1412", "valor_recebido": 100}

    session_factory = get_session_factory()

    async with session_factory() as s1:
        first = await trigger_orch(flow_uuid=flow_uuid, payload=payload, db_session=s1)

    schema = get_settings().database_schema.replace('"', '""')
    async with session_factory() as mark_finished_session:
        await mark_finished_session.execute(
            text(
                f'''
                UPDATE "{schema}".orch_sessions
                SET state = 3,
                    ended_at = NOW(),
                    updated_at = NOW()
                WHERE id = :session_id
                '''
            ),
            {"session_id": first.session_id},
        )
        await mark_finished_session.commit()

    async with session_factory() as s2:
        second = await trigger_orch(flow_uuid=flow_uuid, payload=payload, db_session=s2)

    async with session_factory() as db_session:
        row = (
            await db_session.execute(
                text(
                    f'''
                    SELECT COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE state = 3) AS finished_total
                    FROM "{schema}".orch_sessions
                    WHERE flow_uuid = :flow_uuid
                      AND entity = :entity
                      AND entity_type = 'api_request'
                      AND entity_address = :entity
                    '''
                ),
                {"flow_uuid": str(flow_uuid), "entity": "generic-test-1412"},
            )
        ).mappings().one()

    assert first.session_id != second.session_id
    assert second.session_created is True
    assert int(row["total"]) == 2
    assert int(row["finished_total"]) == 1
