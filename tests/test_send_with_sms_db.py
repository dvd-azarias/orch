from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.repositories.send_with_sms_repository import assign_sms_routing_for_session


@pytest.mark.asyncio
async def test_send_with_sms_isolated_marker_guards_and_rollback() -> None:
    flow_uuid = str(uuid4())
    contact_list_uuid = str(uuid4())
    person_uuid = str(uuid4())
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
                        entity_address TEXT NOT NULL,
                        ended_at TIMESTAMPTZ NULL,
                        unassigned_at TIMESTAMPTZ NULL
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
                        contact_channel_type TEXT NULL,
                        contact_channel_address TEXT NULL,
                        contact_list_id UUID NOT NULL,
                        mailing_id BIGINT NOT NULL,
                        person_uuid UUID NULL,
                        linked_actuator TEXT NULL,
                        unassigned_at TIMESTAMPTZ NULL,
                        updated_at TIMESTAMPTZ NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (
                        id, flow_uuid, state, entity, entity_address
                    ) VALUES (
                        123,
                        CAST(:flow_uuid AS uuid),
                        0,
                        '12345678901',
                        '5511999990001'
                    )
                    """
                ),
                {"flow_uuid": flow_uuid},
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO contact_list_members (
                        id,
                        contact_identifier,
                        contact_channel_type,
                        contact_channel_address,
                        contact_list_id,
                        mailing_id,
                        person_uuid,
                        linked_actuator,
                        updated_at
                    ) VALUES
                        (
                            77, '12345678901', 'sms', '5511999990001',
                            CAST(:contact_list_uuid AS uuid), 1140,
                            CAST(:person_uuid AS uuid), NULL,
                            CAST('2026-01-01T00:00:00+00:00' AS timestamptz)
                        ),
                        (
                            88, '12345678901', 'voice', '5511999990001',
                            CAST(:contact_list_uuid AS uuid), 1140,
                            CAST(:person_uuid AS uuid), NULL, NOW()
                        )
                    """
                ),
                {
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
            }

            with pytest.raises(RuntimeError, match="force outer workflow failure"):
                async with db_session.begin_nested():
                    marked_before_failure = await assign_sms_routing_for_session(
                        db_session,
                        **common,
                    )
                    assert marked_before_failure is not None
                    assert marked_before_failure["mode"] == "marked"
                    raise RuntimeError("force outer workflow failure")

            rolled_back = (
                await db_session.execute(
                    text(
                        """
                        SELECT linked_actuator, updated_at
                        FROM contact_list_members
                        WHERE id = 77
                        """
                    )
                )
            ).mappings().one()
            assert rolled_back["linked_actuator"] is None
            assert rolled_back["updated_at"] == datetime(
                2026, 1, 1, tzinfo=timezone.utc
            )

            marked = await assign_sms_routing_for_session(db_session, **common)
            assert marked == {
                "contact_list_member_id": 77,
                "linked_actuator": "sms",
                "mode": "marked",
            }
            marked_again = await assign_sms_routing_for_session(db_session, **common)
            assert marked_again == {
                "contact_list_member_id": 77,
                "linked_actuator": "sms",
                "mode": "already_marked",
            }

            voice_member = await assign_sms_routing_for_session(
                db_session,
                **{**common, "contact_list_member_id": 88},
            )
            wrong_person = await assign_sms_routing_for_session(
                db_session,
                **{**common, "person_uuid": str(uuid4())},
            )
            wrong_mailing = await assign_sms_routing_for_session(
                db_session,
                **{**common, "mailing_id": 1141},
            )
            assert voice_member is None
            assert wrong_person is None
            assert wrong_mailing is None

            rows = (
                await db_session.execute(
                    text(
                        """
                        SELECT id, linked_actuator
                        FROM contact_list_members
                        ORDER BY id
                        """
                    )
                )
            ).mappings().all()
            assert [dict(row) for row in rows] == [
                {"id": 77, "linked_actuator": "sms"},
                {"id": 88, "linked_actuator": None},
            ]
