from __future__ import annotations

from types import SimpleNamespace

import pytest
from celery.exceptions import Retry

from app.tasks import workflow_tasks


class _DummySession:
    def __init__(self, events: list[str] | None = None) -> None:
        self.commits = 0
        self.events = events

    def in_transaction(self) -> bool:
        return True

    async def commit(self) -> None:
        self.commits += 1
        if self.events is not None:
            self.events.append("commit")


class _DummySessionContext:
    def __init__(self, events: list[str] | None = None) -> None:
        self.session = _DummySession(events)

    async def __aenter__(self) -> _DummySession:
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stopped_reason", "alarm_code"),
    [
        ("condition_branch_not_mapped", "workflow_m2_condition_branch_not_mapped"),
        ("api_call_missing_url", "workflow_m2_api_call_missing_url"),
        ("contact_member_scope_not_found", "workflow_m2_contact_member_scope_not_found"),
        ("contact_member_routing_update_failed", "workflow_m2_contact_member_routing_update_failed"),
        (
            "identidade_person_invalid_document",
            "workflow_m2_identidade_person_invalid_document",
        ),
    ],
)
async def test_advance_session_commits_terminal_failure_alarm(
    monkeypatch: pytest.MonkeyPatch,
    stopped_reason: str,
    alarm_code: str,
) -> None:
    session_context = _DummySessionContext()
    monkeypatch.setattr(workflow_tasks, "get_settings", lambda: SimpleNamespace(celery_enabled=True))
    monkeypatch.setattr(workflow_tasks, "get_session_factory", lambda: (lambda: session_context))
    monkeypatch.setattr(workflow_tasks, "bind_workspace_context", lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"))

    async def _advance(*_args, **_kwargs) -> str:
        return stopped_reason

    alarms: list[dict] = []
    metrics: list[list[dict]] = []

    async def _persist_alarm(*_args, **kwargs) -> None:
        alarms.append(kwargs)

    async def _persist_metrics(*_args, **kwargs) -> None:
        metrics.append(kwargs["metrics"])

    monkeypatch.setattr(workflow_tasks, "advance_session_once", _advance)
    monkeypatch.setattr(workflow_tasks, "persist_alarm", _persist_alarm)
    monkeypatch.setattr(workflow_tasks, "persist_session_metrics", _persist_metrics)

    result = await workflow_tasks._advance_session_task(
        workspace_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        flow_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        session_id=123,
    )

    assert result == stopped_reason
    assert session_context.session.commits == 1
    assert len(alarms) == 1
    assert alarms[0]["code"] == alarm_code
    assert metrics[0][0]["status"] == "error"
    assert metrics[0][0]["stopped_reason"] == stopped_reason


def test_supplier_v2_terminal_resume_retries_session_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _advance(**_kwargs) -> str:
        return "session_execution_locked"

    retry_calls: list[dict] = []

    def _retry(**kwargs):
        retry_calls.append(kwargs)
        raise Retry()

    task = workflow_tasks.resume_dialer_supplier_v2_terminal_task
    monkeypatch.setattr(workflow_tasks, "_advance_session_task", _advance)
    monkeypatch.setattr(task, "retry", _retry)

    with pytest.raises(Retry):
        task.run(
            workspace_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            flow_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            session_id=123,
        )

    assert len(retry_calls) == 1
    assert retry_calls[0]["countdown"] == 1
    assert str(retry_calls[0]["exc"]) == (
        "dialer_supplier_v2_terminal_session_locked"
    )


def test_supplier_v2_terminal_resume_does_not_retry_after_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _advance(**_kwargs) -> str:
        return "finished_by_component"

    task = workflow_tasks.resume_dialer_supplier_v2_terminal_task
    monkeypatch.setattr(workflow_tasks, "_advance_session_task", _advance)

    result = task.run(
        workspace_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        flow_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        session_id=123,
    )

    assert result == {
        "status": "completed",
        "stopped_reason": "finished_by_component",
    }


@pytest.mark.asyncio
async def test_advance_session_enqueues_identidade_link_only_after_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    session_context = _DummySessionContext(events)
    monkeypatch.setattr(
        workflow_tasks,
        "get_settings",
        lambda: SimpleNamespace(celery_enabled=True, celery_execute_queue="orch_execute_test"),
    )
    monkeypatch.setattr(workflow_tasks, "get_session_factory", lambda: (lambda: session_context))
    monkeypatch.setattr(
        workflow_tasks,
        "bind_workspace_context",
        lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"),
    )

    async def _advance(*_args, **_kwargs) -> str:
        return "blocked_identidade_person_flow_link"

    async def _persist_metrics(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(workflow_tasks, "advance_session_once", _advance)
    monkeypatch.setattr(workflow_tasks, "persist_session_metrics", _persist_metrics)
    monkeypatch.setattr(
        workflow_tasks.link_identidade_person_mailing_task,
        "apply_async",
        lambda **_kwargs: events.append("enqueue"),
    )

    await workflow_tasks._advance_session_task(
        workspace_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        flow_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        session_id=123,
    )

    assert events == ["commit", "enqueue"]


@pytest.mark.asyncio
async def test_advance_session_enqueues_supplier_v2_only_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_uuid = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
    flow_uuid = "4e163399-e9a0-4335-895f-316c6a161299"
    queue = "orch_dialer_supplier_v2_test"
    events: list[str] = []
    enqueued: list[dict] = []
    session_context = _DummySessionContext(events)
    monkeypatch.setattr(
        workflow_tasks,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=True,
            dialer_supplier_v2_enabled=True,
            dialer_supplier_v2_workspace_allowlist=(workspace_uuid,),
            dialer_supplier_v2_flow_allowlist=(flow_uuid,),
            celery_dialer_supplier_v2_queue=queue,
        ),
    )
    monkeypatch.setattr(
        workflow_tasks,
        "get_session_factory",
        lambda: (lambda: session_context),
    )
    monkeypatch.setattr(
        workflow_tasks,
        "bind_workspace_context",
        lambda value: (value, f"ws_{value}"),
    )

    async def _advance(*_args, **_kwargs) -> str:
        return "blocked_send_with_dialer_handoff"

    async def _persist_metrics(*_args, **_kwargs) -> None:
        return None

    def _enqueue(**kwargs) -> None:  # type: ignore[no-untyped-def]
        events.append("enqueue")
        enqueued.append(kwargs)

    monkeypatch.setattr(workflow_tasks, "advance_session_once", _advance)
    monkeypatch.setattr(
        workflow_tasks,
        "persist_session_metrics",
        _persist_metrics,
    )
    from app.tasks import dialer_supplier_v2_tasks

    monkeypatch.setattr(
        dialer_supplier_v2_tasks.register_dialer_supplier_v2_cycle_task,
        "apply_async",
        _enqueue,
    )

    await workflow_tasks._advance_session_task(
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
        session_id=123,
    )

    assert events == ["commit", "enqueue"]
    assert enqueued == [
        {
            "kwargs": {
                "workspace_uuid": workspace_uuid,
                "flow_uuid": flow_uuid,
                "session_id": 123,
            },
            "queue": queue,
            "routing_key": queue,
        }
    ]


@pytest.mark.asyncio
async def test_advance_session_does_not_enqueue_supplier_v2_for_unlisted_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    workspace_uuid = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
    session_context = _DummySessionContext(events)
    monkeypatch.setattr(
        workflow_tasks,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=True,
            dialer_supplier_v2_enabled=True,
            dialer_supplier_v2_workspace_allowlist=(workspace_uuid,),
            dialer_supplier_v2_flow_allowlist=(
                "4e163399-e9a0-4335-895f-316c6a161299",
            ),
            celery_dialer_supplier_v2_queue="orch_dialer_supplier_v2_test",
        ),
    )
    monkeypatch.setattr(
        workflow_tasks,
        "get_session_factory",
        lambda: (lambda: session_context),
    )
    monkeypatch.setattr(
        workflow_tasks,
        "bind_workspace_context",
        lambda value: (value, f"ws_{value}"),
    )

    async def _advance(*_args, **_kwargs) -> str:
        return "blocked_send_with_dialer_handoff"

    async def _persist_metrics(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(workflow_tasks, "advance_session_once", _advance)
    monkeypatch.setattr(
        workflow_tasks,
        "persist_session_metrics",
        _persist_metrics,
    )

    await workflow_tasks._advance_session_task(
        workspace_uuid=workspace_uuid,
        flow_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        session_id=123,
    )

    assert events == ["commit"]
