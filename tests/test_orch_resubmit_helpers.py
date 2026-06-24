from __future__ import annotations

from types import SimpleNamespace

import app.api.v1.orch as orch_api


def test_normalize_supplier_entity_address_keeps_digits() -> None:
    assert orch_api._normalize_supplier_entity_address("+55 (11) 97562-0806") == "5511975620806"
    assert orch_api._normalize_supplier_entity_address("abc-123") == "123"
    assert orch_api._normalize_supplier_entity_address("   ") == ""


def test_is_allowed_supplier_client_accepts_sync_and_arquivos_pairs(monkeypatch) -> None:
    monkeypatch.setattr(
        orch_api,
        "get_settings",
        lambda: SimpleNamespace(
            sync_ws_client_id="sync-id",
            sync_ws_client_secret="sync-secret",
            arquivos_client_id="arq-id",
            arquivos_client_secret="arq-secret",
        ),
    )

    assert orch_api._is_allowed_supplier_client(client_id="sync-id", client_secret="sync-secret") is True
    assert orch_api._is_allowed_supplier_client(client_id="arq-id", client_secret="arq-secret") is True
    assert orch_api._is_allowed_supplier_client(client_id="sync-id", client_secret="wrong") is False
