from __future__ import annotations

import pytest

import app.services.channel_event_service as channel_event_service
from app.services.channel_event_service import extract_channel_events, persist_channel_events


def test_extract_channel_events_returns_whatsapp_status_items() -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [
                                {
                                    "id": "wamid-1",
                                    "status": "sent",
                                    "timestamp": "1778682845",
                                    "recipient_id": "5511975620806",
                                },
                                {
                                    "id": "wamid-2",
                                    "status": "delivered",
                                    "timestamp": "1778682855",
                                    "recipient_id": "5511975620806",
                                },
                            ],
                        }
                    }
                ]
            }
        ],
    }

    events = extract_channel_events("WhatsApp", payload)

    assert len(events) == 2
    assert events[0].channel == "whatsapp"
    assert events[0].event_type == "sent"
    assert events[0].event_id == "wamid-1"
    assert events[0].event_ts is not None
    assert events[1].event_type == "delivered"
    assert events[1].event_id == "wamid-2"


def test_extract_channel_events_ignores_non_supported_app() -> None:
    events = extract_channel_events("GenericApp", {"payload": "x"})
    assert events == []


def test_extract_channel_events_returns_whatsapp_message_items() -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "551147371486"},
                            "messages": [
                                {
                                    "id": "wamid-message-1",
                                    "from": "5511975620806",
                                    "timestamp": "1781526585",
                                    "type": "interactive",
                                    "interactive": {
                                        "button_reply": {"id": "OTIMO"},
                                    },
                                },
                                {
                                    "id": "wamid-message-2",
                                    "from": "5511975620806",
                                    "timestamp": "1781526586",
                                    "type": "text",
                                    "text": {"body": "olá. bom dia!"},
                                },
                            ],
                        }
                    }
                ]
            }
        ],
    }

    events = extract_channel_events("WhatsApp", payload)

    assert len(events) == 2
    assert events[0].channel == "whatsapp"
    assert events[0].event_type == "message:otimo"
    assert events[0].event_id == "wamid-message-1"
    assert events[0].event_ts is not None
    assert events[1].event_type == "message:ola_bom_dia"
    assert events[1].event_id == "wamid-message-2"


def test_extract_channel_events_returns_dialer_item() -> None:
    payload = {
        "uniqueid": "GW01-444.1",
        "hangup": {
            "Event": "Hangup",
            "Disposition": "BUSY",
            "Cause": "486",
            "Uniqueid": "GW01-444.1",
        },
    }

    events = extract_channel_events("DialerApp", payload)

    assert len(events) == 1
    assert events[0].channel == "dialer"
    assert events[0].event_type == "busy"
    assert events[0].event_id == "GW01-444.1"


class _Transaction:
    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
        return False


class _Session:
    def in_transaction(self) -> bool:
        return False

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None


@pytest.mark.asyncio
async def test_persist_dialer_event_sets_single_session_cdr_after_ledger_insert(monkeypatch) -> None:
    payload = {
        "uniqueid": "GW01-444.1",
        "hangup": {
            "Event": "Hangup",
            "Disposition": "BUSY",
            "Cause": "486",
            "Uniqueid": "GW01-444.1",
        },
    }
    stored: list[dict] = []

    async def _insert(*_args, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
        return True

    async def _set_cdr(*_args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        stored.append(kwargs["cdr"])

    async def _fetch_flow(*_args, **_kwargs) -> dict:  # type: ignore[no-untyped-def]
        return {"id": "3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17"}

    async def _fetch_revision(*_args, **_kwargs) -> dict:  # type: ignore[no-untyped-def]
        return {
            "definition": {
                "components": [
                    {
                        "component_id": "finish_flow",
                        "parameters": {"webhook": "https://example.test/hook"},
                    }
                ]
            }
        }

    monkeypatch.setattr(channel_event_service, "insert_channel_event", _insert)
    monkeypatch.setattr(channel_event_service, "set_session_cdr", _set_cdr)
    monkeypatch.setattr(channel_event_service, "fetch_flow_row", _fetch_flow)
    monkeypatch.setattr(channel_event_service, "fetch_selected_revision", _fetch_revision)

    persisted = await persist_channel_events(
        _Session(),
        session_id=6941,
        flow_uuid="3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17",
        app_name="DialerApp",
        payload=payload,
    )

    assert persisted == 1
    assert stored == [payload]


@pytest.mark.asyncio
async def test_persist_dialer_event_does_not_set_cdr_without_finish_webhook(monkeypatch) -> None:
    payload = {
        "uniqueid": "GW01-445.1",
        "hangup": {
            "Event": "Hangup",
            "Disposition": "BUSY",
            "Cause": "486",
            "Uniqueid": "GW01-445.1",
        },
    }
    stored: list[dict] = []

    async def _insert(*_args, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
        return True

    async def _set_cdr(*_args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        stored.append(kwargs["cdr"])

    async def _fetch_flow(*_args, **_kwargs) -> dict:  # type: ignore[no-untyped-def]
        return {"id": "3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17"}

    async def _fetch_revision(*_args, **_kwargs) -> dict:  # type: ignore[no-untyped-def]
        return {
            "definition": {
                "components": [
                    {"component_id": "finish_flow", "parameters": {"webhook": None}}
                ]
            }
        }

    monkeypatch.setattr(channel_event_service, "insert_channel_event", _insert)
    monkeypatch.setattr(channel_event_service, "set_session_cdr", _set_cdr)
    monkeypatch.setattr(channel_event_service, "fetch_flow_row", _fetch_flow)
    monkeypatch.setattr(channel_event_service, "fetch_selected_revision", _fetch_revision)

    persisted = await persist_channel_events(
        _Session(),
        session_id=6942,
        flow_uuid="3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17",
        app_name="DialerApp",
        payload=payload,
    )

    assert persisted == 1
    assert stored == []
