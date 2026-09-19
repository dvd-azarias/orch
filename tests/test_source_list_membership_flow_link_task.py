from __future__ import annotations

import pytest

import app.tasks.workflow_tasks as tasks
from app.services.identidade_person_flow_link_service import (
    IdentidadePersonFlowLinkResult,
)


class _LockResult:
    def scalar_one(self) -> bool:
        return True


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement, _parameters=None):
        if "pg_try_advisory_xact_lock" in str(statement):
            return _LockResult()
        return object()


class _Settings:
    celery_execute_queue = "orch_execute_test"


@pytest.mark.asyncio
async def test_membership_flow_link_task_persists_completion_and_requeues_same_session(
    monkeypatch,
) -> None:
    runtime = {
        "workflow_v2": {
            "source_list_membership_flow_link": {
                "component_ref_id": "membership-1",
                "mailing_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "status": "pending",
            }
        }
    }
    persisted: list[dict] = []
    requeued: list[dict] = []

    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        tasks,
        "bind_workspace_context",
        lambda value: (value, "ws_test"),
    )
    monkeypatch.setattr(tasks, "get_session_factory", lambda: lambda: _Session())
    monkeypatch.setattr(
        tasks,
        "fetch_session_workflow_state",
        lambda *_args, **_kwargs: _async_value(
            {
                "flow_uuid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "runtime_variables": runtime,
                "last_card_uuid": "membership-1",
                "next_card_uuid": "membership-1",
            }
        ),
    )
    monkeypatch.setattr(
        tasks,
        "replace_session_workflow_state",
        lambda *_args, **kwargs: _async_append(persisted, kwargs),
    )
    monkeypatch.setattr(
        tasks,
        "link_source_list_membership_mailing_to_current_flow",
        lambda **_kwargs: _async_value(
            IdentidadePersonFlowLinkResult(True, 200, 1, "linked")
        ),
    )
    monkeypatch.setattr(
        tasks.advance_session_task,
        "apply_async",
        lambda **kwargs: requeued.append(kwargs),
    )

    await tasks._link_source_list_membership_mailing_task(
        workspace_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        flow_uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
        session_id=77,
    )

    state = runtime["workflow_v2"]["source_list_membership_flow_link"]
    assert state["status"] == "completed"
    assert state["attempts"] == 1
    assert persisted
    assert requeued == [
        {
            "kwargs": {
                "workspace_uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "flow_uuid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "session_id": 77,
            },
            "queue": "orch_execute_test",
            "routing_key": "orch_execute_test",
        }
    ]


@pytest.mark.asyncio
async def test_membership_flow_link_task_is_idempotent_after_completion(
    monkeypatch,
) -> None:
    runtime = {
        "workflow_v2": {
            "source_list_membership_flow_link": {
                "component_ref_id": "membership-1",
                "mailing_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "status": "completed",
            }
        }
    }
    calls: list[dict] = []

    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        tasks,
        "bind_workspace_context",
        lambda value: (value, "ws_test"),
    )
    monkeypatch.setattr(tasks, "get_session_factory", lambda: lambda: _Session())
    monkeypatch.setattr(
        tasks,
        "fetch_session_workflow_state",
        lambda *_args, **_kwargs: _async_value(
            {
                "flow_uuid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "runtime_variables": runtime,
            }
        ),
    )
    monkeypatch.setattr(
        tasks,
        "link_source_list_membership_mailing_to_current_flow",
        lambda **kwargs: _async_append(calls, kwargs),
    )

    await tasks._link_source_list_membership_mailing_task(
        workspace_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        flow_uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
        session_id=77,
    )

    assert calls == []


@pytest.mark.asyncio
async def test_membership_flow_link_task_persists_failure_and_requeues_exception_path(
    monkeypatch,
) -> None:
    runtime = {
        "workflow_v2": {
            "source_list_membership_flow_link": {
                "component_ref_id": "membership-1",
                "mailing_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "status": "pending",
            }
        }
    }
    persisted: list[dict] = []
    requeued: list[dict] = []

    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        tasks,
        "bind_workspace_context",
        lambda value: (value, "ws_test"),
    )
    monkeypatch.setattr(tasks, "get_session_factory", lambda: lambda: _Session())
    monkeypatch.setattr(
        tasks,
        "fetch_session_workflow_state",
        lambda *_args, **_kwargs: _async_value(
            {
                "flow_uuid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "runtime_variables": runtime,
                "last_card_uuid": "membership-1",
                "next_card_uuid": "membership-1",
            }
        ),
    )
    monkeypatch.setattr(
        tasks,
        "replace_session_workflow_state",
        lambda *_args, **kwargs: _async_append(persisted, kwargs),
    )
    monkeypatch.setattr(
        tasks,
        "link_source_list_membership_mailing_to_current_flow",
        lambda **_kwargs: _async_value(
            IdentidadePersonFlowLinkResult(
                False,
                422,
                1,
                "target_core_http_error",
                "Target Core recusou o vínculo.",
            )
        ),
    )
    monkeypatch.setattr(
        tasks.advance_session_task,
        "apply_async",
        lambda **kwargs: requeued.append(kwargs),
    )

    await tasks._link_source_list_membership_mailing_task(
        workspace_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        flow_uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
        session_id=77,
    )

    state = runtime["workflow_v2"]["source_list_membership_flow_link"]
    assert state["status"] == "failed"
    assert state["status_code"] == 422
    assert state["last_error"]["code"] == (
        "source_list_membership_flow_link_target_core_http_error"
    )
    assert persisted
    assert len(requeued) == 1


async def _async_value(value):
    return value


async def _async_append(target: list, value):
    target.append(value)
