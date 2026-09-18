from __future__ import annotations

import pytest

import app.services.workflow_m2_service as workflow_m2_service
from app.services.workflow_m2_service import execute_workflow_m2_for_session
from app.services.workflow_revision_service import WorkflowRevisionResolution


@pytest.mark.asyncio
async def test_missing_component_terminalizes_old_revision_with_diagnostic_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_uuid = "33333333-3333-3333-3333-333333333333"
    revision_id = "44444444-4444-4444-4444-444444444444"
    missing_ref = "22222222-2222-2222-2222-222222222222"
    runtime_variables = {
        "workflow_v2": {
            "flow_id": flow_uuid,
            "revision_id": revision_id,
            "next_card_cursor": missing_ref,
        },
        "variables": {"payload": {}, "customs": {}},
    }
    persisted: list[dict] = []

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class _LockResult:
        def scalar_one(self) -> bool:
            return True

    class _Session:
        def in_transaction(self) -> bool:
            return False

        def begin(self) -> _Transaction:
            return _Transaction()

        async def execute(self, *_args, **_kwargs) -> _LockResult:
            return _LockResult()

    async def _fetch_flow(*_args, **_kwargs) -> dict:
        return {"id": flow_uuid}

    async def _fetch_revision(*_args, **_kwargs) -> WorkflowRevisionResolution:
        return WorkflowRevisionResolution(
            revision={
                "id": revision_id,
                "definition": {
                    "components": [
                        {
                            "ref_id": "11111111-1111-1111-1111-111111111111",
                            "component_id": "finish_flow",
                            "parameters": {},
                        }
                    ],
                    "branches": [],
                },
            },
            source="pinned",
            requested_revision_id=revision_id,
            failure_reason=None,
        )

    async def _fetch_session(*_args, **_kwargs) -> dict:
        return {
            "uuid": "55555555-5555-5555-5555-555555555555",
            "state": 0,
            "runtime_variables": runtime_variables,
            "last_card_uuid": None,
            "next_card_uuid": missing_ref,
        }

    async def _fetch_contact(*_args, **_kwargs):
        return None

    async def _replace(*_args, **kwargs) -> None:
        persisted.append(kwargs)

    async def _persist_metrics(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(
        workflow_m2_service,
        "_read_contextual_member_routing_enabled",
        lambda _settings: False,
    )
    monkeypatch.setattr(workflow_m2_service, "fetch_flow_row", _fetch_flow)
    monkeypatch.setattr(
        workflow_m2_service,
        "resolve_workflow_revision_for_session",
        _fetch_revision,
    )
    monkeypatch.setattr(
        workflow_m2_service,
        "fetch_session_workflow_state",
        _fetch_session,
    )
    monkeypatch.setattr(
        workflow_m2_service,
        "fetch_contact_runtime_context_for_session",
        _fetch_contact,
    )
    monkeypatch.setattr(
        workflow_m2_service,
        "replace_session_workflow_state",
        _replace,
    )
    monkeypatch.setattr(
        workflow_m2_service,
        "persist_session_metrics",
        _persist_metrics,
    )

    result = await execute_workflow_m2_for_session(
        _Session(),
        flow_uuid=flow_uuid,
        session_id=8231,
    )

    assert result.stopped_reason == "component_not_found"
    assert result.next_card_uuid is None
    assert persisted[-1]["state"] == 3
    assert persisted[-1]["ended_at"] is not None
    assert persisted[-1]["next_card_uuid"] is None
    terminal_failure = runtime_variables["workflow_v2"]["terminal_failure"]
    assert terminal_failure["code"] == "component_not_found"
    assert terminal_failure["missing_component_ref_id"] == missing_ref
