from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.api.v1.orch as orch_api
from app.schemas.orch import OrchTriggerAccepted, SessionExtraction


@pytest.mark.asyncio
async def test_create_orch_flow_alias_returns_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_ensure_active_workspace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    def _fake_bind_workspace_context(_workspace_uuid: str):  # type: ignore[no-untyped-def]
        return "ba7eb0ec-e565-447c-8c11-8f870cf72a60", "ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60"

    async def _fake_create_or_get(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return {
            "alias": "1a2b3c4d5e6f70",
            "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            "flow_uuid": "0300054c-5f39-4cda-ae88-fe993fd9044b",
            "is_active": True,
        }

    class _DummyTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _DummySession:
        def in_transaction(self) -> bool:
            return False

        def begin(self):
            return _DummyTx()

        def begin_nested(self):
            return _DummyTx()

    monkeypatch.setattr(orch_api, "ensure_active_workspace", _fake_ensure_active_workspace)
    monkeypatch.setattr(orch_api, "bind_workspace_context", _fake_bind_workspace_context)
    monkeypatch.setattr(orch_api, "create_or_get_flow_alias", _fake_create_or_get)

    response = await orch_api.create_orch_flow_alias(
        workspace_uuid=uuid4(),
        flow_uuid=uuid4(),
        db_session=_DummySession(),  # type: ignore[arg-type]
    )
    assert response.status == "ok"
    assert response.item.alias == "1a2b3c4d5e6f70"
    assert response.item.is_active is True


@pytest.mark.asyncio
async def test_get_orch_flow_alias_by_workspace_flow_returns_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_ensure_active_workspace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    def _fake_bind_workspace_context(_workspace_uuid: str):  # type: ignore[no-untyped-def]
        return "ba7eb0ec-e565-447c-8c11-8f870cf72a60", "ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60"

    async def _fake_fetch_pair(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return {
            "alias": "1a2b3c4d5e6f70",
            "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            "flow_uuid": "0300054c-5f39-4cda-ae88-fe993fd9044b",
            "is_active": True,
        }

    monkeypatch.setattr(orch_api, "ensure_active_workspace", _fake_ensure_active_workspace)
    monkeypatch.setattr(orch_api, "bind_workspace_context", _fake_bind_workspace_context)
    monkeypatch.setattr(orch_api, "fetch_flow_alias_by_workspace_flow", _fake_fetch_pair)

    response = await orch_api.get_orch_flow_alias_by_workspace_flow(
        workspace_uuid=uuid4(),
        flow_uuid=uuid4(),
        db_session=object(),  # type: ignore[arg-type]
    )

    assert response.alias == "1a2b3c4d5e6f70"
    assert response.is_active is True


@pytest.mark.asyncio
async def test_get_orch_flow_alias_by_workspace_flow_returns_404_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_ensure_active_workspace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    def _fake_bind_workspace_context(_workspace_uuid: str):  # type: ignore[no-untyped-def]
        return "ba7eb0ec-e565-447c-8c11-8f870cf72a60", "ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60"

    async def _fake_fetch_pair(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(orch_api, "ensure_active_workspace", _fake_ensure_active_workspace)
    monkeypatch.setattr(orch_api, "bind_workspace_context", _fake_bind_workspace_context)
    monkeypatch.setattr(orch_api, "fetch_flow_alias_by_workspace_flow", _fake_fetch_pair)

    with pytest.raises(HTTPException) as exc:
        await orch_api.get_orch_flow_alias_by_workspace_flow(
            workspace_uuid=uuid4(),
            flow_uuid=uuid4(),
            db_session=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_trigger_orch_resolves_alias_and_calls_workspace_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def _fake_fetch_alias(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return {
            "alias": "1a2b3c4d5e6f70",
            "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            "flow_uuid": "0300054c-5f39-4cda-ae88-fe993fd9044b",
            "is_active": True,
        }

    async def _fake_trigger(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        captured.update(_kwargs)
        return OrchTriggerAccepted(
            status="accepted",
            accepted=True,
            flow_uuid="0300054c-5f39-4cda-ae88-fe993fd9044b",
            app="GenericApp",
            persistence="saved",
            extracted=SessionExtraction(
                entity="e",
                entity_type="person",
                entity_address="addr",
                entity_session_id="sid",
            ),
            session_id=1,
            session_uuid=str(uuid4()),
            session_state=0,
            session_created=True,
        )

    monkeypatch.setattr(orch_api, "fetch_active_flow_alias", _fake_fetch_alias)
    monkeypatch.setattr(orch_api, "_trigger_orch_for_workspace", _fake_trigger)

    response = await orch_api.trigger_orch(
        alias_or_flow_uuid="1a2b3c4d5e6f70",
        payload={},
        db_session=object(),  # type: ignore[arg-type]
    )

    assert response.accepted is True
    assert captured["workspace_uuid"] == "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
    assert str(captured["flow_uuid"]) == "0300054c-5f39-4cda-ae88-fe993fd9044b"


@pytest.mark.asyncio
async def test_trigger_orch_alias_not_found_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_fetch_alias(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(orch_api, "fetch_active_flow_alias", _fake_fetch_alias)

    with pytest.raises(HTTPException) as exc:
        await orch_api.trigger_orch(
            alias_or_flow_uuid="1a2b3c4d5e6f70",
            payload={},
            db_session=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_trigger_orch_invalid_target_returns_422() -> None:
    with pytest.raises(HTTPException) as exc:
        await orch_api.trigger_orch(
            alias_or_flow_uuid="not-valid-target",
            payload={},
            db_session=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_trigger_orch_uuid_fallback_keeps_legacy_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    flow_uuid = str(uuid4())

    def _fake_legacy_workspace_context():  # type: ignore[no-untyped-def]
        return "ba7eb0ec-e565-447c-8c11-8f870cf72a60", "ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60"

    async def _fake_trigger(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        captured.update(_kwargs)
        return OrchTriggerAccepted(
            status="accepted",
            accepted=True,
            flow_uuid=flow_uuid,
            app="GenericApp",
            persistence="saved",
            extracted=SessionExtraction(
                entity="e",
                entity_type="person",
                entity_address="addr",
                entity_session_id="sid",
            ),
            session_id=1,
            session_uuid=str(uuid4()),
            session_state=0,
            session_created=True,
        )

    monkeypatch.setattr(orch_api, "_legacy_workspace_context", _fake_legacy_workspace_context)
    monkeypatch.setattr(orch_api, "_trigger_orch_for_workspace", _fake_trigger)

    response = await orch_api.trigger_orch(
        alias_or_flow_uuid=flow_uuid,
        payload={},
        db_session=SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert response.accepted is True
    assert captured["workspace_uuid"] == "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
    assert captured["validate_workspace"] is False
