from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.services.journey_metrics_service import record_journey_channel_action_event
from app.services.migration_service import _run_migration_file


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"


@pytest.mark.asyncio
async def test_real_digital_events_freeze_one_complete_dispatch_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = f"orch_metrics_dispatch_test_{uuid4().hex}"
    safe_schema = schema.replace('"', '""')
    flow_uuid = uuid4()
    revision_uuid = uuid4()
    session_uuid = uuid4()
    person_uuid = uuid4()
    component_uuid = uuid4()
    occurred_at = datetime(2026, 9, 29, 13, 0, tzinfo=UTC)
    enabled_settings = replace(
        get_settings(),
        orch_metrics_events_enabled=True,
        orch_metrics_events_workspace_allowlist=(WORKSPACE_UUID,),
    )
    monkeypatch.setattr(
        "app.services.journey_metrics_service.get_current_workspace_schema",
        lambda: schema,
    )
    monkeypatch.setattr(
        "app.services.alarm_service.get_current_workspace_schema",
        lambda: schema,
    )
    monkeypatch.setattr(
        "app.services.metrics_orchestration_event_service.get_current_workspace_uuid",
        lambda: WORKSPACE_UUID,
    )
    monkeypatch.setattr(
        "app.services.metrics_orchestration_event_service.get_settings",
        lambda: enabled_settings,
    )
    monkeypatch.setattr(
        "app.services.metrics_event_outbox_service.get_settings",
        lambda: enabled_settings,
    )

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        transaction = await db_session.begin()
        try:
            await db_session.execute(text(f'CREATE SCHEMA "{safe_schema}"'))
            await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
            await db_session.execute(
                text(
                    """
                    CREATE TABLE orch_sessions (
                        id BIGSERIAL PRIMARY KEY,
                        uuid UUID NOT NULL UNIQUE,
                        flow_uuid UUID NOT NULL,
                        entity TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_address TEXT NOT NULL,
                        runtime_variables JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TABLE flow_v2 (
                        id UUID PRIMARY KEY,
                        display_name TEXT
                    )
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TABLE flow_v2_revision (
                        id UUID PRIMARY KEY,
                        definition JSONB NOT NULL
                    )
                    """
                )
            )
            for migration_path in (
                "sql/023_create_orch_journey_metrics_tables.sql",
                "sql/024_create_orch_journey_workspace_snapshot_state.sql",
                "sql/025_create_orch_metrics_event_outbox.sql",
                "sql/026_create_orch_metrics_dispatch_snapshots.sql",
            ):
                await _run_migration_file(
                    db_session,
                    schema=schema,
                    migration_path=migration_path,
                )

            runtime = {
                "variables": {
                    "contact": {
                        "person_uuid": str(person_uuid),
                        "identifier": "34455521852",
                        "full_name": "Contato Canário",
                        "email": "canario@example.test",
                    }
                },
                "workflow_v2": {
                    "selected_contact_channel": {
                        "type": "voice",
                        "address": "5511975620806",
                    }
                },
            }
            source_session_id = (
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_sessions (
                            uuid, flow_uuid, entity, entity_type, entity_address,
                            runtime_variables
                        )
                        VALUES (
                            :session_uuid, :flow_uuid, '34455521852', 'person',
                            '5511975620806', CAST(:runtime AS jsonb)
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "session_uuid": session_uuid,
                        "flow_uuid": flow_uuid,
                        "runtime": json.dumps(runtime),
                    },
                )
            ).scalar_one()
            await db_session.execute(
                text(
                    """
                    INSERT INTO flow_v2 (id, display_name)
                    VALUES (:flow_uuid, 'Canário Digital')
                    """
                ),
                {"flow_uuid": flow_uuid},
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO flow_v2_revision (id, definition)
                    VALUES (:revision_uuid, CAST(:definition AS jsonb))
                    """
                ),
                {
                    "revision_uuid": revision_uuid,
                    "definition": json.dumps(
                        {
                            "components": [
                                {
                                    "ref_id": str(component_uuid),
                                    "component_id": "send_with_sms",
                                    "parameters": {"stage": "abordagem"},
                                }
                            ]
                        }
                    ),
                },
            )
            journey_session_id = (
                await db_session.execute(
                    text(
                        """
                        INSERT INTO orch_journey_sessions (
                            source_session_id, session_uuid, flow_uuid,
                            flow_revision_id, lifecycle_status,
                            current_component_ref_id, started_at, last_progress_at
                        )
                        VALUES (
                            :source_session_id, :session_uuid, :flow_uuid,
                            :revision_uuid, 'in_progress', :component_uuid,
                            :occurred_at, :occurred_at
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "source_session_id": source_session_id,
                        "session_uuid": session_uuid,
                        "flow_uuid": flow_uuid,
                        "revision_uuid": revision_uuid,
                        "component_uuid": component_uuid,
                        "occurred_at": occurred_at,
                    },
                )
            ).scalar_one()
            inbound_without_dispatch = await record_journey_channel_action_event(
                db_session,
                source_session_id=source_session_id,
                flow_uuid=str(flow_uuid),
                session_uuid=str(session_uuid),
                channel="whatsapp",
                source_kind="whatsapp_provider_message",
                source_id="inbound-without-context",
                native_status="message",
                event_id="inbound-without-context",
                occurred_at=occurred_at,
                component_ref_id=str(component_uuid),
                component_kind="send_whatsapp",
                prefer_existing_latest_action=True,
            )
            assert inbound_without_dispatch is None
            source_id = f"cdv2:sms:{uuid4().hex}"
            first = await record_journey_channel_action_event(
                db_session,
                source_session_id=source_session_id,
                flow_uuid=str(flow_uuid),
                session_uuid=str(session_uuid),
                channel="sms",
                source_kind="channel_supplier_v2_dispatch",
                source_id=source_id,
                native_status="accepted",
                event_id="provider-accepted-1",
                occurred_at=occurred_at,
                component_ref_id=str(component_uuid),
                component_kind="send_with_sms",
                provider_reference="provider-message-1",
                metadata={"provider_status": "13"},
            )
            assert first is not None

            runtime["workflow_v2"]["selected_contact_channel"]["address"] = (
                "5511999999999"
            )
            await db_session.execute(
                text(
                    """
                    UPDATE orch_sessions
                    SET entity_address = '5511999999999',
                        runtime_variables = CAST(:runtime AS jsonb)
                    WHERE id = :source_session_id
                    """
                ),
                {
                    "source_session_id": source_session_id,
                    "runtime": json.dumps(runtime),
                },
            )
            await record_journey_channel_action_event(
                db_session,
                source_session_id=source_session_id,
                flow_uuid=str(flow_uuid),
                session_uuid=str(session_uuid),
                channel="sms",
                source_kind="channel_supplier_v2_dispatch",
                source_id=source_id,
                native_status="delivered",
                event_id="provider-delivered-1",
                occurred_at=occurred_at + timedelta(seconds=5),
                component_ref_id=str(component_uuid),
                component_kind="send_with_sms",
                provider_reference="provider-message-1",
                metadata={"provider_status": "DELIVRD"},
            )
            await record_journey_channel_action_event(
                db_session,
                source_session_id=source_session_id,
                flow_uuid=str(flow_uuid),
                session_uuid=str(session_uuid),
                channel="sms",
                source_kind="channel_supplier_v2_dispatch",
                source_id=source_id,
                native_status="delivered",
                event_id="provider-delivered-1",
                occurred_at=occurred_at + timedelta(seconds=5),
                component_ref_id=str(component_uuid),
                component_kind="send_with_sms",
                provider_reference="provider-message-1",
                metadata={"provider_status": "DELIVRD"},
            )
            await record_journey_channel_action_event(
                db_session,
                source_session_id=source_session_id,
                flow_uuid=str(flow_uuid),
                session_uuid=str(session_uuid),
                channel="sms",
                source_kind="channel_supplier_v2_dispatch",
                source_id=source_id,
                native_status="limit_reached",
                event_id="internal-limit-1",
                occurred_at=occurred_at + timedelta(seconds=6),
                component_ref_id=str(component_uuid),
                component_kind="send_with_sms",
            )

            rows = (
                await db_session.execute(
                    text(
                        """
                        SELECT idempotency_key, envelope
                        FROM orch_metrics_event_outbox
                        WHERE event_type = 'flow.dispatch.updated.v1'
                        ORDER BY id
                        """
                    )
                )
            ).mappings().all()
            assert len(rows) == 2
            payloads = [row["envelope"]["payload"] for row in rows]
            assert [payload["status"] for payload in payloads] == [
                "sent",
                "delivered",
            ]
            assert payloads[0]["dispatch_id"] == payloads[1]["dispatch_id"]
            assert payloads[0]["dispatched_at"] == payloads[1]["dispatched_at"]
            assert payloads[0]["destination"] == "+5511975620806"
            assert payloads[1]["destination"] == "+5511975620806"
            assert payloads[1]["contact_name"] == "Contato Canário"
            assert payloads[1]["contact_identifier"] == "34455521852"
            assert payloads[1]["flow_name"] == "Canário Digital"
            assert payloads[1]["provider_status"] == "DELIVRD"
            assert rows[0]["envelope"]["context"] == {
                "flow_id": str(flow_uuid),
                "flow_type": "orchestration",
                "node_id": str(component_uuid),
                "interaction_id": str(session_uuid),
                "contact_id": str(person_uuid),
                "channel": "SMS",
            }
            snapshot_count = (
                await db_session.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM orch_metrics_dispatch_snapshots
                        WHERE action_id = CAST(:action_id AS uuid)
                        """
                    ),
                    {"action_id": first.action_id},
                )
            ).scalar_one()
            assert snapshot_count == 1
            assert journey_session_id > 0

            whatsapp_prepared_at = occurred_at + timedelta(seconds=7)
            whatsapp_sent_at = occurred_at + timedelta(seconds=9)
            whatsapp_delivered_at = occurred_at + timedelta(seconds=10)
            runtime["workflow_v2"]["selected_contact_channel"] = {
                "type": "whatsapp",
                "address": "5511975620806",
            }
            runtime["whatsapp_hsm_outbound"] = {
                "component_ref_id": str(component_uuid),
                "prepared_at": whatsapp_prepared_at.isoformat(),
                "template_name": "template_canario",
            }
            await db_session.execute(
                text(
                    """
                    UPDATE orch_sessions
                    SET runtime_variables = CAST(:runtime AS jsonb)
                    WHERE id = :source_session_id
                    """
                ),
                {
                    "source_session_id": source_session_id,
                    "runtime": json.dumps(runtime),
                },
            )
            whatsapp_action = await record_journey_channel_action_event(
                db_session,
                source_session_id=source_session_id,
                flow_uuid=str(flow_uuid),
                session_uuid=str(session_uuid),
                channel="whatsapp",
                source_kind="whatsapp_provider_message",
                source_id="wamid.out-of-order",
                native_status="delivered",
                event_id="wamid.out-of-order",
                occurred_at=whatsapp_delivered_at,
                component_ref_id=str(component_uuid),
                component_kind="send_whatsapp",
                provider_reference="wamid.out-of-order",
                metadata={"provider_status": "delivered"},
            )
            assert whatsapp_action is not None
            await record_journey_channel_action_event(
                db_session,
                source_session_id=source_session_id,
                flow_uuid=str(flow_uuid),
                session_uuid=str(session_uuid),
                channel="whatsapp",
                source_kind="whatsapp_provider_message",
                source_id="wamid.out-of-order",
                native_status="sent",
                event_id="wamid.out-of-order",
                occurred_at=whatsapp_sent_at,
                component_ref_id=str(component_uuid),
                component_kind="send_whatsapp",
                provider_reference="wamid.out-of-order",
                metadata={"provider_status": "sent"},
            )

            whatsapp_rows = (
                await db_session.execute(
                    text(
                        """
                        SELECT envelope
                        FROM orch_metrics_event_outbox
                        WHERE event_type = 'flow.dispatch.updated.v1'
                          AND envelope #>> '{payload,dispatch_id}' = :dispatch_id
                        ORDER BY id
                        """
                    ),
                    {"dispatch_id": whatsapp_action.action_id},
                )
            ).scalars().all()
            assert [row["payload"]["status"] for row in whatsapp_rows] == [
                "delivered",
                "sent",
            ]
            expected_dispatched_at = whatsapp_sent_at.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            assert {
                row["payload"]["dispatched_at"] for row in whatsapp_rows
            } == {expected_dispatched_at}
            whatsapp_snapshot = (
                await db_session.execute(
                    text(
                        """
                        SELECT dispatched_at, snapshot
                        FROM orch_metrics_dispatch_snapshots
                        WHERE action_id = CAST(:action_id AS uuid)
                        """
                    ),
                    {"action_id": whatsapp_action.action_id},
                )
            ).mappings().one()
            assert whatsapp_snapshot["dispatched_at"] == whatsapp_sent_at
            assert (
                whatsapp_snapshot["snapshot"]["dispatched_at"]
                == expected_dispatched_at
            )
        finally:
            await transaction.rollback()
