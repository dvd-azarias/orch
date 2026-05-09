from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services.app_detector import detect_app
from app.services.session_extractor import extract_session_fields

PAYLOADS_DIR = Path(__file__).parent / "payloads"


def _load_payload(file_name: str) -> dict:
    return json.loads((PAYLOADS_DIR / file_name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("file_name", "expected_app"),
    [
        ("arquivos_app.json", "ArquivosApp"),
        ("whatsapp_sent.json", "WhatsApp"),
        ("whatsapp_delivered.json", "WhatsApp"),
        ("whatsapp_read.json", "WhatsApp"),
        ("whatsapp_failed.json", "WhatsApp"),
        ("dialer_app.json", "DialerApp"),
        ("generic_app.json", "GenericApp"),
    ],
)
def test_detect_app_with_phase1_payloads(file_name: str, expected_app: str) -> None:
    payload = _load_payload(file_name)
    assert detect_app(payload) == expected_app


@pytest.mark.parametrize(
    ("file_name", "expected_extracted"),
    [
        (
            "arquivos_app.json",
            {
                "entity": "d5061f9a-0416-4719-886c-bfef5ff35696",
                "entity_type": "file",
                "entity_address": "xbank/enriquecimento/mailing_1_contato_adriano_oficial.csv",
                "entity_session_id": "d5061f9a-0416-4719-886c-bfef5ff35696",
            },
        ),
        (
            "whatsapp_sent.json",
            {
                "entity": "554196311412",
                "entity_type": "person",
                "entity_address": "554196311412",
                "entity_session_id": "554196311412",
            },
        ),
        (
            "dialer_app.json",
            {
                "entity": "GW01-1778291275.2634",
                "entity_type": "person",
                "entity_address": "5511975620806",
                "entity_session_id": "GW01-1778291275.2634",
            },
        ),
        (
            "generic_app.json",
            {
                "entity": "123456",
                "entity_type": "api_request",
                "entity_address": "123456",
                "entity_session_id": "123456",
            },
        ),
    ],
)
def test_extract_session_fields_with_phase1_payloads(file_name: str, expected_extracted: dict) -> None:
    payload = _load_payload(file_name)
    app_name = detect_app(payload)
    extracted = extract_session_fields(app_name, payload)
    assert extracted.model_dump() == expected_extracted


def test_detect_app_returns_422_for_unknown_payload() -> None:
    payload = _load_payload("unknown_invalid.json")

    with pytest.raises(HTTPException) as exc_info:
        detect_app(payload)

    assert exc_info.value.status_code == 422
    assert "external_id" in str(exc_info.value.detail)


def test_generic_extraction_returns_422_for_blank_external_id() -> None:
    payload = {"external_id": "   "}

    with pytest.raises(HTTPException) as exc_info:
        extract_session_fields("GenericApp", payload)

    assert exc_info.value.status_code == 422
    assert "external_id" in str(exc_info.value.detail)
