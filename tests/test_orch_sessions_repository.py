from __future__ import annotations

from datetime import datetime, timezone

from app.repositories.orch_sessions_repository import (
    DialerStatusTimestamps,
    WhatsappStatusTimestamps,
    _compute_effective_whatsapp_limit,
    _derive_state_update,
)


def test_derive_state_update_dialer_answered_does_not_finish_session() -> None:
    dialer_timestamps = DialerStatusTimestamps(
        dialer_answered_at=datetime.now(timezone.utc),
        dialer_busy_at=None,
        dialer_rejected_at=None,
        dialer_invalid_number_at=None,
        dialer_not_answered_at=None,
        dialer_failed_at=None,
    )
    whatsapp_timestamps = WhatsappStatusTimestamps(
        whatsapp_sent_at=None,
        whatsapp_delivered_at=None,
        whatsapp_read_at=None,
        whatsapp_failed_at=None,
    )

    result = _derive_state_update(
        app_name="DialerApp",
        whatsapp_timestamps=whatsapp_timestamps,
        dialer_timestamps=dialer_timestamps,
    )

    assert result.state == 1
    assert result.ended_at is None


def test_compute_effective_whatsapp_limit_with_unlimited_minus_one() -> None:
    assert (
        _compute_effective_whatsapp_limit(
            allowed_limit_raw=-1,
            percentual_consumo=50,
        )
        is None
    )


def test_compute_effective_whatsapp_limit_with_percentual_zero() -> None:
    assert (
        _compute_effective_whatsapp_limit(
            allowed_limit_raw=1000,
            percentual_consumo=0,
        )
        == 0
    )
