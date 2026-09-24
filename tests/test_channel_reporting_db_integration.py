from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.repositories.orch_channel_reporting_repository import (
    activate_channel_reporting,
    link_channel_event_to_action,
    register_channel_action,
    update_channel_action,
)


@pytest.mark.asyncio
async def test_reporting_cutover_action_and_out_of_order_callback_in_real_postgres() -> None:
    session_uuid = str(uuid4())
    flow_uuid = str(uuid4())
    revision_uuid = str(uuid4())
    person_uuid = str(uuid4())
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        async with db_session.begin():
            table_statements = [
                """
                    CREATE TEMP TABLE orch_channel_reporting_state (
                        singleton_id SMALLINT PRIMARY KEY,
                        status TEXT NOT NULL,
                        coverage_started_at TIMESTAMPTZ,
                        activated_at TIMESTAMPTZ,
                        activated_by TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    ) ON COMMIT DROP
                """,
                """
                    CREATE TEMP TABLE orch_sessions (
                        id BIGINT PRIMARY KEY,
                        uuid UUID NOT NULL,
                        flow_uuid UUID NOT NULL
                    ) ON COMMIT DROP
                """,
                """
                    CREATE TEMP TABLE orch_channel_actions (
                        id BIGSERIAL PRIMARY KEY,
                        uuid UUID NOT NULL DEFAULT gen_random_uuid(),
                        session_id BIGINT NOT NULL,
                        session_uuid UUID NOT NULL,
                        flow_uuid UUID NOT NULL,
                        flow_revision_id UUID NOT NULL,
                        component_ref_id TEXT NOT NULL,
                        component_kind TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        action_sequence INTEGER NOT NULL,
                        source_kind TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        person_uuid UUID,
                        destination_masked TEXT,
                        provider_reference TEXT,
                        lifecycle_status TEXT NOT NULL,
                        native_outcome TEXT,
                        native_outcome_at TIMESTAMPTZ,
                        requested_at TIMESTAMPTZ NOT NULL,
                        queued_at TIMESTAMPTZ,
                        accepted_at TIMESTAMPTZ,
                        sent_at TIMESTAMPTZ,
                        delivered_at TIMESTAMPTZ,
                        engaged_at TIMESTAMPTZ,
                        failed_at TIMESTAMPTZ,
                        terminal_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (source_kind, source_id),
                        UNIQUE (
                            session_uuid,
                            flow_revision_id,
                            component_ref_id,
                            channel,
                            action_sequence
                        )
                    ) ON COMMIT DROP
                """,
                """
                    CREATE TEMP TABLE orch_channel_events (
                        id BIGINT PRIMARY KEY,
                        session_id BIGINT NOT NULL,
                        flow_uuid UUID NOT NULL,
                        action_id BIGINT
                    ) ON COMMIT DROP
                """,
            ]
            for statement in table_statements:
                await db_session.execute(text(statement))
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_channel_reporting_state (singleton_id, status)
                    VALUES (1, 'pending')
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (id, uuid, flow_uuid)
                    VALUES (101, CAST(:session_uuid AS uuid), CAST(:flow_uuid AS uuid))
                    """
                ),
                {"session_uuid": session_uuid, "flow_uuid": flow_uuid},
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_channel_events (id, session_id, flow_uuid)
                    VALUES (9001, 101, CAST(:flow_uuid AS uuid))
                    """
                ),
                {"flow_uuid": flow_uuid},
            )

            state = await activate_channel_reporting(
                db_session,
                activated_by="pytest",
            )
            requested_at = state["coverage_started_at"] + timedelta(milliseconds=1)

            action = await register_channel_action(
                db_session,
                session_id=101,
                session_uuid=session_uuid,
                flow_uuid=flow_uuid,
                flow_revision_id=revision_uuid,
                component_ref_id="send-whatsapp-1",
                component_kind="send_with_whatsapp_template",
                channel="whatsapp",
                action_sequence=1,
                source_kind="whatsapp_outbound",
                source_id="wamid.reporting.001",
                person_uuid=person_uuid,
                destination_masked="********0806",
                provider_reference="wamid.reporting.001",
                lifecycle_status="accepted",
                requested_at=requested_at,
            )
            duplicate = await register_channel_action(
                db_session,
                session_id=101,
                session_uuid=session_uuid,
                flow_uuid=flow_uuid,
                flow_revision_id=revision_uuid,
                component_ref_id="send-whatsapp-1",
                component_kind="send_with_whatsapp_template",
                channel="whatsapp",
                action_sequence=1,
                source_kind="whatsapp_outbound",
                source_id="wamid.reporting.001",
                person_uuid=person_uuid,
                destination_masked="********0806",
                provider_reference="wamid.reporting.001",
                lifecycle_status="accepted",
                requested_at=requested_at,
            )

            assert action is not None
            assert duplicate is not None
            assert action["id"] == duplicate["id"]

            await update_channel_action(
                db_session,
                source_kind="whatsapp_outbound",
                source_id="wamid.reporting.001",
                lifecycle_status="in_progress",
                milestone="sent",
                occurred_at=requested_at + timedelta(seconds=1),
                native_outcome="sent",
            )
            await update_channel_action(
                db_session,
                source_kind="whatsapp_outbound",
                source_id="wamid.reporting.001",
                lifecycle_status="completed",
                milestone="engaged",
                occurred_at=requested_at + timedelta(seconds=3),
                native_outcome="read",
            )
            final_action = await update_channel_action(
                db_session,
                source_kind="whatsapp_outbound",
                source_id="wamid.reporting.001",
                lifecycle_status="completed",
                milestone="delivered",
                occurred_at=requested_at + timedelta(seconds=2),
                native_outcome="delivered",
            )
            missing_action = await update_channel_action(
                db_session,
                source_kind="whatsapp_outbound",
                source_id="wamid.reporting.unknown",
                lifecycle_status="completed",
                milestone="delivered",
                occurred_at=requested_at + timedelta(seconds=4),
                native_outcome="delivered",
            )
            linked = await link_channel_event_to_action(
                db_session,
                event_row_id=9001,
                action_id=int(action["id"]),
            )

            action_count = await db_session.scalar(text("SELECT COUNT(*) FROM orch_channel_actions"))
            event_action_id = await db_session.scalar(
                text("SELECT action_id FROM orch_channel_events WHERE id = 9001")
            )

            assert action_count == 1
            assert final_action is not None
            assert final_action["lifecycle_status"] == "completed"
            assert final_action["native_outcome"] == "read"
            assert final_action["delivered_at"] == requested_at + timedelta(seconds=2)
            assert final_action["engaged_at"] == requested_at + timedelta(seconds=3)
            assert missing_action is None
            assert linked is True
            assert event_action_id == action["id"]
