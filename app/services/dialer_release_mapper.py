from __future__ import annotations

from typing import Any

DIALER_RELEASE_MACHINE_CODES = {490, 491, 492}
DIALER_RELEASE_ANSWERED_CODES = {200, 202, 204, 16}
DIALER_RELEASE_BUSY_CODES = {486, 600, 17}
DIALER_RELEASE_NO_ANSWER_CODES = {100, 180, 181, 182, 183, 199, 408, 480, 487, 18, 19, 20}
DIALER_RELEASE_INVALID_NUMBER_CODES = {404, 410, 484, 485, 604, 1, 5, 14, 22, 26, 28, 31}
DIALER_RELEASE_REJECTED_CODES = {401, 402, 403, 407, 428, 429, 433, 436, 437, 438, 470, 494, 603, 607, 608, 7, 21, 29, 50, 52, 54, 57}


def _parse_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


def _extract_release_code(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None

    hangup = payload.get("hangup") if isinstance(payload.get("hangup"), dict) else {}
    for raw in (
        hangup.get("DialerHangupCause"),
        hangup.get("Cause"),
        payload.get("DialerHangupCause"),
        payload.get("Cause"),
    ):
        parsed = _parse_int_or_none(raw)
        if parsed is not None:
            return parsed
    return None


def resolve_dialer_status_from_release(payload: Any) -> str | None:
    code = _extract_release_code(payload)
    if code is not None:
        if code in DIALER_RELEASE_MACHINE_CODES:
            return "machine"
        if code in DIALER_RELEASE_ANSWERED_CODES:
            return "answered"
        if code in DIALER_RELEASE_BUSY_CODES:
            return "busy"
        if code in DIALER_RELEASE_NO_ANSWER_CODES:
            return "no_answer"
        if code in DIALER_RELEASE_INVALID_NUMBER_CODES:
            return "invalid_number"
        if code in DIALER_RELEASE_REJECTED_CODES:
            return "rejected"
        return "failed"
    return None
