from __future__ import annotations

import json

import pytest

import app.services.identidade_person_service as service


def _provider_person() -> dict:
    return {
        "document": {"raw": "12345678901", "formatted": "123.456.789-01"},
        "name": {"raw": "MANOEL DO CARMO", "formatted": "Manoel do Carmo"},
        "birthday": "1940-08-12",
        "gender": {"code": None, "description": "Desconhecido"},
        "addresses": [
            {
                "ranking": 1,
                "city": {"raw": "ITABORAI", "formatted": "Itaborai"},
                "state": {"code": "RJ", "description": "Rio de Janeiro"},
            }
        ],
        "emails": [{"ranking": 1, "email": "PESSOA@EXAMPLE.COM"}],
        "phones": [
            {
                "ranking": 1,
                "score": 5,
                "number": {"raw": "21974305218"},
                "has_whatsapp": True,
                "has_rcs": True,
                "do_not_disturb": True,
            },
            {
                "ranking": 2,
                "score": 4,
                "number": {"raw": "64992798295"},
                "has_whatsapp": True,
                "has_rcs": False,
                "do_not_disturb": False,
            },
        ],
    }


def test_normalize_identidade_person_excludes_dnd_from_actionable_channels() -> None:
    normalized = service.normalize_identidade_person(
        _provider_person(),
        fallback_document="12345678901",
        phone_policy="all_eligible",
    )

    assert normalized["identifier"] == "12345678901"
    assert normalized["full_name"] == "Manoel do Carmo"
    assert normalized["birthdate"] == "1940-08-12"
    assert normalized["state"] == "RJ"
    assert normalized["city"] == "Itaborai"
    assert {(item["type"], item["value"]) for item in normalized["channels"]} == {
        ("voice", "64992798295"),
        ("whatsapp", "64992798295"),
        ("email", "pessoa@example.com"),
    }
    assert normalized["extras"]["identidade"]["phones"][0]["do_not_disturb"] is True


def test_best_eligible_uses_first_non_dnd_phone() -> None:
    normalized = service.normalize_identidade_person(
        _provider_person(),
        fallback_document="12345678901",
        phone_policy="best_eligible",
    )
    phone_values = {
        item["value"]
        for item in normalized["channels"]
        if item["type"] in {"voice", "whatsapp", "rcs"}
    }
    assert phone_values == {"64992798295"}


def test_merge_person_fill_missing_preserves_values_and_adds_new_channels() -> None:
    existing = {
        "full_name": "Nome local",
        "city": None,
        "primary_channel_type": "voice",
        "primary_channel_value": "11999999999",
        "primary_channel_label": "local",
        "channels": [{"type": "voice", "value": "11999999999", "label": "local"}],
        "extras": {"origem": "crm"},
    }
    incoming = {
        "full_name": "Nome externo",
        "city": "Itaborai",
        "channels": [{"type": "whatsapp", "value": "64992798295", "label": "identidade"}],
        "extras": {"identidade": {"income": 1000}},
    }

    merged = service.merge_person_payload(existing, incoming, enrichment_policy="fill_missing")

    assert merged["full_name"] == "Nome local"
    assert merged["city"] == "Itaborai"
    assert len(merged["channels"]) == 2
    assert merged["extras"] == {"origem": "crm", "identidade": {"income": 1000}}
    assert merged["primary_channel_value"] == "11999999999"


def test_query_sync_uses_fixed_endpoint_and_bearer(monkeypatch) -> None:
    captured: dict = {}

    def fake_http_get(*, url: str, access_token: str, timeout_seconds: float):
        captured.update(url=url, access_token=access_token, timeout_seconds=timeout_seconds)
        return 200, json.dumps({"data": [_provider_person()]})

    monkeypatch.setattr(service, "_http_get", fake_http_get)
    result = service._query_person_sync(
        workspace_id="27861684-6da2-478c-9996-86eb9ac14d5d",
        access_token="secret-token",
        document="12345678901",
        require_phone=True,
        require_email=False,
        timeout_seconds=7,
        max_attempts=2,
    )

    assert result.found is True
    assert captured["url"].startswith(
        "https://api.identidade.io/v1/workspaces/27861684-6da2-478c-9996-86eb9ac14d5d/person?"
    )
    assert "document=12345678901" in captured["url"]
    assert "format=identidade" in captured["url"]
    assert "require_phone=true" in captured["url"]
    assert "require_email" not in captured["url"]
    assert captured["access_token"] == "secret-token"


@pytest.mark.parametrize("value", [None, "", "123", "123.456.789-0"])
def test_normalize_document_rejects_non_cpf_length(value) -> None:
    with pytest.raises(service.IdentidadePersonServiceError) as exc:
        service.normalize_document(value)
    assert exc.value.code == "identidade_person_invalid_document"
