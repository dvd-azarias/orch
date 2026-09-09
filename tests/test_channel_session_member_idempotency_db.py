from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.repositories.orch_sessions_repository import upsert_active_session


def _session_fields(*, flow_uuid: str, entity: str, address: str) -> dict:
    return {
        "flow_uuid": flow_uuid,
        "app_name": "GenericApp",
        "entity": entity,
        "entity_type": "person",
        "entity_address": address,
        "entity_session_id": f"{address}:::{flow_uuid}",
    }


@pytest.mark.asyncio
async def test_channel_scope_creates_one_session_per_member_and_reuses_its_retry() -> None:
    flow_uuid = str(uuid4())
    entity = f"person-{uuid4()}"
    address = "5511900000001"
    common = _session_fields(flow_uuid=flow_uuid, entity=entity, address=address)
    session_factory = get_session_factory()
    schema = get_settings().database_schema.replace('"', '""')

    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))

            first_payload = {"session_scope": "channel", "contact_list_member_id": 9100001}
            second_payload = {"session_scope": "channel", "contact_list_member_id": 9100002}
            first = await upsert_active_session(
                db_session,
                **common,
                payload=first_payload,
                extracted=common,
                channel_scope_contact_list_member_id=9100001,
            )
            second = await upsert_active_session(
                db_session,
                **common,
                payload=second_payload,
                extracted=common,
                channel_scope_contact_list_member_id=9100002,
            )
            first_retry = await upsert_active_session(
                db_session,
                **common,
                payload=first_payload,
                extracted=common,
                channel_scope_contact_list_member_id=9100001,
            )

            rows = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            id,
                            entity_session_id,
                            COALESCE(
                                runtime_variables #>> '{session_identity,contact_list_member_id}',
                                runtime_variables #>> '{input_payload,contact_list_member_id}',
                                runtime_variables #>> '{last_payload,contact_list_member_id}'
                            ) AS contact_list_member_id
                        FROM orch_sessions
                        WHERE flow_uuid = CAST(:flow_uuid AS uuid)
                          AND entity = :entity
                          AND entity_address = :entity_address
                        ORDER BY id
                        """
                    ),
                    {
                        "flow_uuid": flow_uuid,
                        "entity": entity,
                        "entity_address": address,
                    },
                )
            ).mappings().all()

            assert first.created is True
            assert second.created is True
            assert first.id != second.id
            assert first_retry.created is False
            assert first_retry.id == first.id
            assert [str(row["contact_list_member_id"]) for row in rows] == ["9100001", "9100002"]
            assert {str(row["entity_session_id"]) for row in rows} == {
                f"{address}:::{flow_uuid}"
            }
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_unspecified_member_identity_preserves_legacy_address_reuse() -> None:
    flow_uuid = str(uuid4())
    entity = f"person-{uuid4()}"
    address = "5511900000002"
    common = _session_fields(flow_uuid=flow_uuid, entity=entity, address=address)
    session_factory = get_session_factory()
    schema = get_settings().database_schema.replace('"', '""')

    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            first = await upsert_active_session(
                db_session,
                **common,
                payload={"contact_list_member_id": 9200001},
                extracted=common,
            )
            second = await upsert_active_session(
                db_session,
                **common,
                payload={"contact_list_member_id": 9200002},
                extracted=common,
            )

            total = await db_session.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM orch_sessions
                    WHERE flow_uuid = CAST(:flow_uuid AS uuid)
                      AND entity = :entity
                      AND entity_address = :entity_address
                    """
                ),
                {
                    "flow_uuid": flow_uuid,
                    "entity": entity,
                    "entity_address": address,
                },
            )

            assert first.created is True
            assert second.created is False
            assert second.id == first.id
            assert int(total or 0) == 1
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_name", ["sent", "limit_reached"])
async def test_channel_member_filter_preserves_whatsapp_finished_session_correlation(
    status_name: str,
) -> None:
    flow_uuid = str(uuid4())
    entity = f"person-{uuid4()}"
    address = "5511900000003"
    member_id = 9300001
    common = _session_fields(flow_uuid=flow_uuid, entity=entity, address=address)
    session_factory = get_session_factory()
    schema = get_settings().database_schema.replace('"', '""')

    payload = {
        "session_scope": "channel",
        "contact_list_member_id": member_id,
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": address}],
                            "statuses": [
                                {
                                    "status": status_name,
                                    "id": f"wamid-channel-member-{status_name}",
                                    "timestamp": "1778238999",
                                    "recipient_id": address,
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            existing_id = await db_session.scalar(
                text(
                    """
                    INSERT INTO orch_sessions (
                        flow_uuid,
                        state,
                        entity_origin_app,
                        entity,
                        entity_type,
                        entity_address,
                        entity_session_id,
                        started_at,
                        ended_at,
                        runtime_variables,
                        created_at,
                        updated_at
                    ) VALUES (
                        CAST(:flow_uuid AS uuid),
                        3,
                        'WhatsApp',
                        :entity,
                        'person',
                        :entity_address,
                        'older-provider-session',
                        NOW(),
                        NOW(),
                        jsonb_build_object(
                            'session_identity',
                            jsonb_build_object(
                                'scope',
                                'channel',
                                'contact_list_member_id',
                                CAST(:contact_list_member_id AS bigint)
                            )
                        ),
                        NOW(),
                        NOW()
                    )
                    RETURNING id
                    """
                ),
                {
                    "flow_uuid": flow_uuid,
                    "entity": entity,
                    "entity_address": address,
                    "contact_list_member_id": member_id,
                },
            )

            result = await upsert_active_session(
                db_session,
                **{**common, "app_name": "WhatsApp"},
                payload=payload,
                extracted=common,
                channel_scope_contact_list_member_id=member_id,
            )

            assert result.created is False
            assert result.id == int(existing_id or 0)
            assert result.state == 2
        finally:
            await transaction.rollback()
