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
