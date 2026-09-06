from __future__ import annotations

import pytest

import app.tasks.workflow_tasks as tasks
from app.services.identidade_person_flow_link_service import IdentidadePersonFlowLinkResult


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
        return _LockResult() if "pg_try_advisory_xact_lock" in str(statement) else object()


class _Settings:
    celery_execute_queue = "orch_execute_test"


@pytest.mark.asyncio
async def test_flow_link_task_persists_completion_and_requeues_executor(monkeypatch) -> None:
    runtime = {
        "workflow_v2": {
            "identidade_person_flow_link": {
                "component_ref_id": "identidade-1",
                "mailing_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "status": "pending",
            }
        }
    }
    persisted: list[dict] = []
    requeued: list[dict] = []

    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(tasks, "bind_workspace_context", lambda value: (value, "ws_test"))
    monkeypatch.setattr(tasks, "get_session_factory", lambda: lambda: _Session())
    monkeypatch.setattr(
        tasks,
        "fetch_session_workflow_state",
        lambda *_args, **_kwargs: _async_value(
            {
                "flow_uuid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "runtime_variables": runtime,
                "last_card_uuid": "identidade-1",
                "next_card_uuid": "identidade-1",
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
        "link_identidade_mailing_to_current_flow",
        lambda **_kwargs: _async_value(IdentidadePersonFlowLinkResult(True, 200, 1, "linked")),
    )
    monkeypatch.setattr(tasks.advance_session_task, "apply_async", lambda **kwargs: requeued.append(kwargs))

    await tasks._link_identidade_person_mailing_task(
        workspace_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        flow_uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
        session_id=77,
    )

    state = runtime["workflow_v2"]["identidade_person_flow_link"]
    assert state["status"] == "completed"
    assert state["attempts"] == 1
    assert persisted
    assert requeued[0]["queue"] == "orch_execute_test"


async def _async_value(value):
    return value


async def _async_append(target: list, value):
    target.append(value)
