from __future__ import annotations

from app.services.file_event_ingest_service import build_row_payloads, parse_file_rows


def test_parse_file_rows_csv_with_header() -> None:
    raw = b"cpf,nome\n123,Ana\n456,Bruno\n"
    rows = parse_file_rows(raw)
    assert rows == [
        {"cpf": "123", "nome": "Ana"},
        {"cpf": "456", "nome": "Bruno"},
    ]


def test_build_row_payloads_injects_file_content_and_row_metadata() -> None:
    base_payload = {
        "file": {
            "id": "f-1",
            "folder_path": "dev-orch/mailing",
            "original_name": "mailing.csv",
        }
    }
    rows = [{"cpf": "123"}, {"cpf": "456"}]

    payloads = build_row_payloads(base_payload, rows)
    assert len(payloads) == 2
    assert payloads[0]["file"]["content"]["cpf"] == "123"
    assert payloads[0]["file"]["row_index"] == 1
    assert payloads[0]["file"]["row_count"] == 2
    assert payloads[1]["file"]["content"]["cpf"] == "456"
    assert payloads[1]["file"]["row_index"] == 2
    assert payloads[1]["file"]["row_count"] == 2

