from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

import app.services.workflow_m2_service as workflow_m2_service
import app.services.workflow_revision_service as workflow_revision_service
import app.services.workflow_runtime_service as workflow_runtime_service
from app.services.workflow_revision_service import resolve_workflow_revision_for_session


FLOW_UUID = "33333333-3333-3333-3333-333333333333"
PINNED_REVISION_ID = "44444444-4444-4444-4444-444444444444"
CURRENT_REVISION_ID = "55555555-5555-5555-5555-555555555555"
PINNED_FINISH_CARD = "11111111-1111-1111-1111-111111111111"
CURRENT_FINISH_CARD = "22222222-2222-2222-2222-222222222222"


class _Transaction:
    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        return False


class _LockResult:
    def scalar_one(self) -> bool:
        return True


class _Session:
    def in_transaction(self) -> bool:
        return False

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, *_args, **_kwargs) -> _LockResult:  # type: ignore[no-untyped-def]
        return _LockResult()


def _finish_definition(card_uuid: str) -> dict:
    return {
        "components": [
            {
                "ref_id": card_uuid,
                "component_id": "finish_flow",
                "parameters": {"result": card_uuid},
            }
        ],
        "branches": [],
    }


def _revision(revision_id: str, version: int, card_uuid: str) -> dict:
    return {
        "id": revision_id,
        "flow_id": FLOW_UUID,
        "version": version,
        "definition": _finish_definition(card_uuid),
        "selection_mode": "published",
    }


def _patch_common_m2_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime_variables: dict,
    persisted: list[dict],
    metrics: list[list[dict]],
) -> None:
    async def _fetch_session(*_args, **_kwargs) -> dict:  # type: ignore[no-untyped-def]
        return {
            "uuid": "66666666-6666-6666-6666-666666666666",
            "flow_uuid": FLOW_UUID,
            "state": 0,
            "runtime_variables": runtime_variables,
            "last_card_uuid": None,
            "next_card_uuid": PINNED_FINISH_CARD,
            "frozen_until": None,
            "whatsapp_sent_at": None,
            "whatsapp_delivered_at": None,
            "whatsapp_read_at": None,
            "whatsapp_failed_at": None,
        }

    async def _replace(*_args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        persisted.append(kwargs)

    async def _persist_metrics(*_args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        metrics.append(kwargs["metrics"])

    monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(
        workflow_m2_service,
        "fetch_flow_row",
        AsyncMock(return_value={"id": FLOW_UUID}),
    )
    monkeypatch.setattr(workflow_m2_service, "fetch_session_workflow_state", _fetch_session)
    monkeypatch.setattr(
        workflow_m2_service,
        "fetch_contact_runtime_context_for_session",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(workflow_m2_service, "replace_session_workflow_state", _replace)
    monkeypatch.setattr(workflow_m2_service, "persist_session_metrics", _persist_metrics)


@pytest.mark.asyncio
async def test_new_session_bootstrap_pins_revision_selected_after_publication(monkeypatch) -> None:
    current_revision = _revision(CURRENT_REVISION_ID, 8, CURRENT_FINISH_CARD)
    update_position = AsyncMock()

    monkeypatch.setattr(
        workflow_runtime_service,
        "get_settings",
        lambda: SimpleNamespace(workflow_v2_enabled=True),
    )
    monkeypatch.setattr(
        workflow_runtime_service,
        "fetch_session_workflow_state",
        AsyncMock(
            return_value={
                "runtime_variables": {},
                "next_card_uuid": None,
            }
        ),
    )
    monkeypatch.setattr(
        workflow_runtime_service,
        "fetch_flow_row",
        AsyncMock(return_value={"id": FLOW_UUID}),
    )
    monkeypatch.setattr(
        workflow_runtime_service,
        "fetch_selected_revision",
        AsyncMock(return_value=current_revision),
    )
    monkeypatch.setattr(workflow_runtime_service, "update_session_workflow_position", update_position)

    result = await workflow_runtime_service.bootstrap_workflow_for_session(
        _Session(),
        flow_uuid=FLOW_UUID,
        session_id=122,
        payload={"document": "12345678901"},
    )

    assert result.revision_id == CURRENT_REVISION_ID
    assert result.revision_version == 8
    assert result.next_card_uuid == CURRENT_FINISH_CARD
    runtime_patch = json.loads(update_position.await_args.kwargs["runtime_patch_json"])
    assert runtime_patch["workflow_v2"]["revision_id"] == CURRENT_REVISION_ID


@pytest.mark.asyncio
async def test_revision_resolver_uses_declared_revision_without_reading_current(monkeypatch) -> None:
    pinned_revision = _revision(PINNED_REVISION_ID, 7, PINNED_FINISH_CARD)
    fetch_by_id = AsyncMock(return_value=pinned_revision)
    fetch_selected = AsyncMock(return_value=_revision(CURRENT_REVISION_ID, 8, CURRENT_FINISH_CARD))
    monkeypatch.setattr(workflow_revision_service, "fetch_revision_by_id", fetch_by_id)
    monkeypatch.setattr(workflow_revision_service, "fetch_selected_revision", fetch_selected)

    result = await resolve_workflow_revision_for_session(
        AsyncMock(),
        flow_id=FLOW_UUID,
        runtime_variables={"workflow_v2": {"revision_id": PINNED_REVISION_ID}},
    )

    assert result.revision == pinned_revision
    assert result.source == "pinned"
    assert result.failure_reason is None
    fetch_by_id.assert_awaited_once_with(
        ANY,
        flow_id=FLOW_UUID,
        revision_id=PINNED_REVISION_ID,
    )
    fetch_selected.assert_not_awaited()


@pytest.mark.asyncio
async def test_m2_executes_pinned_revision_after_current_revision_changes(monkeypatch) -> None:
    runtime_variables = {
        "workflow_v2": {
            "revision_id": PINNED_REVISION_ID,
            "revision_version": 7,
            "revision_mode": "published",
            "next_card_cursor": PINNED_FINISH_CARD,
        }
    }
    persisted: list[dict] = []
    metrics: list[list[dict]] = []
    _patch_common_m2_dependencies(
        monkeypatch,
        runtime_variables=runtime_variables,
        persisted=persisted,
        metrics=metrics,
    )

    fetch_by_id = AsyncMock(return_value=_revision(PINNED_REVISION_ID, 7, PINNED_FINISH_CARD))
    fetch_selected = AsyncMock(return_value=_revision(CURRENT_REVISION_ID, 8, CURRENT_FINISH_CARD))
    monkeypatch.setattr(workflow_revision_service, "fetch_revision_by_id", fetch_by_id)
    monkeypatch.setattr(workflow_revision_service, "fetch_selected_revision", fetch_selected)

    result = await workflow_m2_service.execute_workflow_m2_for_session(
        _Session(),
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.last_card_uuid == PINNED_FINISH_CARD
    assert persisted[-1]["state"] == 3
    assert metrics[-1][-1]["revision_id"] == PINNED_REVISION_ID
    fetch_by_id.assert_awaited_once()
    fetch_selected.assert_not_awaited()


@pytest.mark.asyncio
async def test_m2_terminalizes_invalid_pinned_revision_without_falling_back(monkeypatch) -> None:
    runtime_variables = {
        "workflow_v2": {
            "revision_id": "not-a-uuid",
            "next_card_cursor": PINNED_FINISH_CARD,
        }
    }
    persisted: list[dict] = []
    metrics: list[list[dict]] = []
    _patch_common_m2_dependencies(
        monkeypatch,
        runtime_variables=runtime_variables,
        persisted=persisted,
        metrics=metrics,
    )

    fetch_by_id = AsyncMock()
    fetch_selected = AsyncMock(return_value=_revision(CURRENT_REVISION_ID, 8, CURRENT_FINISH_CARD))
    monkeypatch.setattr(workflow_revision_service, "fetch_revision_by_id", fetch_by_id)
    monkeypatch.setattr(workflow_revision_service, "fetch_selected_revision", fetch_selected)

    result = await workflow_m2_service.execute_workflow_m2_for_session(
        _Session(),
        flow_uuid=FLOW_UUID,
        session_id=124,
    )

    assert result.stopped_reason == "pinned_revision_invalid"
    assert result.next_card_uuid is None
    assert persisted[-1]["state"] == 3
    assert persisted[-1]["next_card_uuid"] is None
    assert runtime_variables["workflow_v2"]["terminal_failure"]["code"] == "pinned_revision_invalid"
    fetch_by_id.assert_not_awaited()
    fetch_selected.assert_not_awaited()


@pytest.mark.asyncio
async def test_m2_terminalizes_missing_pinned_revision_without_falling_back(monkeypatch) -> None:
    runtime_variables = {
        "workflow_v2": {
            "revision_id": PINNED_REVISION_ID,
            "next_card_cursor": PINNED_FINISH_CARD,
        }
    }
    persisted: list[dict] = []
    metrics: list[list[dict]] = []
    _patch_common_m2_dependencies(
        monkeypatch,
        runtime_variables=runtime_variables,
        persisted=persisted,
        metrics=metrics,
    )

    fetch_by_id = AsyncMock(return_value=None)
    fetch_selected = AsyncMock(return_value=_revision(CURRENT_REVISION_ID, 8, CURRENT_FINISH_CARD))
    monkeypatch.setattr(workflow_revision_service, "fetch_revision_by_id", fetch_by_id)
    monkeypatch.setattr(workflow_revision_service, "fetch_selected_revision", fetch_selected)

    result = await workflow_m2_service.execute_workflow_m2_for_session(
        _Session(),
        flow_uuid=FLOW_UUID,
        session_id=126,
    )

    assert result.stopped_reason == "pinned_revision_not_found"
    assert persisted[-1]["state"] == 3
    assert runtime_variables["workflow_v2"]["terminal_failure"]["code"] == "pinned_revision_not_found"
    fetch_by_id.assert_awaited_once()
    fetch_selected.assert_not_awaited()


@pytest.mark.asyncio
async def test_m2_pins_current_revision_for_legacy_session_before_execution(monkeypatch) -> None:
    runtime_variables = {
        "workflow_v2": {
            "next_card_cursor": PINNED_FINISH_CARD,
        }
    }
    persisted: list[dict] = []
    metrics: list[list[dict]] = []
    _patch_common_m2_dependencies(
        monkeypatch,
        runtime_variables=runtime_variables,
        persisted=persisted,
        metrics=metrics,
    )

    selected_revision = _revision(PINNED_REVISION_ID, 7, PINNED_FINISH_CARD)
    fetch_selected = AsyncMock(return_value=selected_revision)
    fetch_by_id = AsyncMock(return_value=selected_revision)
    monkeypatch.setattr(workflow_revision_service, "fetch_selected_revision", fetch_selected)
    monkeypatch.setattr(workflow_revision_service, "fetch_revision_by_id", fetch_by_id)

    async def _ensure_pin(*_args, **kwargs) -> dict:  # type: ignore[no-untyped-def]
        runtime_variables["workflow_v2"].update(kwargs["revision_patch"])
        return runtime_variables

    ensure_pin = AsyncMock(side_effect=_ensure_pin)
    monkeypatch.setattr(workflow_m2_service, "ensure_session_workflow_revision_pin", ensure_pin)

    result = await workflow_m2_service.execute_workflow_m2_for_session(
        _Session(),
        flow_uuid=FLOW_UUID,
        session_id=125,
    )

    assert result.stopped_reason == "finished_by_component"
    assert runtime_variables["workflow_v2"]["revision_id"] == PINNED_REVISION_ID
    assert runtime_variables["workflow_v2"]["revision_version"] == 7
    ensure_pin.assert_awaited_once()
    fetch_selected.assert_awaited_once()
    fetch_by_id.assert_awaited_once()
