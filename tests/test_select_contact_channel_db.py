from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.repositories.select_contact_channel_repository import (
    fetch_select_contact_channel_candidate,
    rebind_person_session_to_contact_channel,
)


@pytest.mark.asyncio
async def test_select_contact_channel_isolated_in_temporary_tables() -> None:
    flow_uuid = str(uuid4())
    contact_list_uuid = str(uuid4())
    person_uuid = str(uuid4())
    origin_channel_uuid = str(uuid4())
    primary_channel_uuid = str(uuid4())
    email_channel_uuid = str(uuid4())
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_sessions (
                        id BIGINT PRIMARY KEY,
                        flow_uuid UUID NOT NULL,
                        state SMALLINT NOT NULL,
                        entity TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_address TEXT NOT NULL,
                        unassigned_at TIMESTAMPTZ NULL,
                        updated_at TIMESTAMPTZ NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE contact_draft_channels (
                        id UUID PRIMARY KEY,
                        is_primary BOOLEAN NOT NULL DEFAULT FALSE
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE contact_list_members (
                        id BIGINT PRIMARY KEY,
                        contact_identifier TEXT NOT NULL,
                        contact_name TEXT NULL,
                        contact_full_name TEXT NULL,
                        contact_gender TEXT NULL,
                        contact_country TEXT NULL,
                        contact_province TEXT NULL,
                        contact_city TEXT NULL,
                        contact_birth_date DATE NULL,
                        contact_age INTEGER NULL,
                        contact_channel_type TEXT NULL,
                        contact_channel_label TEXT NULL,
                        contact_channel_address TEXT NULL,
                        contact_channel_extra_data JSONB NULL,
                        contact_channel_id UUID NOT NULL,
                        contact_list_id UUID NOT NULL,
                        mailing_id BIGINT NOT NULL,
                        person_uuid UUID NULL,
                        linked_actuator TEXT NULL,
                        unassigned_at TIMESTAMPTZ NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (
                        id, flow_uuid, state, entity, entity_type, entity_address
                    ) VALUES (
                        123, CAST(:flow_uuid AS uuid), 0, '12345678901', 'person', '5511999990001'
                    )
                    """
                ),
                {"flow_uuid": flow_uuid},
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO contact_draft_channels (id, is_primary)
                    VALUES
                        (CAST(:origin_channel_uuid AS uuid), false),
                        (CAST(:primary_channel_uuid AS uuid), true),
                        (CAST(:email_channel_uuid AS uuid), true)
                    """
                ),
                {
                    "origin_channel_uuid": origin_channel_uuid,
                    "primary_channel_uuid": primary_channel_uuid,
                    "email_channel_uuid": email_channel_uuid,
                },
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO contact_list_members (
                        id,
                        contact_identifier,
                        contact_channel_type,
                        contact_channel_label,
                        contact_channel_address,
                        contact_channel_id,
                        contact_list_id,
                        mailing_id,
                        person_uuid
                    ) VALUES
                        (
                            77, '12345678901', 'voice', 'telefone_1', '5511999990001',
                            CAST(:origin_channel_uuid AS uuid), CAST(:contact_list_uuid AS uuid),
                            1140, CAST(:person_uuid AS uuid)
                        ),
                        (
                            88, '12345678901', 'phone', 'telefone_2', '5511988880002',
                            CAST(:primary_channel_uuid AS uuid), CAST(:contact_list_uuid AS uuid),
                            1140, CAST(:person_uuid AS uuid)
                        ),
                        (
                            99, '12345678901', 'email', 'email_1', 'person@example.test',
                            CAST(:email_channel_uuid AS uuid), CAST(:contact_list_uuid AS uuid),
                            1140, CAST(:person_uuid AS uuid)
                        )
                    """
                ),
                {
                    "origin_channel_uuid": origin_channel_uuid,
                    "primary_channel_uuid": primary_channel_uuid,
                    "email_channel_uuid": email_channel_uuid,
                    "contact_list_uuid": contact_list_uuid,
                    "person_uuid": person_uuid,
                },
            )

            common = {
                "flow_uuid": flow_uuid,
                "session_id": 123,
                "contact_list_member_id": 77,
                "contact_list_id": contact_list_uuid,
                "mailing_id": 1140,
                "person_uuid": person_uuid,
                "channel_type": "voice",
                "channel_label": None,
            }
            channel_candidate = await fetch_select_contact_channel_candidate(
                db_session,
                session_scope="channel",
                **common,
            )
            person_candidate = await fetch_select_contact_channel_candidate(
                db_session,
                session_scope="person",
                **common,
            )
            sms_channel_candidate = await fetch_select_contact_channel_candidate(
                db_session,
                session_scope="channel",
                **{**common, "channel_type": "sms"},
            )
            sms_person_candidate = await fetch_select_contact_channel_candidate(
                db_session,
                session_scope="person",
                **{**common, "channel_type": "sms"},
            )

            assert channel_candidate is not None
            assert channel_candidate["contact_list_member_id"] == 77
            assert person_candidate is not None
            assert person_candidate["contact_list_member_id"] == 88
            assert person_candidate["contact_channel_type"] == "voice"
            assert person_candidate["is_primary"] is True
            assert sms_channel_candidate is not None
            assert sms_channel_candidate["contact_list_member_id"] == 77
            assert sms_channel_candidate["contact_channel_type"] == "voice"
            assert sms_person_candidate is not None
            assert sms_person_candidate["contact_list_member_id"] == 88
            assert sms_person_candidate["contact_channel_type"] == "voice"

            rebound = await rebind_person_session_to_contact_channel(
                db_session,
                flow_uuid=flow_uuid,
                session_id=123,
                contact_list_member_id=88,
                contact_list_id=contact_list_uuid,
                mailing_id=1140,
                person_uuid=person_uuid,
            )
            assert rebound is True

            persisted = (
                (
                    await db_session.execute(
                        text(
                            """
                        SELECT entity_address
                        FROM orch_sessions
                        WHERE id = 123
                        """
                        )
                    )
                )
                .mappings()
                .one()
            )
            members = (
                (
                    await db_session.execute(
                        text(
                            """
                        SELECT id, linked_actuator
                        FROM contact_list_members
                        ORDER BY id
                        """
                        )
                    )
                )
                .mappings()
                .all()
            )

            assert persisted["entity_address"] == "5511988880002"
            assert [dict(row) for row in members] == [
                {"id": 77, "linked_actuator": None},
                {"id": 88, "linked_actuator": None},
                {"id": 99, "linked_actuator": None},
            ]

            await db_session.execute(
                text(
                    """
                    UPDATE orch_sessions
                    SET entity_address = '5511999990001'
                    WHERE id = 123
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (
                        id, flow_uuid, state, entity, entity_type, entity_address
                    ) VALUES (
                        124, CAST(:flow_uuid AS uuid), 0, '12345678901', 'person', '5511988880002'
                    )
                    """
                ),
                {"flow_uuid": flow_uuid},
            )

            conflicting_rebind = await rebind_person_session_to_contact_channel(
                db_session,
                flow_uuid=flow_uuid,
                session_id=123,
                contact_list_member_id=88,
                contact_list_id=contact_list_uuid,
                mailing_id=1140,
                person_uuid=person_uuid,
            )
            current_address = (
                await db_session.execute(
                    text("SELECT entity_address FROM orch_sessions WHERE id = 123")
                )
            ).scalar_one()

            assert conflicting_rebind is False
            assert current_address == "5511999990001"

            await db_session.execute(
                text(
                    """
                    UPDATE orch_sessions
                    SET unassigned_at = NOW()
                    WHERE id = 124
                    """
                )
            )
            rebound_after_unassign = await rebind_person_session_to_contact_channel(
                db_session,
                flow_uuid=flow_uuid,
                session_id=123,
                contact_list_member_id=88,
                contact_list_id=contact_list_uuid,
                mailing_id=1140,
                person_uuid=person_uuid,
            )
            current_address = (
                await db_session.execute(
                    text("SELECT entity_address FROM orch_sessions WHERE id = 123")
                )
            ).scalar_one()

            assert rebound_after_unassign is True
            assert current_address == "5511988880002"
