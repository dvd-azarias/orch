from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.repositories.identidade_person_repository import (
    set_person_materialized_membership_state,
)


@pytest.mark.asyncio
async def test_membership_state_is_idempotent_and_scoped_in_real_postgres() -> None:
    flow_uuid = str(uuid4())
    other_flow_uuid = str(uuid4())
    contact_list_id = str(uuid4())
    other_contact_list_id = str(uuid4())
    person_uuid = str(uuid4())
    other_person_uuid = str(uuid4())
    contact_draft_id = str(uuid4())
    other_contact_draft_id = str(uuid4())
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE contact_list_members (
                        id bigint PRIMARY KEY,
                        status integer DEFAULT 0,
                        contact_identifier text,
                        contact_draft_id uuid,
                        contact_list_id uuid,
                        mailing_id bigint,
                        person_uuid uuid,
                        deleted_at timestamptz,
                        unassigned_at timestamptz,
                        last_ref_id uuid,
                        linked_actuator text,
                        ani text,
                        next_candidate integer DEFAULT 0,
                        failure_attempts integer DEFAULT 0,
                        busy_attempts integer DEFAULT 0,
                        noanswer_attempts integer DEFAULT 0,
                        machine_attempts integer DEFAULT 0,
                        rejected_attempts integer DEFAULT 0,
                        invalidnumber_attempts integer DEFAULT 0,
                        external_identifier text,
                        scheduling_moment timestamptz,
                        segment text,
                        updated_at timestamptz
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_sessions (
                        id bigint PRIMARY KEY,
                        flow_uuid uuid NOT NULL,
                        state smallint NOT NULL DEFAULT 0,
                        entity text NOT NULL,
                        runtime_variables jsonb NOT NULL DEFAULT '{}'::jsonb,
                        ended_at timestamptz,
                        unassigned_at timestamptz,
                        updated_at timestamptz
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO contact_list_members (
                        id, status, contact_identifier, contact_draft_id,
                        contact_list_id, mailing_id, person_uuid, linked_actuator
                    ) VALUES
                        (10, 1, '12345678901', CAST(:contact_draft_id AS uuid),
                         CAST(:contact_list_id AS uuid), 1139, CAST(:person_uuid AS uuid), 'sms'),
                        (11, 0, '12345678901', CAST(:contact_draft_id AS uuid),
                         CAST(:contact_list_id AS uuid), 1139, CAST(:person_uuid AS uuid), NULL),
                        (12, 0, '12345678901', CAST(:contact_draft_id AS uuid),
                         CAST(:other_contact_list_id AS uuid), 1139, CAST(:person_uuid AS uuid), NULL),
                        (13, 0, '99999999999', CAST(:other_contact_draft_id AS uuid),
                         CAST(:contact_list_id AS uuid), 1139, CAST(:other_person_uuid AS uuid), NULL)
                    """
                ),
                {
                    "contact_draft_id": contact_draft_id,
                    "other_contact_draft_id": other_contact_draft_id,
                    "contact_list_id": contact_list_id,
                    "other_contact_list_id": other_contact_list_id,
                    "person_uuid": person_uuid,
                    "other_person_uuid": other_person_uuid,
                },
            )

            def runtime(member_id: int, list_id: str = contact_list_id) -> str:
                return json.dumps(
                    {
                        "input_payload": {
                            "contact_list_member_id": member_id,
                            "contact_list_id": list_id,
                            "mailing_id": 1139,
                        },
                        "session_identity": {"contact_list_member_id": member_id},
                    }
                )

            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (id, flow_uuid, entity, runtime_variables)
                    VALUES
                        (100, CAST(:flow_uuid AS uuid), '12345678901', CAST(:current_runtime AS jsonb)),
                        (101, CAST(:flow_uuid AS uuid), '12345678901', CAST(:sibling_runtime AS jsonb)),
                        (102, CAST(:flow_uuid AS uuid), '12345678901', CAST(:other_list_runtime AS jsonb)),
                        (103, CAST(:other_flow_uuid AS uuid), '12345678901', CAST(:sibling_runtime AS jsonb)),
                        (104, CAST(:flow_uuid AS uuid), '99999999999', CAST(:other_person_runtime AS jsonb)),
                        (105, CAST(:flow_uuid AS uuid), '12345678901', CAST(:sibling_runtime AS jsonb))
                    """
                ),
                {
                    "flow_uuid": flow_uuid,
                    "other_flow_uuid": other_flow_uuid,
                    "current_runtime": runtime(10),
                    "sibling_runtime": runtime(11),
                    "other_list_runtime": runtime(12, other_contact_list_id),
                    "other_person_runtime": runtime(13),
                },
            )
            await db_session.execute(
                text(
                    """
                    UPDATE orch_sessions
                    SET state = 3, ended_at = NOW()
                    WHERE id = 105
                    """
                )
            )

            common = {
                "flow_uuid": flow_uuid,
                "current_session_id": 100,
                "source_list_id": 1139,
                "contact_list_id": contact_list_id,
                "person_uuid": person_uuid,
                "contact_draft_id": contact_draft_id,
                "identifier": "12345678901",
            }
            first = await set_person_materialized_membership_state(
                db_session, **common, desired_state="inactive"
            )
            second = await set_person_materialized_membership_state(
                db_session, **common, desired_state="inactive"
            )

            assert first == {
                "contact_list_id": contact_list_id,
                "previous_state": "active",
                "matched_members": 2,
                "members_changed": 2,
                "sessions_stopped": 1,
            }
            assert second == {
                "contact_list_id": contact_list_id,
                "previous_state": "inactive",
                "matched_members": 2,
                "members_changed": 0,
                "sessions_stopped": 0,
            }

            member_rows = (
                await db_session.execute(
                    text(
                        """
                        SELECT id, unassigned_at IS NOT NULL AS inactive
                        FROM contact_list_members
                        ORDER BY id
                        """
                    )
                )
            ).mappings().all()
            assert [dict(row) for row in member_rows] == [
                {"id": 10, "inactive": True},
                {"id": 11, "inactive": True},
                {"id": 12, "inactive": False},
                {"id": 13, "inactive": False},
            ]

            session_rows = (
                await db_session.execute(
                    text(
                        """
                        SELECT id, state, unassigned_at IS NOT NULL AS inactive
                        FROM orch_sessions
                        ORDER BY id
                        """
                    )
                )
            ).mappings().all()
            assert [dict(row) for row in session_rows] == [
                {"id": 100, "state": 0, "inactive": False},
                {"id": 101, "state": 5, "inactive": True},
                {"id": 102, "state": 0, "inactive": False},
                {"id": 103, "state": 0, "inactive": False},
                {"id": 104, "state": 0, "inactive": False},
                {"id": 105, "state": 3, "inactive": False},
            ]

            reactivated = await set_person_materialized_membership_state(
                db_session, **common, desired_state="active"
            )
            assert reactivated == {
                "contact_list_id": contact_list_id,
                "previous_state": "inactive",
                "matched_members": 2,
                "members_changed": 2,
                "sessions_stopped": 0,
            }

            sibling = (
                await db_session.execute(
                    text("SELECT state, unassigned_at FROM orch_sessions WHERE id = 101")
                )
            ).mappings().one()
            assert sibling["state"] == 5
            assert sibling["unassigned_at"] is not None
