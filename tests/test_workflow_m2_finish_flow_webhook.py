from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

import app.services.workflow_m2_service as workflow_m2_service
from app.services.workflow_m2_service import (
    _dispatch_finish_flow_webhook,
    _finish_flow_requires_dialer_cdr,
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


def test_finish_flow_requires_cdr_for_effective_dialer_session_without_dialer_cards() -> None:
    assert (
        _finish_flow_requires_dialer_cdr(
            runtime_variables={"source_app": "DialerApp"},
            cdr_event=None,
        )
        is True
    )


def test_finish_flow_does_not_require_cdr_only_because_flow_is_mixed() -> None:
    assert (
        _finish_flow_requires_dialer_cdr(
            runtime_variables={"source_app": "GenericApp", "cdr": {"stale": True}},
            cdr_event=None,
        )
        is False
    )


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
        captured["idempotency_key"] = req.get_header("Idempotency-key")
        return 204, {}, "", None

    monkeypatch.setattr(workflow_m2_service, "_http_execute", _http_execute)

    result = await _dispatch_finish_flow_webhook(
        component=_component(),
        session_state=_session_state(runtime_variables),
        runtime_variables=runtime_variables,
        finished_at=datetime(2026, 8, 24, 20, 5, tzinfo=timezone.utc),
        cdr=cdr,
        contact_state={
            "contact_list_member_id": 10655,
            "contact_list_id": "dc7dc1c1-2c98-42e9-a788-5d186f458daa",
            "mailing_id": "1115",
            "contact_identifier": "30392286855",
            "contact_name": "DEIVID AZARIAS",
            "contact_full_name": "DEIVID AZARIAS",
            "contact_channel_type": "voice",
            "contact_channel_label": "tel1",
            "contact_channel_address": "5511975620806",
            "contact_channel_extra_data": {"Bilhete": "292638032"},
        },
    )

    assert result is not None and result["success"] is True
    assert captured["payload"]["cdr"] == cdr
    assert set(captured["payload"]) == {"session", "cdr"}
    assert "runtime_variables" not in captured["payload"]["session"]
    assert captured["payload"]["session"]["entity_address"] == "5511975620806"
    assert captured["payload"]["session"]["contact"] == {
        "id": 10655,
        "contact_list_id": "dc7dc1c1-2c98-42e9-a788-5d186f458daa",
        "mailing_id": "1115",
        "identifier": "30392286855",
        "name": "DEIVID AZARIAS",
        "full_name": "DEIVID AZARIAS",
        "gender": None,
        "country": None,
        "province": None,
        "city": None,
        "birth_date": None,
        "age": None,
        "person_uuid": None,
        "channel": {"type": "voice", "label": "tel1", "address": "5511975620806"},
        "extra": {"Bilhete": "292638032"},
    }
    assert captured["timeout"] == 5.0
    assert captured["idempotency_key"] == "orch-finish-flow:55555555-5555-5555-5555-555555555555:finish-1"
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
        cdr=cdr,
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
        cdr=persisted_cdr,
    )

    assert captured["payload"]["cdr"] == persisted_cdr


@pytest.mark.asyncio
async def test_finish_flow_defers_dialer_webhook_without_cdr(monkeypatch) -> None:
    runtime_variables: dict = {}

    def _unexpected_http_execute(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("webhook de voz não pode sair sem CDR")

    monkeypatch.setattr(workflow_m2_service, "_http_execute", _unexpected_http_execute)

    result = await _dispatch_finish_flow_webhook(
        component=_component(),
        session_state=_session_state(runtime_variables),
        runtime_variables=runtime_variables,
        finished_at=datetime.now(timezone.utc),
        cdr_required=True,
    )

    assert result is not None
    assert result["success"] is False
    assert result["deferred"] is True
    assert result["error"] == "dialer_cdr_not_available"


@pytest.mark.asyncio
async def test_finish_flow_does_not_export_residual_runtime_cdr(monkeypatch) -> None:
    runtime_variables = {"source_app": "GenericApp", "cdr": {"stale": True}}
    captured: dict = {}

    def _http_execute(req, timeout_seconds):  # type: ignore[no-untyped-def]
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return 200, {}, "", None

    monkeypatch.setattr(workflow_m2_service, "_http_execute", _http_execute)

    result = await _dispatch_finish_flow_webhook(
        component=_component(),
        session_state=_session_state(runtime_variables),
        runtime_variables=runtime_variables,
        finished_at=datetime.now(timezone.utc),
        cdr=None,
        cdr_required=False,
    )

    assert result is not None and result["success"] is True
    assert captured["payload"]["cdr"] is None


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
async def test_finish_flow_posts_each_distinct_dialer_cdr_for_same_session(monkeypatch) -> None:
    first_event_identity = "GW02-first.1"
    second_event_identity = "GW01-retry.2"
    second_event_row_id = 13907
    second_cdr = {"uniqueid": "GW01-retry.2", "hangup": {"Disposition": "NO ANSWER"}}
    runtime_variables = {
        "finish_flow_webhook": {
            "success": True,
            "status_code": 200,
            "url": "https://example.test/hook",
            "channel_event_identity": first_event_identity,
            "channel_event_row_id": 13906,
        }
    }
    captured: dict = {}

    def _http_execute(req, timeout_seconds):  # type: ignore[no-untyped-def]
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["idempotency_key"] = req.get_header("Idempotency-key")
        return 200, {}, "", None

    monkeypatch.setattr(workflow_m2_service, "_http_execute", _http_execute)

    result = await _dispatch_finish_flow_webhook(
        component=_component(),
        session_state=_session_state(runtime_variables),
        runtime_variables=runtime_variables,
        finished_at=datetime.now(timezone.utc),
        cdr=second_cdr,
        cdr_required=True,
        cdr_event_id=second_event_identity,
        cdr_event_row_id=second_event_row_id,
    )

    assert result is not None and result["success"] is True
    assert result["channel_event_identity"] == second_event_identity
    assert result["channel_event_row_id"] == second_event_row_id
    assert captured["payload"]["cdr"] == second_cdr
    assert captured["idempotency_key"].endswith(f":{second_event_identity}")


@pytest.mark.asyncio
async def test_finish_flow_does_not_repeat_already_dispatched_dialer_cdr(monkeypatch) -> None:
    runtime_variables = {
        "cdr": {"uniqueid": "GW01-duplicate.1"},
        "finish_flow_webhook": {
            "success": True,
            "status_code": 200,
            "url": "https://example.test/hook",
            "channel_event_identity": "GW01-duplicate.1",
            "channel_event_row_id": 13906,
        },
    }

    def _unexpected_http_execute(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("CDR já confirmado não pode ser reenviado")

    monkeypatch.setattr(workflow_m2_service, "_http_execute", _unexpected_http_execute)

    result = await _dispatch_finish_flow_webhook(
        component=_component(),
        session_state=_session_state(runtime_variables),
        runtime_variables=runtime_variables,
        finished_at=datetime.now(timezone.utc),
        cdr={"uniqueid": "GW01-duplicate.1"},
        cdr_required=True,
        cdr_event_id="GW01-duplicate.1",
        cdr_event_row_id=13906,
        cdr_event_dispatched=True,
    )

    assert result is not None and result["skipped"] is True
    assert result["channel_event_identity"] == "GW01-duplicate.1"
    assert result["channel_event_row_id"] == 13906
    assert "cdr" not in runtime_variables


@pytest.mark.asyncio
async def test_finish_flow_keeps_legacy_confirmed_session_one_time(monkeypatch) -> None:
    runtime_variables = {
        "cdr": {"uniqueid": "GW01-new.1"},
        "finish_flow_webhook": {
            "success": True,
            "status_code": 200,
            "url": "https://example.test/hook",
        },
    }

    def _unexpected_http_execute(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("sessão legada confirmada não pode ser reenviada sem identidade CDR")

    monkeypatch.setattr(workflow_m2_service, "_http_execute", _unexpected_http_execute)

    result = await _dispatch_finish_flow_webhook(
        component=_component(),
        session_state=_session_state(runtime_variables),
        runtime_variables=runtime_variables,
        finished_at=datetime.now(timezone.utc),
        cdr=runtime_variables["cdr"],
        cdr_required=True,
        cdr_event_id="GW01-new.1",
        cdr_event_row_id=13907,
    )

    assert result is not None and result["skipped"] is True
    assert "cdr" not in runtime_variables


@pytest.mark.asyncio
async def test_execute_finish_flow_dispatches_persisted_terminal_snapshot(monkeypatch) -> None:
    flow_uuid = "33333333-3333-3333-3333-333333333333"
    finish_ref = "22222222-2222-2222-2222-222222222222"
    send_dialer_ref = "11111111-1111-1111-1111-111111111111"
    ledger_cdr = {
        "uniqueid": "GW02-later.1",
        "hangup": {
            "DialerActionID": "59456802-f1b0-414a-b4ee-6af5ca3502e5",
            "Disposition": "NO ANSWER",
        },
    }
    runtime_variables = {
        "workflow_v2": {"next_card_cursor": finish_ref},
    }
    definition = {
        "components": [
            {
                "ref_id": send_dialer_ref,
                "component_id": "send_with_dialer",
                "parameters": {},
            },
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

    async def _fetch_contact(*_args, **_kwargs) -> dict:  # type: ignore[no-untyped-def]
        return {
            "contact_list_member_id": 10655,
            "contact_identifier": "30392286855",
            "contact_name": "DEIVID AZARIAS",
            "contact_full_name": "DEIVID AZARIAS",
            "contact_channel_type": "voice",
            "contact_channel_label": "tel1",
            "contact_channel_address": "5511975620806",
            "contact_channel_extra_data": {},
        }

    async def _fetch_snapshot(*_args, **_kwargs) -> dict:  # type: ignore[no-untyped-def]
        assert persisted[-1]["state"] == 3
        return {
            "id": 6941,
            "uuid": "55555555-5555-5555-5555-555555555555",
            "entity_address": "5511975620806",
            "runtime_variables": copy.deepcopy(persisted[-1]["runtime_variables"]),
            "state": persisted[-1]["state"],
        }

    async def _fetch_pending_event(*_args, **_kwargs) -> dict:  # type: ignore[no-untyped-def]
        return {
            "id": 13904,
            "channel": "dialer",
            "event_type": "machine",
            "event_id": "GW02-later.1",
            "payload": copy.deepcopy(ledger_cdr),
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
    monkeypatch.setattr(workflow_m2_service, "fetch_next_pending_channel_event", _fetch_pending_event)
    monkeypatch.setattr(workflow_m2_service, "fetch_contact_runtime_context_for_session", _fetch_contact)
    monkeypatch.setattr(workflow_m2_service, "replace_session_workflow_state", _replace)
    monkeypatch.setattr(workflow_m2_service, "persist_session_metrics", _persist_metrics)
    monkeypatch.setattr(workflow_m2_service, "mark_channel_event_processed", _mark_processed)
    monkeypatch.setattr(workflow_m2_service, "_http_execute", _http_execute)

    result = await execute_workflow_m2_for_session(
        _Session(),
        flow_uuid=flow_uuid,
        session_id=6941,
    )

    assert result.stopped_reason == "finished_by_component"
    assert len(dispatched) == 1
    assert marked_processed == [
        {
            "event_row_id": 13904,
            "session_id": 6941,
            "channel": "dialer",
            "discard_reason": "finish_flow_webhook_dispatched",
        }
    ]
    assert dispatched[0]["cdr"] == ledger_cdr
    assert "runtime_variables" not in dispatched[0]["session"]
    assert dispatched[0]["session"]["state"] == 3
    assert dispatched[0]["session"]["contact"]["id"] == 10655
    assert len(persisted) == 2
    assert "cdr" not in persisted[0]["runtime_variables"]
    assert persisted[-1]["state"] == 3
    assert "cdr" not in persisted[-1]["runtime_variables"]
