from __future__ import annotations

from app.handlers.arquivos_app import extract_arquivos_session_fields


def test_extract_arquivos_with_row_content_uses_unique_entity() -> None:
    payload = {
        "file": {
            "id": "file-123",
            "folder_path": "dev-orch/mailing",
            "original_name": "mailing.csv",
            "content": {"cpf": "99911122233"},
            "row_index": 7,
        }
    }

    extracted = extract_arquivos_session_fields(payload)
    assert extracted.entity == "file-123:99911122233"
    assert extracted.entity_type == "file"
    assert extracted.entity_session_id == "file-123:99911122233"
    assert extracted.entity_address.endswith("#99911122233")


def test_extract_arquivos_with_row_index_fallback() -> None:
    payload = {
        "file": {
            "id": "file-abc",
            "folder_path": "dev-orch/mailing",
            "original_name": "mailing.csv",
            "content": {"campo_sem_id": "valor"},
            "row_index": 2,
        }
    }

    extracted = extract_arquivos_session_fields(payload)
    assert extracted.entity == "file-abc:row_2"
    assert extracted.entity_type == "file"
    assert extracted.entity_session_id == "file-abc:row_2"
    assert extracted.entity_address.endswith("#row_2")


def test_extract_arquivos_tipo1_uses_person_entity_type() -> None:
    payload = {
        "mapping_template_id": "66fe246a-a60a-4c26-9363-199206bceabd",
        "file": {
            "id": "file-xyz",
            "folder_path": "system/mailings",
            "original_name": "mailing.csv",
            "content": {"cpf": "01392286840"},
        },
    }

    extracted = extract_arquivos_session_fields(payload)
    assert extracted.entity == "file-xyz:01392286840"
    assert extracted.entity_type == "person"
