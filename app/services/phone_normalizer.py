from __future__ import annotations


def normalize_phone_to_canonical_ani(phone: str | None) -> str | None:
    if phone is None:
        return None

    value = str(phone).strip()
    if not value:
        return value

    digits = "".join(char for char in value if char.isdigit())
    if not digits:
        return ""

    if digits.startswith("55") and len(digits) in (12, 13):
        return digits[2:]
    return digits


def normalize_br_mobile_missing_ninth_digit(phone: str | None) -> str | None:
    if phone is None:
        return None

    value = str(phone).strip()
    if not value:
        return value

    if value.isdigit() and value.startswith("55") and len(value) == 12 and value[4] == "9":
        return f"{value[:4]}9{value[4:]}"

    return value
