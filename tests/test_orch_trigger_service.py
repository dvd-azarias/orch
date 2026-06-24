from __future__ import annotations

from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_process_single_payload_callback_updates_existing_session(monkeypatch) -> None:
    monkeypatch.setattr(
        orch_trigger_service,
        "extract_session_fields",
        lambda app_name, payload: SessionExtraction(
            entity="30392287848",
            entity_type="person",
            entity_address="30392287848",
            entity_session_id="30392287848",
        ),
    )

    async def _fake_persist_callback(*args, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            id=77,
            uuid="d60e2f8a-7a56-4f2e-b20b-d3b1d40b9f2a",
            state=1,
            created=False,
        )

    async def _fail_if_called(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("persist_session não deveria ser chamado para callback")

    async def _fake_persist_channel_events(*args, **kwargs):  # type: ignore[no-untyped-def]
        return 0

    monkeypatch.setattr(orch_trigger_service, "persist_callback_event_for_active_entity", _fake_persist_callback)
    monkeypatch.setattr(orch_trigger_service, "persist_session", _fail_if_called)
    monkeypatch.setattr(orch_trigger_service, "persist_channel_events", _fake_persist_channel_events)
    monkeypatch.setattr(
        orch_trigger_service,
        "bootstrap_workflow_for_session",
        lambda *a, **k: orch_trigger_service.WorkflowBootstrapResult(
            enabled=True,
            loaded=False,
            reason="already_bootstrapped",
            flow_id="0300054c-5f39-4cda-ae88-fe993fd9044b",
            revision_id="3cc52a68-47f8-4142-9104-69388c0f274f",
            revision_version=1,
            revision_mode="published",
            next_card_uuid="card-1",
        ),
    )

    response = await orch_trigger_service.process_single_payload(
        safe_workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        workspace_schema="ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="0300054c-5f39-4cda-ae88-fe993fd9044b",
        payload={"event_name": "callback", "entity": "30392287848", "result": "success"},
        db_session=_DummySession(),
        app_name="GenericApp",
    )

    assert response.accepted is True
    assert response.session_id == 77
    assert response.session_created is False


@pytest.mark.asyncio
async def test_process_single_payload_callback_ignores_when_session_not_found(monkeypatch) -> None:
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        orch_trigger_service,
        "extract_session_fields",
        lambda app_name, payload: SessionExtraction(
            entity="30392287848",
            entity_type="person",
            entity_address="30392287848",
            entity_session_id="30392287848",
        ),
    )

    async def _fake_persist_callback(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def _fake_discard(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["reason"] = str(kwargs.get("discard_reason"))

    monkeypatch.setattr(orch_trigger_service, "persist_callback_event_for_active_entity", _fake_persist_callback)
    monkeypatch.setattr(orch_trigger_service, "persist_discarded_event", _fake_discard)

    response = await orch_trigger_service.process_single_payload(
        safe_workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        workspace_schema="ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="0300054c-5f39-4cda-ae88-fe993fd9044b",
        payload={"event_name": "callback", "entity": "30392287848", "result": "success"},
        db_session=_DummySession(),
        app_name="GenericApp",
    )

    assert response.accepted is False
    assert response.status == "ignored"
    assert captured["reason"] == "callback_session_not_found"


@pytest.mark.asyncio
async def test_process_single_payload_dialer_hangup_updates_existing_session_by_address(monkeypatch) -> None:
    monkeypatch.setattr(
        orch_trigger_service,
        "extract_session_fields",
        lambda app_name, payload: SessionExtraction(
            entity="dialer-action-1",
            entity_type="person",
            entity_address="5511975620806",
            entity_session_id="GW02-1781218669.3310",
        ),
    )

    async def _fake_persist_hangup(*args, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            id=6260,
            uuid="96ce9a8e-9b26-4f3a-96e1-f019efb3d048",
            state=1,
            created=False,
        )

    async def _fail_if_called(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("persist_session não deveria ser chamado para hangup do run_flow")

    async def _fake_persist_channel_events(*args, **kwargs):  # type: ignore[no-untyped-def]
        return 0

    monkeypatch.setattr(
        orch_trigger_service,
        "persist_run_flow_event_for_active_entity_address",
        _fake_persist_hangup,
    )
    monkeypatch.setattr(orch_trigger_service, "persist_session", _fail_if_called)
    monkeypatch.setattr(orch_trigger_service, "persist_channel_events", _fake_persist_channel_events)
    monkeypatch.setattr(
        orch_trigger_service,
        "bootstrap_workflow_for_session",
        lambda *a, **k: orch_trigger_service.WorkflowBootstrapResult(
            enabled=True,
            loaded=False,
            reason="already_bootstrapped",
            flow_id="16ce4b08-b756-425a-a56d-a5c861580714",
            revision_id="3cc52a68-47f8-4142-9104-69388c0f274f",
            revision_version=1,
            revision_mode="published",
            next_card_uuid="card-1",
        ),
    )

    response = await orch_trigger_service.process_single_payload(
        safe_workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        workspace_schema="ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="16ce4b08-b756-425a-a56d-a5c861580714",
        payload={
            "uniqueid": "GW02-1781218669.3310",
            "hangup": {"Event": "Hangup", "Disposition": "ANSWERED"},
            "makecall": {"Event": "DialBegin", "DialString": "trunk-sbc-router106/5511975620806"},
        },
        db_session=_DummySession(),
        app_name="DialerApp",
    )

    assert response.accepted is True
    assert response.session_id == 6260
    assert response.session_created is False


@pytest.mark.asyncio
async def test_process_single_payload_dialer_hangup_ignores_when_session_not_found(monkeypatch) -> None:
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        orch_trigger_service,
        "extract_session_fields",
        lambda app_name, payload: SessionExtraction(
            entity="dialer-action-1",
            entity_type="person",
            entity_address="5511975620806",
            entity_session_id="GW02-1781218669.3310",
        ),
    )

    async def _fake_persist_hangup(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def _fake_discard(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["reason"] = str(kwargs.get("discard_reason"))

    async def _fake_fetch_flow_row(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(
        orch_trigger_service,
        "persist_run_flow_event_for_active_entity_address",
        _fake_persist_hangup,
    )
    monkeypatch.setattr(orch_trigger_service, "fetch_flow_row", _fake_fetch_flow_row)
    monkeypatch.setattr(orch_trigger_service, "persist_discarded_event", _fake_discard)

    response = await orch_trigger_service.process_single_payload(
        safe_workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        workspace_schema="ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="16ce4b08-b756-425a-a56d-a5c861580714",
        payload={
            "uniqueid": "GW02-1781218669.3310",
            "hangup": {"Event": "Hangup", "Disposition": "ANSWERED"},
            "makecall": {"Event": "DialBegin", "DialString": "trunk-sbc-router106/5511975620806"},
        },
        db_session=_DummySession(),
        app_name="DialerApp",
    )

    assert response.accepted is False
    assert response.status == "ignored"
    assert captured["reason"] == "run_flow_hangup_session_not_found_by_address"


@pytest.mark.asyncio
async def test_process_single_payload_dialer_hangup_resumes_recent_session_by_send_card(monkeypatch) -> None:
    captured: dict[str, str | int] = {}

    monkeypatch.setattr(
        orch_trigger_service,
        "extract_session_fields",
        lambda app_name, payload: SessionExtraction(
            entity="dialer-action-2",
            entity_type="person",
            entity_address="5511975620806",
            entity_session_id="GW02-1781218669.3311",
        ),
    )

    async def _fake_active_persist(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def _fake_recent_persist(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["resume_card_uuid"] = str(kwargs.get("resume_card_uuid"))
        captured["window_hours"] = int(kwargs.get("correlation_window_hours"))
        return SimpleNamespace(
            id=6646,
            uuid="49cc976b-8468-4d48-b85e-d2f6fbb61e47",
            state=1,
            created=False,
        )

    async def _fail_if_called(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("persist_session não deveria ser chamado para hangup do run_flow")

    async def _fake_persist_channel_events(*args, **kwargs):  # type: ignore[no-untyped-def]
        return 0

    async def _fake_fetch_flow_row(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {"id": "16ce4b08-b756-425a-a56d-a5c861580714"}

    async def _fake_fetch_selected_revision(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {
            "definition": {
                "components": [
                    {"ref_id": "send-dialer-card-1", "component": "send_with_dialer"},
                ]
            }
        }

    monkeypatch.setattr(
        orch_trigger_service,
        "persist_run_flow_event_for_active_entity_address",
        _fake_active_persist,
    )
    monkeypatch.setattr(
        orch_trigger_service,
        "persist_run_flow_event_for_recent_entity_address",
        _fake_recent_persist,
    )
    monkeypatch.setattr(orch_trigger_service, "fetch_flow_row", _fake_fetch_flow_row)
    monkeypatch.setattr(orch_trigger_service, "fetch_selected_revision", _fake_fetch_selected_revision)
    monkeypatch.setattr(
        orch_trigger_service,
        "get_settings",
        lambda: SimpleNamespace(workflow_dialer_event_correlation_window_hours=36),
    )
    monkeypatch.setattr(orch_trigger_service, "persist_session", _fail_if_called)
    monkeypatch.setattr(orch_trigger_service, "persist_channel_events", _fake_persist_channel_events)
    monkeypatch.setattr(
        orch_trigger_service,
        "bootstrap_workflow_for_session",
        lambda *a, **k: orch_trigger_service.WorkflowBootstrapResult(
            enabled=True,
            loaded=False,
            reason="already_bootstrapped",
            flow_id="16ce4b08-b756-425a-a56d-a5c861580714",
            revision_id="3cc52a68-47f8-4142-9104-69388c0f274f",
            revision_version=1,
            revision_mode="published",
            next_card_uuid="send-dialer-card-1",
        ),
    )

    response = await orch_trigger_service.process_single_payload(
        safe_workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        workspace_schema="ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="16ce4b08-b756-425a-a56d-a5c861580714",
        payload={
            "uniqueid": "GW02-1781218669.3311",
            "hangup": {"Event": "Hangup", "Disposition": "BUSY"},
            "makecall": {"Event": "DialBegin", "DialString": "trunk-sbc-router106/5511975620806"},
        },
        db_session=_DummySession(),
        app_name="DialerApp",
    )

    assert response.accepted is True
    assert response.status == "accepted"
    assert response.session_id == 6646
    assert captured["resume_card_uuid"] == "send-dialer-card-1"
    assert captured["window_hours"] == 36


@pytest.mark.asyncio
async def test_process_single_payload_dialer_hangup_ignores_when_send_card_is_not_unique(monkeypatch) -> None:
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        orch_trigger_service,
        "extract_session_fields",
        lambda app_name, payload: SessionExtraction(
            entity="dialer-action-3",
            entity_type="person",
            entity_address="5511975620806",
            entity_session_id="GW02-1781218669.3312",
        ),
    )

    async def _fake_active_persist(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def _fake_recent_persist(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("fallback por janela não deveria rodar sem send_with_dialer único")

    async def _fake_discard(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["reason"] = str(kwargs.get("discard_reason"))

    async def _fake_fetch_flow_row(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {"id": "16ce4b08-b756-425a-a56d-a5c861580714"}

    async def _fake_fetch_selected_revision(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {
            "definition": {
                "components": [
                    {"ref_id": "send-dialer-card-1", "component": "send_with_dialer"},
                    {"ref_id": "send-dialer-card-2", "component": "send_with_dialer"},
                ]
            }
        }

    monkeypatch.setattr(
        orch_trigger_service,
        "persist_run_flow_event_for_active_entity_address",
        _fake_active_persist,
    )
    monkeypatch.setattr(
        orch_trigger_service,
        "persist_run_flow_event_for_recent_entity_address",
        _fake_recent_persist,
    )
    monkeypatch.setattr(orch_trigger_service, "fetch_flow_row", _fake_fetch_flow_row)
    monkeypatch.setattr(orch_trigger_service, "fetch_selected_revision", _fake_fetch_selected_revision)
    monkeypatch.setattr(orch_trigger_service, "persist_discarded_event", _fake_discard)

    response = await orch_trigger_service.process_single_payload(
        safe_workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        workspace_schema="ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="16ce4b08-b756-425a-a56d-a5c861580714",
        payload={
            "uniqueid": "GW02-1781218669.3312",
            "hangup": {"Event": "Hangup", "Disposition": "NO ANSWER"},
            "makecall": {"Event": "DialBegin", "DialString": "trunk-sbc-router106/5511975620806"},
        },
        db_session=_DummySession(),
        app_name="DialerApp",
    )

    assert response.accepted is False
    assert response.status == "ignored"
    assert captured["reason"] == "run_flow_hangup_session_not_found_by_address"
