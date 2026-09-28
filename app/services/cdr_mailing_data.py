from __future__ import annotations

import ast
import json
from typing import Any


MAX_CDR_MAILING_DATA_LENGTH = 4096


def parse_cdr_mailing_data(raw_value: Any) -> dict[str, Any]:
    """Parse CdrMailingData emitted as a mapping, JSON or Python literal.

    Asterisk currently returns the mapping using Python's literal
    representation (single quotes). Other callers may provide an already
    decoded mapping or a JSON string. The result is deliberately restricted to
    a dictionary and the input size is bounded before ``literal_eval``.
    """

    if isinstance(raw_value, dict):
        return dict(raw_value)

    if not isinstance(raw_value, str):
        return {}

    raw_text = raw_value.strip()
    if not raw_text or len(raw_text) > MAX_CDR_MAILING_DATA_LENGTH:
        return {}

    try:
        parsed = json.loads(raw_text)
    except (
        json.JSONDecodeError,
        TypeError,
        ValueError,
        MemoryError,
        RecursionError,
    ):
        try:
            parsed = ast.literal_eval(raw_text)
        except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
            return {}

    return dict(parsed) if isinstance(parsed, dict) else {}


def extract_cdr_mailing_phone(raw_value: Any) -> str | None:
    parsed = parse_cdr_mailing_data(raw_value)
    phone = parsed.get("phone")
    if not isinstance(phone, str) or not phone.strip():
        return None
    return phone
