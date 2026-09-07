from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.services.workflow_m2_service as workflow
from app.services.workflow_revision_service import WorkflowRevisionResolution


FLOW_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REVISION_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SESSION_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
API_REF = "11111111-1111-1111-1111-111111111111"
FINISH_REF = "22222222-2222-2222-2222-222222222222"


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _Result:
    def scalar_one(self) -> bool:
        return True


class _Session:
    def in_transaction(self) -> bool:
        return False

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, *_args, **_kwargs) -> _Result:
        return _Result()


def _definition(*, with_exception_branch: bool) -> dict:
    branches = []
    components = [
        {
            "ref_id": API_REF,
            "component_id": "api_call",
            "parameters": {
                "request": {
                    "method": "POST",
                    "url": "{{contact.extra.callback_url}}",
                }
            },
        }
    ]
    if with_exception_branch:
        components.append(
            {"ref_id": FINISH_REF, "component_id": "finish_flow", "parameters": {}}
        )
        branches.append(
            {"from": API_REF, "to": FINISH_REF, "branch": "exception"}
        )
    return {"components": components, "branches": branches}


def _runtime() -> dict:
    return {
        "workflow_v2": {
            "flow_id": FLOW_UUID,
            "revision_id": REVISION_UUID,
            "next_card_cursor": API_REF,
        },
        "variables": {"payload": {}, "customs": {}},
    }


def _configure_execution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    definition: dict,
    runtime: dict,
) -> tuple[list[dict], AsyncMock]:
    persisted: list[dict] = []
    persist_metrics = AsyncMock()

    monkeypatch.setattr(workflow, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(workflow, "fetch_flow_row", AsyncMock(return_value={"id": FLOW_UUID}))
    monkeypatch.setattr(
        workflow,
        "resolve_workflow_revision_for_session",
        AsyncMock(
            return_value=WorkflowRevisionResolution(
                revision={"id": REVISION_UUID, "definition": definition},
                source="pinned",
                requested_revision_id=REVISION_UUID,
                failure_reason=None,
            )
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_session_workflow_state",
        AsyncMock(
            return_value={
                "uuid": SESSION_UUID,
                "state": 2,
                "runtime_variables": runtime,
                "last_card_uuid": None,
                "next_card_uuid": API_REF,
                "frozen_until": None,
            }
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_contact_runtime_context_for_session",
        AsyncMock(return_value=None),
    )

    async def _replace(*_args, **kwargs) -> None:
        persisted.append(kwargs)

    monkeypatch.setattr(workflow, "replace_session_workflow_state", _replace)
    monkeypatch.setattr(workflow, "persist_session_metrics", persist_metrics)
    return persisted, persist_metrics


@pytest.mark.asyncio
async def test_api_call_missing_runtime_url_uses_exception_branch(monkeypatch) -> None:
    runtime = _runtime()
    persisted, _persist_metrics = _configure_execution(
        monkeypatch,
        definition=_definition(with_exception_branch=True),
        runtime=runtime,
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.last_card_uuid == FINISH_REF
    assert result.next_card_uuid is None
    assert runtime["api_call_last_error"]["code"] == "api_call_missing_url"
    assert runtime["api_call_last_error"]["component_ref_id"] == API_REF
    assert "terminal_failure" not in runtime["workflow_v2"]
    assert persisted[-1]["state"] == 3
    assert persisted[-1]["ended_at"] is not None


@pytest.mark.asyncio
async def test_api_call_missing_runtime_url_terminalizes_without_exception_branch(monkeypatch) -> None:
    runtime = _runtime()
    persisted, persist_metrics = _configure_execution(
        monkeypatch,
        definition=_definition(with_exception_branch=False),
        runtime=runtime,
    )

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "api_call_missing_url"
    assert result.last_card_uuid == API_REF
    assert result.next_card_uuid is None
    assert runtime["workflow_v2"]["terminal_failure"]["code"] == "api_call_missing_url"
    assert persisted[-1]["state"] == 3
    assert persisted[-1]["ended_at"] is not None
    assert persisted[-1]["next_card_uuid"] is None
    metrics = persist_metrics.await_args.kwargs["metrics"]
    assert metrics[-2]["status"] == "error"
    assert metrics[-2]["stopped_reason"] == "api_call_missing_url"
