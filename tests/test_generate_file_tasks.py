from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.core.database as database
import app.tasks.generate_file_tasks as tasks


class _SessionContext:
    def __init__(self) -> None:
        self.session = AsyncMock()

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self.session

    async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
        return False


@pytest.mark.asyncio
async def test_scan_fans_out_only_immediate_jobs(monkeypatch) -> None:
    class _Settings:
        celery_enabled = True
        celery_generate_file_enabled = True
        celery_generate_file_workspace_uuid = None
        celery_generate_file_scan_batch_size = 200

    enqueue_calls: list[dict[str, object]] = []

    async def _list_workspaces(_db_session):  # type: ignore[no-untyped-def]
        return [{"workspace_uuid": "253148c7-a85f-42a3-bc8b-5ffd9d885efe"}]

    async def _list_due_jobs(_db_session, **_kwargs):  # type: ignore[no-untyped-def]
        return [
            {"job_id": "immediate-job", "mode": "imediato"},
            {"job_id": "scheduled-job", "mode": "agendado"},
        ]

    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(tasks, "list_completed_workspaces", _list_workspaces)
    monkeypatch.setattr(tasks, "list_due_jobs", _list_due_jobs)
    monkeypatch.setattr(database, "get_session_factory", lambda: _SessionContext)
    monkeypatch.setattr(
        tasks.generate_file_run_task,
        "delay",
        lambda **kwargs: enqueue_calls.append(kwargs),
    )

    scanned, jobs_enqueued, tasks_enqueued = await tasks._scan_due_generate_file_jobs_task()

    assert scanned == 1
    assert jobs_enqueued == 2
    assert tasks_enqueued == 11
    immediate_calls = [call for call in enqueue_calls if call["job_id"] == "immediate-job"]
    scheduled_calls = [call for call in enqueue_calls if call["job_id"] == "scheduled-job"]
    assert len(immediate_calls) == 10
    assert all(call["drain_immediate"] is True for call in immediate_calls)
    assert scheduled_calls == [
        {
            "workspace_uuid": "253148c7-a85f-42a3-bc8b-5ffd9d885efe",
            "job_id": "scheduled-job",
            "drain_immediate": False,
        }
    ]


@pytest.mark.asyncio
async def test_immediate_drain_enqueues_one_replacement_after_processed_row(monkeypatch) -> None:
    class _Settings:
        celery_enabled = True
        celery_generate_file_enabled = True
        celery_generate_file_workspace_uuid = None
        celery_generate_file_run_queue = "orch_component_generate_file_run"

    class _Continuation:
        id = "continuation-1"

    process_job = AsyncMock(return_value={"status": "success", "rows_selected": 1, "rows_sent": 1})
    apply_async = MagicMock(return_value=_Continuation())

    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(tasks, "process_generate_file_job", process_job)
    monkeypatch.setattr(database, "get_session_factory", lambda: _SessionContext)
    monkeypatch.setattr(tasks.generate_file_run_task, "apply_async", apply_async)

    result = await tasks._generate_file_run_task(
        workspace_uuid="253148c7-a85f-42a3-bc8b-5ffd9d885efe",
        job_id="79850bc2-ba05-4376-86e0-465ddd6f1b14",
        drain_immediate=True,
    )

    assert result["drain_continuation_task_id"] == "continuation-1"
    apply_async.assert_called_once_with(
        kwargs={
            "workspace_uuid": "253148c7-a85f-42a3-bc8b-5ffd9d885efe",
            "job_id": "79850bc2-ba05-4376-86e0-465ddd6f1b14",
            "drain_immediate": True,
        },
        queue="orch_component_generate_file_run",
        routing_key="orch_component_generate_file_run",
    )
