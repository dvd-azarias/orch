from __future__ import annotations


def normalize_br_mobile_missing_ninth_digit(phone: str | None) -> str | None:
    if phone is None:
        return None

    value = str(phone).strip()
    if not value:
        return value

    if value.isdigit() and value.startswith("55") and len(value) == 12 and value[4] == "9":
        return f"{value[:4]}9{value[4:]}"

    return value
