from __future__ import annotations

import pytest

import app.services.orch_trigger_service as orch_trigger_service
from app.schemas.orch import SessionExtraction


class _DummyTx:
    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
        return False


class _DummySession:
    def in_transaction(self) -> bool:
        return False

    def begin(self) -> _DummyTx:
        return _DummyTx()

    def begin_nested(self) -> _DummyTx:
        return _DummyTx()

    async def execute(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None


@pytest.mark.asyncio
async def test_process_single_payload_discards_whatsapp_status_already_processed(monkeypatch) -> None:
    captured: dict = {}

    monkeypatch.setattr(
        orch_trigger_service,
        "extract_session_fields",
        lambda app_name, payload: SessionExtraction(
            entity="5511975620806",
            entity_type="person",
            entity_address="5511975620806",
            entity_session_id="5511975620806",
        ),
    )

    async def _fake_fetch_latest(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {
            "id": 1,
            "whatsapp_sent_at": "2026-05-13T18:43:05Z",
            "whatsapp_delivered_at": None,
            "whatsapp_read_at": None,
            "whatsapp_failed_at": None,
        }

    async def _fake_discard(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["discard_reason"] = kwargs.get("discard_reason")

    monkeypatch.setattr(orch_trigger_service, "fetch_latest_session_by_flow_entity_address", _fake_fetch_latest)
    monkeypatch.setattr(orch_trigger_service, "persist_discarded_event", _fake_discard)

    response = await orch_trigger_service.process_single_payload(
        safe_workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        workspace_schema="ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="0300054c-5f39-4cda-ae88-fe993fd9044b",
        payload={
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [{"status": "sent"}],
                            }
                        }
                    ]
                }
            ],
        },
        db_session=_DummySession(),
        app_name="WhatsApp",
    )

    assert response.accepted is False
    assert response.status == "ignored"
    assert response.persistence == "ignored"
    assert captured["discard_reason"] == "whatsapp_status_already_processed"
