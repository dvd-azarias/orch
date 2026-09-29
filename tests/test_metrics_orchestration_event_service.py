from __future__ import annotations

from app.services.metrics_orchestration_event_service import (
    _canonical_dispatch_status,
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
