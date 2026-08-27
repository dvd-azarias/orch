from __future__ import annotations

import pytest

import app.tasks.switch_bot_flow_tasks as tasks
from app.services.switch_bot_flow_service import RunnerDeliveryResult


class _LockResult:
    def scalar_one(self) -> bool:
        return True


class _Transaction:
    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_args):  # noqa: ANN204, ANN002
        return None


class _Session:
    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_args):  # noqa: ANN204, ANN002
        return None

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement, _parameters=None):  # noqa: ANN001, ANN201
        return _LockResult() if "pg_try_advisory_xact_lock" in str(statement) else object()


@pytest.mark.asyncio
async def test_next_user_message_event_discards_status_before_message(monkeypatch) -> None:
    events = [
        {
            "id": 10,
            "event_type": "delivered",
            "payload": {
                "object": "whatsapp_business_account",
                "entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}],
            },
        },
        {
            "id": 11,
            "event_type": "message",
            "payload": {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "messages": [
                                        {"id": "wamid.011", "from": "5511975620806", "type": "text"}
                                    ]
                                }
                            }
                        ]
                    }
                ],
            },
        },
    ]
    discarded: list[dict] = []

    async def _fetch(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return events.pop(0) if events else None

    async def _mark(*_args, **kwargs):  # type: ignore[no-untyped-def]
        discarded.append(kwargs)
        return 1

    monkeypatch.setattr(tasks, "fetch_next_pending_channel_event", _fetch)
    monkeypatch.setattr(tasks, "mark_channel_event_processed", _mark)

    result = await tasks._next_user_message_event(object(), session_id=7001)

    assert result is not None
    assert result["id"] == 11
    assert discarded == [
        {
            "event_row_id": 10,
            "session_id": 7001,
            "channel": "whatsapp",
            "discard_reason": "switch_bot_flow_non_user_event",
        }
    ]


@pytest.mark.asyncio
async def test_process_handoff_opens_session_with_original_pending_payload(monkeypatch) -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"id": "wamid.initial", "from": "5511975620806", "type": "text"}
                            ]
                        }
                    }
                ]
            }
        ],
    }
    runtime_variables = {
        "workflow_v2": {
            "switch_bot_flow": {
                "component_ref_id": "switch-1",
                "target_flow_uuid": "b88c26b2-b5df-4a3d-a9b1-2611c0e3cb31",
                "target_session_id": None,
                "status": "pending_delivery",
                "pending_payload": payload,
                "pending_message_ids": ["wamid.initial"],
            }
        }
    }
    session_state = {
        "flow_uuid": "d3c79b7c-4726-46d0-a787-d99e590242b7",
        "runtime_variables": runtime_variables,
        "last_card_uuid": "11111111-1111-1111-1111-111111111111",
        "next_card_uuid": "11111111-1111-1111-1111-111111111111",
    }
    delivered: dict = {}
    replaced: dict = {}
    marked_ids: list[str] = []

    monkeypatch.setattr(tasks, "bind_workspace_context", lambda workspace_uuid: (workspace_uuid, "ws_test"))
    monkeypatch.setattr(tasks, "get_session_factory", lambda: (lambda: _Session()))

    async def _fetch_state(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return session_state

    def _deliver(**kwargs):  # type: ignore[no-untyped-def]
        delivered.update(kwargs)
        return RunnerDeliveryResult(
            status_code=202,
            response_body={
                "session_id": "target-session-1",
                "execution_kind": "published",
                "revision_id": "revision-1",
                "revision_version": 4,
            },
            attempts=1,
        )

    async def _replace(*_args, **kwargs):  # type: ignore[no-untyped-def]
        replaced.update(kwargs)

    async def _mark_ids(*_args, **kwargs):  # type: ignore[no-untyped-def]
        marked_ids.extend(kwargs["message_ids"])
        return 1

    async def _has_pending(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return False

    monkeypatch.setattr(tasks, "fetch_session_workflow_state", _fetch_state)
    monkeypatch.setattr(tasks, "_deliver", _deliver)
    monkeypatch.setattr(tasks, "replace_session_workflow_state", _replace)
    monkeypatch.setattr(tasks, "mark_whatsapp_messages_processed_by_ids", _mark_ids)
    monkeypatch.setattr(tasks, "has_pending_channel_events", _has_pending)

    await tasks._process_switch_bot_flow_task(
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="d3c79b7c-4726-46d0-a787-d99e590242b7",
        session_id=7001,
    )

    assert delivered["payload"] == payload
    assert delivered["payload"] is payload
    saved_handoff = replaced["runtime_variables"]["workflow_v2"]["switch_bot_flow"]
    assert saved_handoff["status"] == "active"
    assert saved_handoff["target_session_id"] == "target-session-1"
    assert "pending_payload" not in saved_handoff
    assert marked_ids == ["wamid.initial"]
