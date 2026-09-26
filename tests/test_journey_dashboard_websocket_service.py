from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import WebSocketDisconnect

import app.api.v1.orch_observability as observability_api
import app.services.journey_dashboard_websocket_service as websocket_service
from app.services.journey_dashboard_websocket_service import JourneyDashboardTicket
from app.services.journey_workspace_snapshot_service import (
    JourneyWorkspaceSnapshotCurrent,
)


class _FakeRedis:
    def __init__(self, state: dict[str, str]) -> None:
        self.state = state

    async def set(self, key, value, *, ex, nx):  # noqa: ANN001
        assert ex == 30
        assert nx is True
        if key in self.state:
            return False
        self.state[key] = value
        return True

    async def getdel(self, key):  # noqa: ANN001
        return self.state.pop(key, None)

    async def aclose(self) -> None:
        return None


class _FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)
        self.accepted = False
        self.sent: list[dict] = []
        self.closed_with: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        raise WebSocketDisconnect(code=1000)

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int) -> None:
        self.closed_with = code


class _FakeWorkspaceHub:
    def __init__(self) -> None:
        self.queue: websocket_service.asyncio.Queue[int] = (
            websocket_service.asyncio.Queue(maxsize=1)
        )
        self.registered_workspace: str | None = None
        self.unregistered_workspace: str | None = None

    async def register(self, *, redis_url: str, workspace_uuid: str):  # noqa: ANN201
        assert redis_url == "redis://fake"
        self.registered_workspace = workspace_uuid
        return self.queue

    async def unregister(self, *, workspace_uuid: str, queue) -> None:  # noqa: ANN001
        assert queue is self.queue
        self.unregistered_workspace = workspace_uuid


@pytest.mark.asyncio
async def test_ticket_is_opaque_hashed_and_single_use(monkeypatch) -> None:
    state: dict[str, str] = {}
    monkeypatch.setattr(
        websocket_service.async_redis.Redis,
        "from_url",
        lambda *_args, **_kwargs: _FakeRedis(state),
    )
    workspace_uuid = str(uuid4())
    ticket = await websocket_service.create_journey_dashboard_ticket(
        redis_url="redis://unused",
        workspace_uuid=workspace_uuid,
        workspace_name="Workspace Teste",
        principal="internal-ui",
        ttl_seconds=30,
    )

    assert ticket not in next(iter(state))
    consumed = await websocket_service.consume_journey_dashboard_ticket(
        redis_url="redis://unused",
        ticket=ticket,
    )
    assert consumed is not None
    assert consumed.workspace_uuid == workspace_uuid
    assert consumed.workspace_name == "Workspace Teste"
    assert consumed.principal == "internal-ui"
    assert await websocket_service.consume_journey_dashboard_ticket(
        redis_url="redis://unused",
        ticket=ticket,
    ) is None


@pytest.mark.asyncio
async def test_workspace_hub_coalesces_notifications_per_connection() -> None:
    workspace_uuid = str(uuid4())
    queue: websocket_service.asyncio.Queue[int] = websocket_service.asyncio.Queue(
        maxsize=1
    )
    hub = websocket_service.JourneyDashboardWorkspaceHub()
    hub._queues = {workspace_uuid: {queue}}  # noqa: SLF001

    await hub._dispatch(workspace_uuid, 10)  # noqa: SLF001
    await hub._dispatch(workspace_uuid, 11)  # noqa: SLF001

    assert queue.qsize() == 1
    assert queue.get_nowait() == 11


@pytest.mark.asyncio
async def test_websocket_route_authenticates_and_sends_workspace_snapshot(
    monkeypatch,
) -> None:
    workspace_uuid = str(uuid4())
    flow_uuid = str(uuid4())
    revision_uuid = str(uuid4())
    ticket_value = "t" * 40
    fake_hub = _FakeWorkspaceHub()
    websocket = _FakeWebSocket(
        [
            json.dumps(
                {
                    "type": "authenticate",
                    "version": "1.0",
                    "ticket": ticket_value,
                }
            )
        ]
    )

    monkeypatch.setattr(
        observability_api,
        "get_settings",
        lambda: SimpleNamespace(
            orch_journey_dashboard_enabled=True,
            orch_journey_redis_url="redis://fake",
        ),
    )

    async def _consume_ticket(**kwargs):  # noqa: ANN003, ANN202
        assert kwargs == {"redis_url": "redis://fake", "ticket": ticket_value}
        return JourneyDashboardTicket(
            workspace_uuid=workspace_uuid,
            workspace_name="Workspace Teste",
            principal="internal-ui",
            issued_at=datetime.now(UTC),
        )

    async def _load_snapshot(requested_workspace_uuid: str):  # noqa: ANN202
        assert requested_workspace_uuid == workspace_uuid
        return _current_snapshot(workspace_uuid, flow_uuid, revision_uuid)

    monkeypatch.setattr(
        observability_api,
        "consume_journey_dashboard_ticket",
        _consume_ticket,
    )
    monkeypatch.setattr(observability_api, "_load_workspace_snapshot", _load_snapshot)
    monkeypatch.setattr(
        observability_api,
        "journey_dashboard_workspace_hub",
        fake_hub,
    )

    await observability_api.journey_dashboard_websocket(websocket)

    assert websocket.accepted is True
    assert websocket.closed_with is None
    assert websocket.sent[0] == {
        "type": "authenticated",
        "version": "1.0",
        "workspace_uuid": workspace_uuid,
        "heartbeat_seconds": 30,
    }
    assert websocket.sent[1]["type"] == "orchestration_workspace_snapshot"
    assert websocket.sent[1]["meta"]["snapshot_sequence"] == 4
    assert websocket.sent[1]["meta"]["reason"] == "initial"
    assert fake_hub.registered_workspace == workspace_uuid
    assert fake_hub.unregistered_workspace == workspace_uuid


@pytest.mark.asyncio
async def test_websocket_route_rejects_invalid_ticket(monkeypatch) -> None:
    websocket = _FakeWebSocket(
        [
            json.dumps(
                {
                    "type": "authenticate",
                    "version": "1.0",
                    "ticket": "x" * 40,
                }
            )
        ]
    )
    monkeypatch.setattr(
        observability_api,
        "get_settings",
        lambda: SimpleNamespace(
            orch_journey_dashboard_enabled=True,
            orch_journey_redis_url="redis://fake",
        ),
    )

    async def _reject_ticket(**_kwargs):  # noqa: ANN003, ANN202
        return None

    monkeypatch.setattr(
        observability_api,
        "consume_journey_dashboard_ticket",
        _reject_ticket,
    )

    await observability_api.journey_dashboard_websocket(websocket)

    assert websocket.accepted is True
    assert websocket.sent == []
    assert websocket.closed_with == 4401


def _current_snapshot(workspace_uuid: str, flow_uuid: str, revision_uuid: str):
    now = datetime.now(UTC)
    return JourneyWorkspaceSnapshotCurrent(
        snapshot_id=uuid4(),
        snapshot_sequence=4,
        built_at=now,
        payload={
            "type": "orchestration_workspace_snapshot",
            "meta": {"snapshot_sequence": 4},
            "payload": {
                "workspace": {"uuid": workspace_uuid},
                "retention": {
                    "oldest_available_at": (now - timedelta(days=10))
                    .isoformat()
                    .replace("+00:00", "Z")
                },
                "catalog": {
                    "flows": [{"uuid": flow_uuid, "name": "Fluxo"}],
                    "revisions": [
                        {
                            "uuid": revision_uuid,
                            "flow_uuid": flow_uuid,
                            "version": 1,
                        }
                    ],
                    "channels": ["voice", "sms", "whatsapp", "rcs", "email"],
                },
            },
        },
    )


def test_view_filters_are_bound_to_snapshot_catalog_and_retention() -> None:
    workspace_uuid = str(uuid4())
    flow_uuid = str(uuid4())
    revision_uuid = str(uuid4())
    current = _current_snapshot(workspace_uuid, flow_uuid, revision_uuid)
    today = datetime.now(ZoneInfo("America/Sao_Paulo")).date().isoformat()

    filters, period_from, period_to = observability_api._parse_view_filters(  # noqa: SLF001
        {
            "flow_uuid": flow_uuid,
            "revision_uuid": revision_uuid,
            "channels": ["sms", "voice", "sms"],
            "period": {
                "from": today,
                "to": today,
                "timezone": "America/Sao_Paulo",
            },
        },
        current=current,
    )
    assert filters["channels"] == ["sms", "voice"]
    assert period_from < period_to

    with pytest.raises(ValueError, match="invalid_filters"):
        observability_api._parse_view_filters(  # noqa: SLF001
            {
                "flow_uuid": str(uuid4()),
                "revision_uuid": None,
                "channels": ["sms"],
                "period": {
                    "from": today,
                    "to": today,
                    "timezone": "America/Sao_Paulo",
                },
            },
            current=current,
        )


def test_view_filter_clamps_first_day_to_coverage_boundary() -> None:
    workspace_uuid = str(uuid4())
    flow_uuid = str(uuid4())
    revision_uuid = str(uuid4())
    current = _current_snapshot(workspace_uuid, flow_uuid, revision_uuid)
    now = datetime.now(UTC)
    coverage_started_at = now - timedelta(minutes=5)
    current.payload["payload"]["retention"]["oldest_available_at"] = (
        coverage_started_at.isoformat().replace("+00:00", "Z")
    )
    today = datetime.now(ZoneInfo("America/Sao_Paulo")).date().isoformat()

    _filters, period_from, period_to = observability_api._parse_view_filters(  # noqa: SLF001
        {
            "flow_uuid": flow_uuid,
            "revision_uuid": revision_uuid,
            "channels": ["voice"],
            "period": {
                "from": today,
                "to": today,
                "timezone": "America/Sao_Paulo",
            },
        },
        current=current,
    )

    assert period_from == coverage_started_at
    assert period_from < period_to
