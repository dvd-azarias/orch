from __future__ import annotations

from datetime import UTC, datetime

from app.services.metrics_orchestration_event_service import (
    _canonical_dispatch_status,
    _dispatch_duration_seconds,
    _resolve_dispatched_at,
    resolve_metrics_contact_snapshot,
)


def test_contact_identity_prefers_person_then_member_then_draft_then_external() -> None:
    row = {
        "entity": "external-123",
        "entity_type": "person",
        "entity_address": "5511999999999",
        "runtime_variables": {
            "session_identity": {"contact_list_member_id": 77},
            "input_payload": {"contact_draft_id": "draft-9"},
            "variables": {
                "contact": {
                    "person_uuid": "5b03521c-b81c-4c2e-ad31-9e11ad90a42b",
                    "identifier": "cpf-123",
                }
            },
        },
    }
    snapshot = resolve_metrics_contact_snapshot(row)
    assert snapshot["contact_id"] == "5b03521c-b81c-4c2e-ad31-9e11ad90a42b"

    del row["runtime_variables"]["variables"]
    snapshot = resolve_metrics_contact_snapshot(row)
    assert snapshot["contact_id"] == "77"

    del row["runtime_variables"]["session_identity"]
    snapshot = resolve_metrics_contact_snapshot(row)
    assert snapshot["contact_id"] == "draft-9"

    del row["runtime_variables"]["input_payload"]
    snapshot = resolve_metrics_contact_snapshot(row)
    assert snapshot["contact_id"] == "external-123"


def test_contact_snapshot_prefers_selected_channel_and_normalizes_voice() -> None:
    row = {
        "entity": "external-123",
        "entity_type": "person",
        "entity_address": "5511000000000",
        "runtime_variables": {
            "workflow_v2": {
                "selected_contact_channel": {
                    "selected": True,
                    "person_uuid": "5b03521c-b81c-4c2e-ad31-9e11ad90a42b",
                    "contact_list_member_id": 77,
                    "type": "phone",
                    "address": "5511999999999",
                }
            }
        },
    }
    snapshot = resolve_metrics_contact_snapshot(row)
    assert snapshot["channel"] == "VOZ"
    assert snapshot["destination"] == "5511999999999"


def test_contact_snapshot_prefers_source_person_resolved_before_session_start() -> None:
    row = {
        "source_person_uuid": "d8cfc3a5-0290-48a6-b935-7421683b84d5",
        "entity": "external-123",
        "runtime_variables": {
            "session_identity": {"contact_list_member_id": 77},
            "variables": {
                "contact": {
                    "person_uuid": "5b03521c-b81c-4c2e-ad31-9e11ad90a42b",
                }
            },
        },
    }

    snapshot = resolve_metrics_contact_snapshot(row)

    assert snapshot["contact_id"] == "d8cfc3a5-0290-48a6-b935-7421683b84d5"


def test_digital_dispatch_statuses_follow_metrics_contract() -> None:
    assert _canonical_dispatch_status(channel="whatsapp", native_status="sent") == "sent"
    assert _canonical_dispatch_status(channel="whatsapp", native_status="read") == "read"
    assert (
        _canonical_dispatch_status(channel="whatsapp", native_status="message:sim")
        == "replied"
    )
    assert _canonical_dispatch_status(channel="sms", native_status="accepted") == "sent"
    assert _canonical_dispatch_status(channel="sms", native_status="response") == "replied"
    assert _canonical_dispatch_status(channel="sms", native_status="not_delivered") == "failed"
    assert _canonical_dispatch_status(channel="rcs", native_status="accepted") == "sent"
    assert _canonical_dispatch_status(channel="rcs", native_status="read") == "read"
    assert _canonical_dispatch_status(channel="rcs", native_status="unavailable") == "failed"


def test_internal_limit_does_not_become_provider_failure() -> None:
    assert _canonical_dispatch_status(channel="sms", native_status="limit_reached") is None
    assert _canonical_dispatch_status(channel="rcs", native_status="limit_reached") is None
    assert _canonical_dispatch_status(channel="whatsapp", native_status="limit_reached") is None


def test_voice_dispatch_statuses_are_exactly_the_pdial_taxonomy() -> None:
    expected = {
        "dialing": "dialing",
        "machine": "machine",
        "no_answer": "no_answer",
        "rejected": "rejected",
        "answered": "answered",
        "busy": "busy",
        "invalid_number": "invalid_number",
        "failed": "failed",
    }
    assert {
        status: _canonical_dispatch_status(channel="voice", native_status=status)
        for status in expected
    } == expected
    assert _canonical_dispatch_status(channel="voice", native_status="no_connected") is None
    assert _canonical_dispatch_status(channel="voice", native_status="limit_reached") is None


def test_voice_duration_is_emitted_only_for_answered_calls() -> None:
    metadata = {"duration_seconds": 28}

    assert _dispatch_duration_seconds(
        channel="voice",
        canonical_status="answered",
        metadata=metadata,
    ) == 28
    assert _dispatch_duration_seconds(
        channel="voice",
        canonical_status="no_answer",
        metadata=metadata,
    ) == 0
    assert _dispatch_duration_seconds(
        channel="sms",
        canonical_status="sent",
        metadata=metadata,
    ) is None


def test_whatsapp_dispatch_time_precedes_out_of_order_provider_callbacks() -> None:
    component_ref_id = "8ad3af4c-b4f0-4b8f-adf1-a011f0882585"
    prepared_at = datetime(2026, 10, 9, 15, 32, 22, tzinfo=UTC)
    delivered_at = datetime(2026, 10, 9, 15, 32, 24, tzinfo=UTC)

    dispatched_at = _resolve_dispatched_at(
        observed_at=delivered_at,
        action_row={"requested_at": delivered_at},
        runtime={
            "whatsapp_hsm_outbound": {
                "component_ref_id": component_ref_id,
                "prepared_at": prepared_at.isoformat(),
            }
        },
        channel="whatsapp",
        component_ref_id=component_ref_id,
    )

    assert dispatched_at == prepared_at
    assert dispatched_at <= delivered_at

    sent_at = datetime(2026, 10, 9, 15, 32, 23, tzinfo=UTC)
    corrected_at = _resolve_dispatched_at(
        observed_at=sent_at,
        action_row={"requested_at": delivered_at, "sent_at": sent_at},
        runtime={
            "whatsapp_hsm_outbound": {
                "component_ref_id": component_ref_id,
                "prepared_at": prepared_at.isoformat(),
            }
        },
        channel="whatsapp",
        component_ref_id=component_ref_id,
    )

    assert corrected_at == sent_at
