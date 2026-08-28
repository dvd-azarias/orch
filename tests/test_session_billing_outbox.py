from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

import app.services.session_service as session_service
from app.repositories.orch_sessions_repository import PersistResult


class _DbSession:
    def in_transaction(self) -> bool:
        return False

    @asynccontextmanager
    async def begin(self):
        yield self

    @asynccontextmanager
    async def begin_nested(self):
        yield self

    async def execute(self, *_args, **_kwargs) -> None:
        return None


@pytest.mark.asyncio
async def test_new_session_creates_one_billing_outbox_record(monkeypatch) -> None:
    billing_outbox = AsyncMock()
    monkeypatch.setattr(
        session_service,
        "upsert_active_session",
        AsyncMock(return_value=PersistResult(id=42, uuid="1fe19d40-2330-4263-9781-1805ece1d816", state=0, created=True)),
    )
    monkeypatch.setattr(session_service, "try_create_billing_snapshot_outbox", billing_outbox)
    monkeypatch.setattr(
        session_service,
        "get_settings",
        lambda: SimpleNamespace(orch_billing_snapshot_enabled=True, orch_billing_enabled=False),
    )
    monkeypatch.setattr(session_service, "get_current_workspace_schema", lambda: "ws_workspace")
    monkeypatch.setattr(session_service, "get_current_workspace_uuid", lambda: "workspace-1")

    persisted = await session_service.persist_session(
        _DbSession(),
        flow_uuid="flow-1",
        app_name="GenericApp",
        extracted={"entity": "entity", "entity_type": "type", "entity_address": "address", "entity_session_id": "sid"},
        payload={},
    )

    assert persisted.session_created is True
    billing_outbox.assert_awaited_once_with(
        ANY,
        workspace_uuid="workspace-1",
        session_id=42,
        session_uuid="1fe19d40-2330-4263-9781-1805ece1d816",
    )


@pytest.mark.asyncio
async def test_reused_session_does_not_create_billing_outbox_record(monkeypatch) -> None:
    billing_outbox = AsyncMock()
    monkeypatch.setattr(
        session_service,
        "upsert_active_session",
        AsyncMock(return_value=PersistResult(id=42, uuid="1fe19d40-2330-4263-9781-1805ece1d816", state=0, created=False)),
    )
    monkeypatch.setattr(session_service, "try_create_billing_snapshot_outbox", billing_outbox)
    monkeypatch.setattr(
        session_service,
        "get_settings",
        lambda: SimpleNamespace(orch_billing_snapshot_enabled=True, orch_billing_enabled=False),
    )
    monkeypatch.setattr(session_service, "get_current_workspace_schema", lambda: "ws_workspace")

    persisted = await session_service.persist_session(
        _DbSession(),
        flow_uuid="flow-1",
        app_name="GenericApp",
        extracted={"entity": "entity", "entity_type": "type", "entity_address": "address", "entity_session_id": "sid"},
        payload={},
    )

    assert persisted.session_created is False
    billing_outbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_batch_billing_replaces_legacy_producer_without_dual_write(monkeypatch) -> None:
    legacy_outbox = AsyncMock()
    batch_event = AsyncMock()
    monkeypatch.setattr(
        session_service,
        "upsert_active_session",
        AsyncMock(return_value=PersistResult(id=42, uuid="1fe19d40-2330-4263-9781-1805ece1d816", state=0, created=True)),
    )
    monkeypatch.setattr(session_service, "try_create_billing_snapshot_outbox", legacy_outbox)
    monkeypatch.setattr(session_service, "try_record_billing_event", batch_event)
    monkeypatch.setattr(
        session_service,
        "get_settings",
        lambda: SimpleNamespace(orch_billing_snapshot_enabled=False, orch_billing_enabled=True),
    )
    monkeypatch.setattr(session_service, "get_current_workspace_schema", lambda: "ws_workspace")
    monkeypatch.setattr(session_service, "get_current_workspace_uuid", lambda: "workspace-1")

    await session_service.persist_session(
        _DbSession(),
        flow_uuid="flow-1",
        app_name="GenericApp",
        extracted={"entity": "entity", "entity_type": "type", "entity_address": "address", "entity_session_id": "sid"},
        payload={},
    )

    legacy_outbox.assert_not_awaited()
    batch_event.assert_awaited_once_with(ANY, workspace_uuid="workspace-1", session_id=42)
