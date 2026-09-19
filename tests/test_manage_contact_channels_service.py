from __future__ import annotations

import pytest

from app.services.manage_contact_channels_service import (
    ManageContactChannelsError,
    apply_contact_channel_operation,
    parse_requested_contact_channels,
)


def test_parse_normalizes_phone_and_email_without_merging_transport_types() -> None:
    requested = parse_requested_contact_channels(
        [
            {
                "type": "voice",
                "address": "+55 (11) 97562-0806",
                "label": "celular",
                "priority": 2,
            },
            {
                "type": "whatsapp",
                "address": "5511975620806",
                "priority": 1,
                "is_primary": True,
            },
            {"type": "email", "address": " Pessoa@Example.COM "},
        ]
    )

    assert [(item.channel_type, item.value) for item in requested] == [
        ("voice", "11975620806"),
        ("whatsapp", "11975620806"),
        ("email", "pessoa@example.com"),
    ]
    assert requested[1].is_primary is True


def test_parse_rejects_conflicting_duplicate_after_normalization() -> None:
    with pytest.raises(ManageContactChannelsError) as exc_info:
        parse_requested_contact_channels(
            [
                {"type": "voice", "address": "11975620806", "label": "A"},
                {"type": "voice", "address": "+55 11 97562-0806", "label": "B"},
            ]
        )

    assert exc_info.value.code == "manage_contact_channels_conflicting_duplicate"


def test_parse_rejects_more_than_one_primary() -> None:
    with pytest.raises(ManageContactChannelsError) as exc_info:
        parse_requested_contact_channels(
            [
                {"type": "voice", "address": "11975620806", "is_primary": True},
                {"type": "email", "address": "pessoa@example.com", "is_primary": True},
            ]
        )

    assert exc_info.value.code == "manage_contact_channels_multiple_primary"


def test_upsert_is_idempotent_and_updates_metadata_without_duplicate() -> None:
    requested = parse_requested_contact_channels(
        [
            {
                "type": "voice",
                "address": "+55 11 97562-0806",
                "label": "principal",
                "priority": 1,
                "is_primary": True,
            }
        ]
    )
    first = apply_contact_channel_operation(
        existing_channels=[],
        requested_channels=requested,
        operation="upsert",
    )
    second = apply_contact_channel_operation(
        existing_channels=first.channels,
        requested_channels=requested,
        operation="upsert",
    )

    assert len(first.channels) == 1
    assert first.changed_keys == [("voice", "11975620806")]
    assert first.primary_channel == first.channels[0]
    assert second.changed_keys == []
    assert second.channels == first.channels


def test_deactivate_is_all_or_nothing_when_one_channel_is_missing() -> None:
    requested = parse_requested_contact_channels(
        [
            {"type": "voice", "address": "11975620806"},
            {"type": "email", "address": "missing@example.com"},
        ]
    )
    existing = [
        {
            "type": "voice",
            "value": "11975620806",
            "label": "principal",
            "priority": 1,
            "is_primary": True,
        }
    ]

    result = apply_contact_channel_operation(
        existing_channels=existing,
        requested_channels=requested,
        operation="deactivate",
    )

    assert result.changed_keys == []
    assert result.missing_keys == [("email", "missing@example.com")]
    assert result.channels[0]["state"] == "active"


def test_deactivate_primary_promotes_next_active_channel() -> None:
    requested = parse_requested_contact_channels(
        [{"type": "voice", "address": "11975620806"}]
    )
    result = apply_contact_channel_operation(
        existing_channels=[
            {
                "type": "voice",
                "value": "11975620806",
                "priority": 1,
                "is_primary": True,
            },
            {
                "type": "email",
                "value": "pessoa@example.com",
                "priority": 2,
                "is_primary": False,
            },
        ],
        requested_channels=requested,
        operation="deactivate",
    )

    channels = {(item["type"], item["value"]): item for item in result.channels}
    assert channels[("voice", "11975620806")]["state"] == "inactive"
    assert channels[("voice", "11975620806")]["is_primary"] is False
    assert channels[("email", "pessoa@example.com")]["is_primary"] is True
    assert result.primary_channel == channels[("email", "pessoa@example.com")]


def test_same_address_is_not_a_conflict_between_transport_types() -> None:
    requested = parse_requested_contact_channels(
        [
            {"type": "voice", "address": "11975620806"},
            {"type": "sms", "address": "11975620806"},
            {"type": "rcs", "address": "11975620806"},
        ]
    )

    result = apply_contact_channel_operation(
        existing_channels=[],
        requested_channels=requested,
        operation="upsert",
    )

    assert len(result.channels) == 3
    assert {(item["type"], item["value"]) for item in result.channels} == {
        ("voice", "11975620806"),
        ("sms", "11975620806"),
        ("rcs", "11975620806"),
    }


def test_legacy_string_booleans_do_not_promote_inactive_channel() -> None:
    requested = parse_requested_contact_channels(
        [{"type": "email", "address": "pessoa@example.com"}]
    )

    result = apply_contact_channel_operation(
        existing_channels=[
            {
                "type": "phone",
                "value": "11975620806",
                "label": {"invalid": "legacy"},
                "is_primary": "false",
                "is_valid": "false",
                "is_reachable": "true",
            }
        ],
        requested_channels=requested,
        operation="upsert",
    )

    channels = {(item["type"], item["value"]): item for item in result.channels}
    legacy = channels[("voice", "11975620806")]
    assert legacy["state"] == "inactive"
    assert legacy["is_primary"] is False
    assert legacy["label"] is None
    assert channels[("email", "pessoa@example.com")]["is_primary"] is True
