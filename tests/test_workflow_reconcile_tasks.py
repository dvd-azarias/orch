from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.tasks import workflow_tasks


class _DummySession:
    pass


class _DummySessionCtx:
    async def __aenter__(self) -> _DummySession:
        return _DummySession()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def _dummy_session_factory():
    return _DummySessionCtx()


@pytest.mark.asyncio
async def test_reconcile_pending_events_enqueues_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        celery_enabled=True,
        celery_beat_reconcile_pending_events_enabled=True,
        celery_result_backend=None,
        celery_reconcile_pending_events_workspace_uuid=None,
        celery_reconcile_pending_events_stale_seconds=30,
        celery_reconcile_pending_events_batch_size=200,
        celery_reconcile_pending_events_cooldown_seconds=30,
    )
    monkeypatch.setattr(workflow_tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(workflow_tasks, "get_session_factory", lambda: _dummy_session_factory)
    async def _list_workspaces(_db):
        return [{"workspace_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}]

    monkeypatch.setattr(workflow_tasks, "list_completed_workspaces", _list_workspaces)
    monkeypatch.setattr(workflow_tasks, "bind_workspace_context", lambda _ws: (_ws, f"ws_{_ws}"))
    async def _list_stale_sessions(_db, *, stale_seconds, limit):
        return [
            {"session_id": 101, "flow_uuid": "flow-1"},
            {"session_id": 102, "flow_uuid": "flow-2"},
        ]

    monkeypatch.setattr(workflow_tasks, "list_stale_pending_channel_event_sessions", _list_stale_sessions)
    monkeypatch.setattr(workflow_tasks, "_try_acquire_reconcile_lock", lambda *_args, **_kwargs: True)

    enqueued: list[dict] = []
    monkeypatch.setattr(workflow_tasks.advance_session_task, "delay", lambda **kwargs: enqueued.append(kwargs))

    result = await workflow_tasks._reconcile_pending_channel_events_task()

    assert result == 2
    assert enqueued == [
        {
            "workspace_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "flow_uuid": "flow-1",
            "session_id": 101,
        },
        {
            "workspace_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "flow_uuid": "flow-2",
            "session_id": 102,
        },
    ]


@pytest.mark.asyncio
async def test_reconcile_pending_events_respects_workspace_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    scoped_workspace = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    settings = SimpleNamespace(
        celery_enabled=True,
        celery_beat_reconcile_pending_events_enabled=True,
        celery_result_backend=None,
        celery_reconcile_pending_events_workspace_uuid=scoped_workspace,
        celery_reconcile_pending_events_stale_seconds=30,
        celery_reconcile_pending_events_batch_size=200,
        celery_reconcile_pending_events_cooldown_seconds=30,
    )
    monkeypatch.setattr(workflow_tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(workflow_tasks, "get_session_factory", lambda: _dummy_session_factory)
    async def _list_workspaces(_db):
        return [
            {"workspace_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            {"workspace_uuid": scoped_workspace},
        ]

    monkeypatch.setattr(workflow_tasks, "list_completed_workspaces", _list_workspaces)
    monkeypatch.setattr(workflow_tasks, "bind_workspace_context", lambda _ws: (_ws, f"ws_{_ws}"))
    async def _list_stale_sessions(_db, *, stale_seconds, limit):
        return [{"session_id": 201, "flow_uuid": "flow-scoped"}]

    monkeypatch.setattr(workflow_tasks, "list_stale_pending_channel_event_sessions", _list_stale_sessions)
    monkeypatch.setattr(workflow_tasks, "_try_acquire_reconcile_lock", lambda *_args, **_kwargs: True)

    enqueued: list[dict] = []
    monkeypatch.setattr(workflow_tasks.advance_session_task, "delay", lambda **kwargs: enqueued.append(kwargs))

    result = await workflow_tasks._reconcile_pending_channel_events_task()

    assert result == 1
    assert enqueued == [
        {
            "workspace_uuid": scoped_workspace,
            "flow_uuid": "flow-scoped",
            "session_id": 201,
        }
    ]
