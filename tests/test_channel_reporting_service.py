from __future__ import annotations

import pytest

from app.services.channel_reporting_service import (
    mask_channel_destination,
    register_channel_action_if_enabled,
)


def test_mask_channel_destination_preserves_only_phone_suffix() -> None:
    assert mask_channel_destination("+55 (11) 97562-0806") == "*********0806"


def test_mask_channel_destination_masks_email_local_part() -> None:
    assert mask_channel_destination("deivid@example.com") == "d***@example.com"


def test_mask_channel_destination_does_not_echo_unknown_identifier() -> None:
    assert mask_channel_destination("customer-external-id") == "***"


@pytest.mark.asyncio
async def test_reporting_validation_failure_is_fail_open_for_channel_runtime() -> None:
    result = await register_channel_action_if_enabled(
        object(),  # type: ignore[arg-type]
        session_id=1,
        session_uuid="not-a-uuid",
        flow_uuid="not-a-uuid",
        flow_revision_id=None,
        component_ref_id="send-sms-1",
        component_kind="send_with_sms",
        channel="sms",
        action_sequence=1,
        source_kind="channel_supplier_v2_dispatch",
        source_id="dispatch-001",
    )

    assert result is None
