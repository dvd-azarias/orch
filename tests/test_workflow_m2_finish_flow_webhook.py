from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

import app.services.workflow_m2_service as workflow_m2_service
from app.services.workflow_m2_service import (
    _dispatch_finish_flow_webhook,
    execute_workflow_m2_for_session,
)
from app.services.workflow_engine import definition_has_finish_flow_webhook


def _component(webhook: str | None = "https://example.test/hook") -> dict:
    return {
        "ref_id": "finish-1",
        "component_id": "finish_flow",
        "parameters": {"webhook": webhook, "result": "success"},
    }


def _session_state(runtime_variables: dict) -> dict:
    return {
        "id": 6941,
        "uuid": "55555555-5555-5555-5555-555555555555",
        "flow_uuid": "3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17",
        "state": 2,
        "entity_origin_app": "DialerApp",
        "entity_address": "5511975620806",
        "runtime_variables": runtime_variables,
        "started_at": datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc),
    }


def test_definition_marks_session_only_when_finish_flow_has_webhook() -> None:
    assert definition_has_finish_flow_webhook({"components": [_component()]}) is True
    assert definition_has_finish_flow_webhook({"components": [_component(None)]}) is False


@pytest.mark.asyncio
async def test_finish_flow_posts_single_cdr_and_clears_it_after_2xx(monkeypatch) -> None:
    cdr = {"hangup": {"Disposition": "ANSWERED", "BillableSeconds": 18}}
    runtime_variables = {
        "variables": {"customs": {"protocol": "abc"}},
        "cdr": cdr,
    }
    captured: dict = {}

    def _http_execute(req, timeout_seconds):  # type: ignore[no-untyped-def]
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout_seconds
        return 204, {}, "", None

    monkeypatch.setattr(workflow_m2_service, "_http_execute", _http_execute)

    result = await _dispatch_finish_flow_webhook(
        component=_component(),
        session_state=_session_state(runtime_variables),
        runtime_variables=runtime_variables,
        finished_at=datetime(2026, 8, 24, 20, 5, tzinfo=timezone.utc),
    )

    assert result is not None and result["success"] is True
    assert captured["payload"]["cdr"] == cdr
    assert "runtime_variables" not in captured["payload"]
    assert captured["payload"]["entity_address"] == "5511975620806"
    assert captured["timeout"] == 5.0
    assert "cdr" not in runtime_variables


@pytest.mark.asyncio
async def test_finish_flow_keeps_cdr_when_webhook_is_not_confirmed(monkeypatch) -> None:
    cdr = {"hangup": {"Disposition": "FAILED"}}
    runtime_variables = {"cdr": cdr}
    monkeypatch.setattr(
        workflow_m2_service,
        "_http_execute",
        lambda *_args, **_kwargs: (503, {}, "unavailable", "HTTP 503"),
    )

    result = await _dispatch_finish_flow_webhook(
        component=_component(),
        session_state=_session_state(runtime_variables),
        runtime_variables=runtime_variables,
        finished_at=datetime.now(timezone.utc),
    )

    assert result is not None and result["success"] is False
    assert runtime_variables["cdr"] == cdr


@pytest.mark.asyncio
async def test_finish_flow_uses_persisted_session_cdr(monkeypatch) -> None:
    persisted_cdr = {"uniqueid": "GW02-later.1", "hangup": {"Disposition": "BUSY"}}
    runtime_variables = {
        "cdr": persisted_cdr,
        "last_payload": {"uniqueid": "GW01-older.1", "hangup": {"Disposition": "NO ANSWER"}},
    }
    captured: dict = {}

    def _http_execute(req, timeout_seconds):  # type: ignore[no-untyped-def]
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return 200, {}, "", None

    monkeypatch.setattr(workflow_m2_service, "_http_execute", _http_execute)

    await _dispatch_finish_flow_webhook(
        component=_component(),
        session_state=_session_state(runtime_variables),
        runtime_variables=runtime_variables,
        finished_at=datetime.now(timezone.utc),
    )

    assert captured["payload"]["cdr"] == persisted_cdr


@pytest.mark.asyncio
async def test_finish_flow_does_not_repeat_confirmed_webhook(monkeypatch) -> None:
    runtime_variables = {
        "cdr": {"hangup": {"Disposition": "NO ANSWER"}},
        "finish_flow_webhook": {
            "success": True,
            "status_code": 200,
            "url": "https://example.test/hook",
        },
    }

    def _unexpected_http_execute(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("webhook confirmado nao pode ser reenviado")

    monkeypatch.setattr(workflow_m2_service, "_http_execute", _unexpected_http_execute)

    result = await _dispatch_finish_flow_webhook(
        component=_component(),
        session_state=_session_state(runtime_variables),
        runtime_variables=runtime_variables,
        finished_at=datetime.now(timezone.utc),
    )

    assert result is not None and result["skipped"] is True
    assert "cdr" not in runtime_variables


@pytest.mark.asyncio
async def test_execute_finish_flow_dispatches_persisted_terminal_snapshot(monkeypatch) -> None:
    flow_uuid = "33333333-3333-3333-3333-333333333333"
    finish_ref = "22222222-2222-2222-2222-222222222222"
    runtime_variables = {
        "workflow_v2": {"next_card_cursor": finish_ref},
        "cdr": {"hangup": {"Disposition": "ANSWERED"}},
    }
    definition = {
        "components": [
            {
                "ref_id": finish_ref,
                "component_id": "finish_flow",
                "parameters": {
                    "webhook": "https://example.test/hook",
                    "result": "success",
                },
            }
        ]
    }
    persisted: list[dict] = []
    dispatched: list[dict] = []
    marked_processed: list[dict] = []

    class _Transaction:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
            return False

    class _Result:
        def scalar_one(self) -> bool:
            return True

    class _Session:
        def in_transaction(self) -> bool:
            return False

        def begin(self) -> _Transaction:
            return _Transaction()

        async def execute(self, *_args, **_kwargs) -> _Result:  # type: ignore[no-untyped-def]
            return _Result()

    async def _fetch_flow(*_args, **_kwargs) -> dict:  # type: ignore[no-untyped-def]
        return {"id": flow_uuid}

    async def _fetch_revision(*_args, **_kwargs) -> dict:  # type: ignore[no-untyped-def]
        return {"id": "44444444-4444-4444-4444-444444444444", "definition": definition}

    async def _fetch_session(*_args, **_kwargs) -> dict:  # type: ignore[no-untyped-def]
        return {
            "uuid": "55555555-5555-5555-5555-555555555555",
            "state": 2,
            "runtime_variables": runtime_variables,
            "last_card_uuid": None,
            "next_card_uuid": finish_ref,
            "frozen_until": None,
        }

    async def _fetch_contact(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    async def _fetch_snapshot(*_args, **_kwargs) -> dict:  # type: ignore[no-untyped-def]
        assert persisted[-1]["state"] == 3
        assert persisted[-1]["runtime_variables"]["cdr"] == {
            "hangup": {"Disposition": "ANSWERED"}
        }
        return {
            "id": 6941,
            "uuid": "55555555-5555-5555-5555-555555555555",
            "entity_address": "5511975620806",
            "runtime_variables": copy.deepcopy(persisted[-1]["runtime_variables"]),
            "state": persisted[-1]["state"],
        }

    async def _replace(*_args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        persisted.append(copy.deepcopy(kwargs))

    async def _persist_metrics(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        return None

    async def _mark_processed(*_args, **kwargs) -> int:  # type: ignore[no-untyped-def]
        marked_processed.append(kwargs)
        return 1

    def _http_execute(req, timeout_seconds):  # type: ignore[no-untyped-def]
        dispatched.append(json.loads(req.data.decode("utf-8")))
        return 204, {}, "", None

    monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(workflow_m2_service, "fetch_flow_row", _fetch_flow)
    monkeypatch.setattr(workflow_m2_service, "fetch_selected_revision", _fetch_revision)
    monkeypatch.setattr(workflow_m2_service, "fetch_session_workflow_state", _fetch_session)
    monkeypatch.setattr(workflow_m2_service, "fetch_session_webhook_snapshot", _fetch_snapshot)
    monkeypatch.setattr(workflow_m2_service, "fetch_contact_runtime_context_for_session", _fetch_contact)
    monkeypatch.setattr(workflow_m2_service, "replace_session_workflow_state", _replace)
    monkeypatch.setattr(workflow_m2_service, "persist_session_metrics", _persist_metrics)
    monkeypatch.setattr(workflow_m2_service, "mark_pending_channel_events_processed", _mark_processed)
    monkeypatch.setattr(workflow_m2_service, "_http_execute", _http_execute)

    result = await execute_workflow_m2_for_session(
        _Session(),
        flow_uuid=flow_uuid,
        session_id=6941,
    )

    assert result.stopped_reason == "finished_by_component"
    assert len(dispatched) == 1
    assert marked_processed == [{"session_id": 6941, "channel": "dialer"}]
    assert dispatched[0]["cdr"] == {"hangup": {"Disposition": "ANSWERED"}}
    assert "session" not in dispatched[0]
    assert "runtime_variables" not in dispatched[0]
    assert dispatched[0]["state"] == 3
    assert len(persisted) == 2
    assert persisted[0]["runtime_variables"]["cdr"] == {
        "hangup": {"Disposition": "ANSWERED"}
    }
    assert persisted[-1]["state"] == 3
    assert "cdr" not in persisted[-1]["runtime_variables"]
