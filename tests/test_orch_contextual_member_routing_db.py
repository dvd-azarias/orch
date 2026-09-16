from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.repositories.orch_channel_events_repository import (
    discard_pending_channel_events,
)
from app.repositories.orch_sessions_repository import (
    apply_dialer_supplier_v2_terminal_callback,
    assign_dialer_handoff_routing_for_session,
    assign_dialer_routing_for_session,
    assign_whatsapp_routing_for_session,
    fetch_contact_runtime_context_for_session,
    patch_session_dialer_supplier_v2_registration,
)


@pytest.mark.asyncio
async def test_supplier_v2_discards_only_pending_raw_dialer_callbacks() -> None:
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_channel_events (
                        id BIGINT PRIMARY KEY,
                        session_id BIGINT NOT NULL,
                        channel TEXT NOT NULL,
                        processed_at TIMESTAMPTZ NULL,
                        discard_reason TEXT NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_channel_events (
                        id, session_id, channel, processed_at
                    ) VALUES
                        (1, 9101, 'dialer', NULL),
                        (2, 9101, 'whatsapp', NULL),
                        (3, 9101, 'dialer', NOW())
                    """
                )
            )

            discarded = await discard_pending_channel_events(
                db_session,
                session_id=9101,
                channel="dialer",
                discard_reason="supplier_v2_nonterminal_raw_callback",
            )
            rows = (
                await db_session.execute(
                    text(
                        """
                        SELECT id, processed_at IS NOT NULL AS processed,
                               discard_reason
                          FROM orch_channel_events
                         ORDER BY id
                        """
                    )
                )
            ).mappings().all()

            assert discarded == 1
            assert dict(rows[0]) == {
                "id": 1,
                "processed": True,
                "discard_reason": "supplier_v2_nonterminal_raw_callback",
            }
            assert dict(rows[1]) == {
                "id": 2,
                "processed": False,
                "discard_reason": None,
            }
            assert dict(rows[2]) == {
                "id": 3,
                "processed": True,
                "discard_reason": None,
            }


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
            assert assignment["contact_list_id"] == contact_list_uuid
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


@pytest.mark.asyncio
async def test_supplier_v2_registration_patch_preserves_runtime_and_terminal() -> None:
    session_factory = get_session_factory()
    idempotency_key = "orch:v2:dial-cycle:" + ("a" * 64)

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_sessions (
                        id BIGINT PRIMARY KEY,
                        runtime_variables JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (id, runtime_variables)
                    VALUES (
                        9002,
                        CAST(:runtime_variables AS jsonb)
                    )
                    """
                ),
                {
                    "runtime_variables": (
                        "{"
                        '"callbacks_pending":[{"event_id":"evt-1"}],'
                        '"workflow_v2":{"blocking_stop_reason":'
                        '"blocked_send_with_dialer_handoff",'
                        '"dialer_supplier_v2":{"idempotency_key":"'
                        + idempotency_key
                        + '","status":"pending"}}}'
                    )
                },
            )

            updated = await patch_session_dialer_supplier_v2_registration(
                db_session,
                session_id=9002,
                idempotency_key=idempotency_key,
                registration={
                    "idempotency_key": idempotency_key,
                    "status": "ready",
                    "cycle_id": "11111111-1111-4111-8111-111111111111",
                },
            )
            stale = await patch_session_dialer_supplier_v2_registration(
                db_session,
                session_id=9002,
                idempotency_key="orch:v2:dial-cycle:" + ("b" * 64),
                registration={"status": "failed"},
            )
            runtime = (
                await db_session.execute(
                    text(
                        """
                        SELECT runtime_variables
                          FROM orch_sessions
                         WHERE id = 9002
                        """
                    )
                )
            ).scalar_one()

            assert updated is True
            assert stale is False
            assert runtime["callbacks_pending"] == [{"event_id": "evt-1"}]
            assert (
                runtime["workflow_v2"]["blocking_stop_reason"]
                == "blocked_send_with_dialer_handoff"
            )
            assert runtime["workflow_v2"]["dialer_supplier_v2"] == {
                "idempotency_key": idempotency_key,
                "status": "ready",
                "cycle_id": "11111111-1111-4111-8111-111111111111",
            }

            terminal_registration = {
                "idempotency_key": idempotency_key,
                "status": "terminal_received",
                "cycle_id": "11111111-1111-4111-8111-111111111111",
                "terminal_delivery": {
                    "event_id": "22222222-2222-4222-8222-222222222222",
                    "outcome": "answered",
                },
            }
            await db_session.execute(
                text(
                    """
                    UPDATE orch_sessions
                       SET runtime_variables = jsonb_set(
                            runtime_variables,
                            '{workflow_v2,dialer_supplier_v2}',
                            CAST(:registration AS jsonb),
                            true
                       )
                     WHERE id = 9002
                    """
                ),
                {"registration": json.dumps(terminal_registration)},
            )
            terminal_overwrite = (
                await patch_session_dialer_supplier_v2_registration(
                    db_session,
                    session_id=9002,
                    idempotency_key=idempotency_key,
                    registration={
                        "idempotency_key": idempotency_key,
                        "status": "failed",
                    },
                )
            )
            protected_runtime = (
                await db_session.execute(
                    text(
                        """
                        SELECT runtime_variables
                          FROM orch_sessions
                         WHERE id = 9002
                        """
                    )
                )
            ).scalar_one()

            assert terminal_overwrite is False
            assert (
                protected_runtime["workflow_v2"]["dialer_supplier_v2"]
                == terminal_registration
            )


@pytest.mark.asyncio
async def test_supplier_v2_terminal_callback_is_pinned_and_idempotent() -> None:
    flow_uuid = str(uuid4())
    session_uuid = str(uuid4())
    revision_uuid = str(uuid4())
    card_uuid = str(uuid4())
    cycle_uuid = str(uuid4())
    attempt_uuid = str(uuid4())
    event_uuid = str(uuid4())
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_sessions (
                        id BIGINT PRIMARY KEY,
                        uuid UUID NOT NULL,
                        flow_uuid UUID NOT NULL,
                        state INTEGER NOT NULL,
                        ended_at TIMESTAMPTZ NULL,
                        unassigned_at TIMESTAMPTZ NULL,
                        runtime_variables JSONB NOT NULL,
                        last_card_uuid UUID NULL,
                        next_card_uuid UUID NULL,
                        frozen_until TIMESTAMPTZ NULL,
                        updated_at TIMESTAMPTZ NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            registration = {
                "status": "ready",
                "cycle_id": cycle_uuid,
                "session_uuid": session_uuid,
                "flow_uuid": flow_uuid,
                "flow_revision_id": revision_uuid,
                "component_ref_id": card_uuid,
            }
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (
                        id, uuid, flow_uuid, state, runtime_variables,
                        last_card_uuid, next_card_uuid
                    ) VALUES (
                        9101, CAST(:session_uuid AS uuid), CAST(:flow_uuid AS uuid), 1,
                        CAST(:runtime_variables AS jsonb),
                        CAST(:card_uuid AS uuid), CAST(:card_uuid AS uuid)
                    )
                    """
                ),
                {
                    "session_uuid": session_uuid,
                    "flow_uuid": flow_uuid,
                    "card_uuid": card_uuid,
                    "runtime_variables": json.dumps(
                        {
                            "workflow_v2": {
                                "blocking_execution": True,
                                "blocking_stop_reason": "blocked_send_with_dialer_handoff",
                                "dialer_supplier_v2": registration,
                            }
                        }
                    ),
                },
            )
            payload = {
                "event_id": event_uuid,
                "cycle_id": cycle_uuid,
                "attempt_id": attempt_uuid,
                "session_uuid": session_uuid,
                "flow_uuid": flow_uuid,
                "flow_revision_id": revision_uuid,
                "component_ref_id": card_uuid,
                "outcome": "answered",
                "terminal": True,
                "terminal_reason": "answered",
                "contact_list_member_id": 123,
                "dial_profile_id": str(uuid4()),
                "dial_profile_revision_id": str(uuid4()),
                "attempt_policy_id": str(uuid4()),
                "decision": "finish_person",
                "decision_source": "telephone_outcome",
                "decision_effective_until": None,
                "release_mapping_version": "pdial_v1",
                "occurred_at": "2026-09-15T13:03:13+00:00",
            }

            first = await apply_dialer_supplier_v2_terminal_callback(
                db_session,
                flow_uuid=flow_uuid,
                callback_payload=payload,
            )
            replay = await apply_dialer_supplier_v2_terminal_callback(
                db_session,
                flow_uuid=flow_uuid,
                callback_payload=payload,
            )
            mapping_conflict = await apply_dialer_supplier_v2_terminal_callback(
                db_session,
                flow_uuid=flow_uuid,
                callback_payload={**payload, "release_mapping_version": None},
            )
            conflict = await apply_dialer_supplier_v2_terminal_callback(
                db_session,
                flow_uuid=flow_uuid,
                callback_payload={**payload, "event_id": str(uuid4()), "outcome": "busy"},
            )
            runtime = (
                await db_session.execute(
                    text(
                        "SELECT runtime_variables FROM orch_sessions WHERE id = 9101"
                    )
                )
            ).scalar_one()

            assert first is not None and first["accepted"] is True
            assert first["idempotent"] is False
            assert replay is not None and replay["idempotent"] is True
            assert mapping_conflict is not None
            assert mapping_conflict["status"] == "terminal_conflict"
            assert conflict is not None and conflict["status"] == "terminal_conflict"
            terminal = runtime["workflow_v2"]["dialer_supplier_v2"][
                "terminal_delivery"
            ]
            assert terminal["event_id"] == event_uuid
            assert terminal["outcome"] == "answered"
            assert terminal["decision"] == "finish_person"
            assert terminal["decision_source"] == "telephone_outcome"
            assert terminal["contact_list_member_id"] == 123
            assert terminal["release_mapping_version"] == "pdial_v1"


@pytest.mark.asyncio
async def test_supplier_v2_archived_callback_never_resumes_active_second_card() -> None:
    flow_uuid = str(uuid4())
    session_uuid = str(uuid4())
    revision_uuid = str(uuid4())
    card_a = str(uuid4())
    card_b = str(uuid4())
    cycle_a = str(uuid4())
    cycle_b = str(uuid4())
    session_factory = get_session_factory()

    registration_a = {
        "status": "ready",
        "cycle_id": cycle_a,
        "session_uuid": session_uuid,
        "flow_uuid": flow_uuid,
        "flow_revision_id": revision_uuid,
        "component_ref_id": card_a,
    }
    registration_b = {
        "status": "ready",
        "cycle_id": cycle_b,
        "session_uuid": session_uuid,
        "flow_uuid": flow_uuid,
        "flow_revision_id": revision_uuid,
        "component_ref_id": card_b,
    }

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE orch_sessions (
                        id BIGINT PRIMARY KEY,
                        uuid UUID NOT NULL,
                        flow_uuid UUID NOT NULL,
                        state INTEGER NOT NULL,
                        ended_at TIMESTAMPTZ NULL,
                        unassigned_at TIMESTAMPTZ NULL,
                        runtime_variables JSONB NOT NULL,
                        last_card_uuid UUID NULL,
                        next_card_uuid UUID NULL,
                        frozen_until TIMESTAMPTZ NULL,
                        updated_at TIMESTAMPTZ NULL
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (
                        id, uuid, flow_uuid, state, runtime_variables,
                        last_card_uuid, next_card_uuid
                    ) VALUES (
                        9102, CAST(:session_uuid AS uuid), CAST(:flow_uuid AS uuid), 1,
                        CAST(:runtime_variables AS jsonb),
                        CAST(:card_b AS uuid), CAST(:card_b AS uuid)
                    )
                    """
                ),
                {
                    "session_uuid": session_uuid,
                    "flow_uuid": flow_uuid,
                    "card_b": card_b,
                    "runtime_variables": json.dumps(
                        {
                            "workflow_v2": {
                                "blocking_execution": True,
                                "blocking_stop_reason": "blocked_send_with_dialer_handoff",
                                "dialer_supplier_v2": registration_b,
                                "dialer_supplier_v2_history": {
                                    "a" * 64: registration_a,
                                },
                            }
                        }
                    ),
                },
            )

            callback_a = {
                "event_id": str(uuid4()),
                "cycle_id": cycle_a,
                "attempt_id": str(uuid4()),
                "session_uuid": session_uuid,
                "flow_uuid": flow_uuid,
                "flow_revision_id": revision_uuid,
                "component_ref_id": card_a,
                "outcome": "busy",
                "terminal": True,
                "terminal_reason": "busy",
                "occurred_at": "2026-09-15T13:03:13+00:00",
            }
            archived = await apply_dialer_supplier_v2_terminal_callback(
                db_session,
                flow_uuid=flow_uuid,
                callback_payload=callback_a,
            )
            runtime = (
                await db_session.execute(
                    text(
                        "SELECT runtime_variables FROM orch_sessions WHERE id = 9102"
                    )
                )
            ).scalar_one()

            assert archived is not None
            assert archived["accepted"] is True
            assert archived["idempotent"] is False
            assert archived["resume_required"] is False
            assert (
                runtime["workflow_v2"]["dialer_supplier_v2"]["cycle_id"]
                == cycle_b
            )
            archived_cycle = next(
                iter(
                    runtime["workflow_v2"][
                        "dialer_supplier_v2_history"
                    ].values()
                )
            )
            assert archived_cycle["status"] == "terminal_received"
            assert archived_cycle["terminal_delivery"]["cycle_id"] == cycle_a
