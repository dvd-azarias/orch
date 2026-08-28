from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.tasks.billing_batch_tasks as tasks
from app.core.billing_celery_app import billing_celery_app


def test_billing_worker_has_dedicated_reliable_task_settings() -> None:
    assert billing_celery_app.conf.task_acks_late is True
    assert billing_celery_app.conf.task_reject_on_worker_lost is True
    assert billing_celery_app.conf.worker_prefetch_multiplier == 1
    assert billing_celery_app.conf.worker_max_tasks_per_child == 1000
    assert billing_celery_app.conf.task_soft_time_limit == 240
    assert billing_celery_app.conf.task_time_limit == 300


@pytest.mark.asyncio
async def test_reprocess_task_ignores_inactive_workspace(monkeypatch) -> None:
    class _Session:
        def in_transaction(self) -> bool:
            return True

        async def commit(self) -> None:
            return None

    class _Factory:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(orch_billing_enabled=True))
    monkeypatch.setattr(tasks, "get_session_factory", lambda: lambda: _Factory())
    monkeypatch.setattr(tasks, "fetch_active_workspace", AsyncMock(return_value=None))
    process = AsyncMock()
    monkeypatch.setattr(tasks, "process_billing_reprocess_request", process)

    result = await tasks._billing_reprocess_task(
        workspace_uuid="11111111-1111-1111-1111-111111111111",
        request_id="33333333-3333-3333-3333-333333333333",
    )
    assert result is None
    process.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_batch_tasks_are_noops_when_feature_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(orch_billing_enabled=False))
    assert await tasks._aggregate_billing_task() == {"snapshots_created": 0, "events_batched": 0}
    assert await tasks._publish_due_billing_task() == {"sent": 0, "failed": 0, "blocked": 0, "stale_claims": 0}
    assert await tasks._reconcile_billing_task() == {"events_created": 0}
    assert await tasks._scan_billing_reprocess_task() == {"enqueued": 0, "enqueue_failed": 0}


@pytest.mark.asyncio
async def test_publish_task_blocks_non_object_persisted_payload(monkeypatch) -> None:
    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class _Session:
        def begin(self):
            return _Transaction()

    class _Factory:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return False

    settings = SimpleNamespace(
        orch_billing_enabled=True,
        billing_publish_claim_batch_size=20,
        billing_processing_lease_seconds=120,
    )
    snapshot = {
        "snapshot_id": "orch_usage_202608_111_bad",
        "claim_token": "22222222-2222-2222-2222-222222222222",
        "payload": ["invalid"],
        "quantity": 1,
        "billing_period": "2026-08",
        "attempt_count": 1,
    }
    blocked = AsyncMock(return_value=True)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "get_session_factory", lambda: lambda: _Factory())
    monkeypatch.setattr(tasks, "_completed_workspace_uuids", AsyncMock(return_value=["11111111-1111-1111-1111-111111111111"]))
    monkeypatch.setattr(tasks, "_set_workspace_search_path", AsyncMock())
    monkeypatch.setattr(tasks, "claim_due_billing_snapshots", AsyncMock(return_value=[snapshot]))
    monkeypatch.setattr(tasks, "mark_billing_snapshot_blocked", blocked)

    result = await tasks._publish_due_billing_task()

    assert result == {"sent": 0, "failed": 0, "blocked": 1, "stale_claims": 0}
    blocked.assert_awaited_once()


@pytest.mark.asyncio
async def test_aggregate_isolates_workspace_failure_and_publishes_flush_immediately(monkeypatch) -> None:
    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class _Session:
        def begin(self):
            return _Transaction()

    class _Factory:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return False

    aggregate = AsyncMock(
        side_effect=[RuntimeError("workspace drift"), SimpleNamespace(quantity=30), None]
    )
    immediate_publish = AsyncMock(return_value={"sent": 1})
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(orch_billing_enabled=True),
    )
    monkeypatch.setattr(tasks, "get_session_factory", lambda: lambda: _Factory())
    monkeypatch.setattr(
        tasks,
        "_completed_workspace_uuids",
        AsyncMock(return_value=[
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ]),
    )
    monkeypatch.setattr(tasks, "_set_workspace_search_path", AsyncMock())
    monkeypatch.setattr(tasks, "aggregate_next_billing_snapshot", aggregate)
    monkeypatch.setattr(tasks, "_publish_due_billing_task", immediate_publish)

    result = await tasks._aggregate_billing_task()

    assert result == {"snapshots_created": 1, "events_batched": 30}
    assert aggregate.await_count == 3
    immediate_publish.assert_awaited_once()
