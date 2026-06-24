from __future__ import annotations

import pytest

from app.services.dialer_release_mapper import resolve_dialer_status_from_release


@pytest.mark.parametrize(
    ("cause_code", "expected_status"),
    [
        ("490", "machine"),
        ("491", "machine"),
        ("492", "machine"),
        ("200", "answered"),
        ("16", "answered"),
        ("486", "busy"),
        ("17", "busy"),
        ("480", "no_answer"),
        ("19", "no_answer"),
        ("404", "invalid_number"),
        ("31", "invalid_number"),
        ("403", "rejected"),
        ("57", "rejected"),
        ("999", "failed"),
    ],
)
def test_resolve_dialer_status_from_release_maps_cause_codes(
    cause_code: str,
    expected_status: str,
) -> None:
    payload = {
        "hangup": {
            "Event": "Hangup",
            "Cause": cause_code,
        }
    }
    assert resolve_dialer_status_from_release(payload) == expected_status


def test_resolve_dialer_status_from_release_prioritizes_machine_codes() -> None:
    payload = {
        "status": "answered",
        "hangup": {
            "Event": "Hangup",
            "Cause": "490",
        },
    }
    assert resolve_dialer_status_from_release(payload) == "machine"


def test_resolve_dialer_status_from_release_returns_none_without_release_code() -> None:
    assert resolve_dialer_status_from_release({"status": "noanswer"}) is None
    assert resolve_dialer_status_from_release({"hangup": {"Disposition": "BUSY"}}) is None
