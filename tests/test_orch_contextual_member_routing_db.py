from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.repositories.orch_sessions_repository import (
    assign_dialer_handoff_routing_for_session,
    assign_dialer_routing_for_session,
    assign_whatsapp_routing_for_session,
    fetch_contact_runtime_context_for_session,
)


@pytest.mark.asyncio
async def test_contextual_member_routing_isolated_in_temporary_tables() -> None:
    flow_uuid = str(uuid4())
    expected_list_uuid = str(uuid4())
    wrong_list_uuid = str(uuid4())
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_sessions (
                        id BIGINT PRIMARY KEY,
                        entity TEXT NOT NULL,
                        entity_address TEXT NOT NULL,
                        flow_uuid UUID NOT NULL,
                        unassigned_at TIMESTAMP NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE contact_list_members (
                        id BIGINT PRIMARY KEY,
                        ani TEXT NULL,
                        linked_actuator TEXT NULL,
                        outbound_hsm JSONB NULL,
                        outbound_hsm_idempotency_key TEXT NULL,
                        outbound_hsm_prepared_at TIMESTAMPTZ NULL,
                        outbound_hsm_session_uuid UUID NULL,
                        outbound_hsm_component_ref_id UUID NULL,
                        contact_identifier TEXT NOT NULL,
                        contact_list_id UUID NOT NULL,
                        mailing_id BIGINT NULL,
                        unassigned_at TIMESTAMP NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NULL,
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
                        person_uuid UUID NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (id, entity, entity_address, flow_uuid)
                    VALUES (6937, '30392286855', '5511999999999', CAST(:flow_uuid AS uuid))
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
                        contact_list_id,
                        mailing_id,
                        contact_channel_address,
                        created_at
                    )
                    VALUES
                        (
                            10655,
                            '30392286855',
                            CAST(:expected_list_uuid AS uuid),
                            1115,
                            '5511999999999',
                            NOW() - INTERVAL '1 hour'
                        ),
                        (
                            10687,
                            '30392286855',
                            CAST(:wrong_list_uuid AS uuid),
                            1114,
                            '5511888888888',
                            NOW()
                        )
                    """
                ),
                {
                    "expected_list_uuid": expected_list_uuid,
                    "wrong_list_uuid": wrong_list_uuid,
                },
            )

            base = {
                "flow_uuid": flow_uuid,
                "session_id": 6937,
            }
            legacy = await fetch_contact_runtime_context_for_session(db_session, **base)
            scoped = await fetch_contact_runtime_context_for_session(
                db_session,
                **base,
                contact_list_member_id=10655,
                contact_list_id=expected_list_uuid,
                mailing_id=1115,
            )
            conflict = await fetch_contact_runtime_context_for_session(
                db_session,
                **base,
                contact_list_member_id=10655,
                contact_list_id=wrong_list_uuid,
                mailing_id=1114,
            )
            address_conflict = await fetch_contact_runtime_context_for_session(
                db_session,
                **base,
                contact_list_member_id=10687,
                contact_list_id=wrong_list_uuid,
                mailing_id=1114,
            )

            assert legacy is not None
            assert legacy["contact_list_member_id"] == 10687
            assert scoped is not None
            assert scoped["contact_list_member_id"] == 10655
            assert conflict is None
            assert address_conflict is None

            dialer_assignment = await assign_dialer_routing_for_session(
                db_session,
                **base,
                contact_list_member_id=10655,
            )
            assert dialer_assignment is not None
            assert dialer_assignment["contact_list_member_id"] == 10655

            whatsapp_assignment = await assign_whatsapp_routing_for_session(
                db_session,
                **base,
                numbers=[],
                contact_list_member_id=10655,
            )
            assert whatsapp_assignment is not None
            assert whatsapp_assignment["contact_list_member_id"] == 10655

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
                {"id": 10655, "linked_actuator": "whatsapp"},
                {"id": 10687, "linked_actuator": None},
            ]


@pytest.mark.asyncio
async def test_dialer_handoff_materializes_list_validity_from_active_link() -> None:
    flow_uuid = str(uuid4())
    contact_list_uuid = str(uuid4())
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_sessions (
                        id BIGINT PRIMARY KEY,
                        entity TEXT NOT NULL,
                        flow_uuid UUID NOT NULL,
                        unassigned_at TIMESTAMP NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE contact_list_members (
                        id BIGINT PRIMARY KEY,
                        ani TEXT NULL,
                        linked_actuator TEXT NULL,
                        list_validity DATE NULL,
                        contact_identifier TEXT NOT NULL,
                        contact_list_id UUID NOT NULL,
                        mailing_id BIGINT NOT NULL,
                        unassigned_at TIMESTAMP NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE flow_mailing_links (
                        id UUID PRIMARY KEY,
                        flow_id UUID NOT NULL,
                        mailing_id BIGINT NOT NULL,
                        contact_list_id UUID NOT NULL,
                        linked_at TIMESTAMPTZ NOT NULL,
                        unlinked_at TIMESTAMPTZ NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (id, entity, flow_uuid)
                    VALUES (9001, '30392286855', CAST(:flow_uuid AS uuid))
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
                        contact_list_id,
                        mailing_id,
                        created_at
                    )
                    VALUES (
                        2001,
                        '30392286855',
                        CAST(:contact_list_uuid AS uuid),
                        3001,
                        NOW()
                    )
                    """
                ),
                {"contact_list_uuid": contact_list_uuid},
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO flow_mailing_links (
                        id,
                        flow_id,
                        mailing_id,
                        contact_list_id,
                        linked_at
                    )
                    VALUES (
                        CAST(:link_uuid AS uuid),
                        CAST(:flow_uuid AS uuid),
                        3001,
                        CAST(:contact_list_uuid AS uuid),
                        TIMESTAMPTZ '2026-09-11 02:30:00+00'
                    )
                    """
                ),
                {
                    "flow_uuid": flow_uuid,
                    "contact_list_uuid": contact_list_uuid,
                    "link_uuid": str(uuid4()),
                },
            )

            assignment = await assign_dialer_handoff_routing_for_session(
                db_session,
                flow_uuid=flow_uuid,
                session_id=9001,
                contact_list_member_id=2001,
                list_validity_mode="days_after_link",
                list_validity_days=2,
            )

            assert assignment is not None
            assert assignment["contact_list_member_id"] == 2001
            assert assignment["linked_actuator"] == "dialer"
            assert assignment["list_validity"] == "2026-09-12"

            indefinite = await assign_dialer_handoff_routing_for_session(
                db_session,
                flow_uuid=flow_uuid,
                session_id=9001,
                contact_list_member_id=2001,
                list_validity_mode="indefinite",
                list_validity_days=0,
            )
            assert indefinite is not None
            assert indefinite["list_validity"] is None

            await db_session.execute(
                text(
                    """
                    UPDATE flow_mailing_links SET unlinked_at = NOW()
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    UPDATE contact_list_members
                       SET linked_actuator = NULL,
                           list_validity = NULL
                    """
                )
            )
            missing_link = await assign_dialer_handoff_routing_for_session(
                db_session,
                flow_uuid=flow_uuid,
                session_id=9001,
                contact_list_member_id=2001,
                list_validity_mode="link_date",
                list_validity_days=0,
            )
            assert missing_link is None

            row = (
                await db_session.execute(
                    text(
                        """
                        SELECT linked_actuator, list_validity
                          FROM contact_list_members
                         WHERE id = 2001
                        """
                    )
                )
            ).mappings().one()
            assert dict(row) == {"linked_actuator": None, "list_validity": None}
