from __future__ import annotations

from app.services.channel_event_service import extract_channel_events


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
