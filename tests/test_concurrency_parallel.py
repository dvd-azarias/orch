from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.api.v1.orch import trigger_orch
from app.core.config import get_settings
from app.core.database import get_session_factory


@pytest.mark.asyncio
async def test_concurrent_whatsapp_sent_and_delivered_single_active_session() -> None:
    flow_uuid = uuid4()

    payload_sent = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": "5511911111111"}],
                            "statuses": [
                                {
                                    "status": "sent",
                                    "timestamp": "1778238932",
                                    "recipient_id": "5511911111111",
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    payload_delivered = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": "5511911111111"}],
                            "statuses": [
                                {
                                    "status": "delivered",
                                    "timestamp": "1778239358",
                                    "recipient_id": "5511911111111",
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    session_factory = get_session_factory()

    async def send(payload: dict) -> int:
        async with session_factory() as db_session:
            result = await trigger_orch(flow_uuid=flow_uuid, payload=payload, db_session=db_session)
            return result.session_id

    session_ids = await asyncio.gather(send(payload_sent), send(payload_delivered))

    schema = get_settings().database_schema.replace('"', '""')
    async with session_factory() as db_session:
        row = (
            await db_session.execute(
                text(
                    f'''
                    SELECT COUNT(*) AS total,
                           BOOL_OR(whatsapp_sent_at IS NOT NULL) AS has_sent,
                           BOOL_OR(whatsapp_delivered_at IS NOT NULL) AS has_delivered,
                           MAX(state) AS max_state
                    FROM "{schema}".orch_sessions
                    WHERE flow_uuid = :flow_uuid
                      AND entity = :entity
                      AND entity_type = 'person'
                      AND entity_address = :entity
                    '''
                ),
                {"flow_uuid": str(flow_uuid), "entity": "5511911111111"},
            )
        ).mappings().one()

    assert session_ids[0] == session_ids[1]
    assert int(row["total"]) == 1
    assert bool(row["has_sent"]) is True
    assert bool(row["has_delivered"]) is True
    assert int(row["max_state"]) == 2


@pytest.mark.asyncio
async def test_concurrent_dialer_started_and_hangup_reuse_single_session() -> None:
    flow_uuid = uuid4()

    payload_started = {
        "uniqueid": "GW01-333.1",
        "makecall": {
            "Event": "DialBegin",
            "DialString": "trunk-sbc-router106/5511975620806",
            "DestUniqueid": "GW01-333.1",
        },
    }

    payload_hangup = {
        "uniqueid": "GW01-333.1",
        "hangup": {
            "Event": "Hangup",
            "Disposition": "BUSY",
            "Cause": "17",
            "DialerHangupCause": "17",
            "CdrMailingData": "{'phone': '5511975620806'}",
            "Uniqueid": "GW01-333.1",
            "Linkedid": "GW01-333.1",
            "EndTime": "2026-05-09 01:49:05",
        },
        "makecall": {
            "Event": "DialBegin",
            "DialString": "trunk-sbc-router106/5511975620806",
            "DestUniqueid": "GW01-333.1",
        },
    }

    session_factory = get_session_factory()

    async def send(payload: dict) -> int:
        async with session_factory() as db_session:
            result = await trigger_orch(flow_uuid=flow_uuid, payload=payload, db_session=db_session)
            return result.session_id

    session_ids = await asyncio.gather(send(payload_started), send(payload_hangup))

    schema = get_settings().database_schema.replace('"', '""')
    async with session_factory() as db_session:
        row = (
            await db_session.execute(
                text(
                    f'''
                    SELECT COUNT(*) AS total,
                           MAX(state) AS max_state,
                           BOOL_OR(dialer_busy_at IS NOT NULL) AS has_busy,
                           BOOL_OR(ended_at IS NOT NULL) AS has_ended,
                           BOOL_OR(entity_origin_app = 'DialerApp') AS origin_ok
                    FROM "{schema}".orch_sessions
                    WHERE flow_uuid = :flow_uuid
                      AND entity_address = :entity_address
                      AND entity_session_id = :entity_session_id
                    '''
                ),
                {
                    "flow_uuid": str(flow_uuid),
                    "entity_address": "5511975620806",
                    "entity_session_id": "GW01-333.1",
                },
            )
        ).mappings().one()

    assert session_ids[0] == session_ids[1]
    assert int(row["total"]) == 1
    assert int(row["max_state"]) == 3
    assert bool(row["has_busy"]) is True
    assert bool(row["has_ended"]) is True
    assert bool(row["origin_ok"]) is True
