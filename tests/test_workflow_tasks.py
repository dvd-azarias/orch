from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.tasks import workflow_tasks


class _DummySession:
    def __init__(self) -> None:
        self.commits = 0

    def in_transaction(self) -> bool:
        return True

    async def commit(self) -> None:
        self.commits += 1


class _DummySessionContext:
    def __init__(self) -> None:
        self.session = _DummySession()

    async def __aenter__(self) -> _DummySession:
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


@pytest.mark.asyncio
async def test_advance_session_commits_terminal_branch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    session_context = _DummySessionContext()
    monkeypatch.setattr(workflow_tasks, "get_settings", lambda: SimpleNamespace(celery_enabled=True))
    monkeypatch.setattr(workflow_tasks, "get_session_factory", lambda: (lambda: session_context))
    monkeypatch.setattr(workflow_tasks, "bind_workspace_context", lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"))

    async def _advance(*_args, **_kwargs) -> str:
        return "condition_branch_not_mapped"

    alarms: list[dict] = []
    metrics: list[list[dict]] = []

    async def _persist_alarm(*_args, **kwargs) -> None:
        alarms.append(kwargs)

    async def _persist_metrics(*_args, **kwargs) -> None:
        metrics.append(kwargs["metrics"])

    monkeypatch.setattr(workflow_tasks, "advance_session_once", _advance)
    monkeypatch.setattr(workflow_tasks, "persist_alarm", _persist_alarm)
    monkeypatch.setattr(workflow_tasks, "persist_session_metrics", _persist_metrics)

    await workflow_tasks._advance_session_task(
        workspace_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        flow_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        session_id=123,
    )

    assert session_context.session.commits == 1
    assert len(alarms) == 1
    assert alarms[0]["code"] == "workflow_m2_condition_branch_not_mapped"
    assert metrics[0][0]["status"] == "error"
    assert metrics[0][0]["stopped_reason"] == "condition_branch_not_mapped"
