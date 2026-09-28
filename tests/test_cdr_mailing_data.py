from app.services.cdr_mailing_data import (
    MAX_CDR_MAILING_DATA_LENGTH,
    extract_cdr_mailing_phone,
    parse_cdr_mailing_data,
)


def test_parse_cdr_mailing_data_accepts_mapping_json_and_python_literal() -> None:
    expected = {"phone": "5511975620806", "mailing_id": 123}

    assert parse_cdr_mailing_data(expected) == expected
    assert parse_cdr_mailing_data(
        '{"phone":"5511975620806","mailing_id":123}'
    ) == expected
    assert parse_cdr_mailing_data(
        "{'phone': '5511975620806', 'mailing_id': 123}"
    ) == expected


def test_parse_cdr_mailing_data_rejects_non_mapping_and_oversized_input() -> None:
    assert parse_cdr_mailing_data('["5511975620806"]') == {}
    assert parse_cdr_mailing_data("x" * (MAX_CDR_MAILING_DATA_LENGTH + 1)) == {}
    assert parse_cdr_mailing_data("[" * 1200 + "]" * 1200) == {}
    assert parse_cdr_mailing_data("__import__('os').system('false')") == {}


def test_extract_cdr_mailing_phone_requires_non_empty_string() -> None:
    assert extract_cdr_mailing_phone("{'phone': '5511975620806'}") == (
        "5511975620806"
    )
    assert extract_cdr_mailing_phone('{"phone":"5511975620806"}') == (
        "5511975620806"
    )
    assert extract_cdr_mailing_phone({"phone": 5511975620806}) is None
    assert extract_cdr_mailing_phone({"phone": ""}) is None
