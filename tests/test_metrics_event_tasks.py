from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.tasks import metrics_event_tasks as tasks
from app.services.metrics_event_outbox_service import MetricsPublishResult


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
SECOND_WORKSPACE_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class _Transaction:
    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_args):  # noqa: ANN204, ANN002
        return None


class _Session:
    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_args):  # noqa: ANN204, ANN002
        return None

    def begin(self) -> _Transaction:
        return _Transaction()

    def in_transaction(self) -> bool:
        return False

    async def commit(self) -> None:
        return None

    async def execute(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
        return object()


async def _async_value(value):  # type: ignore[no-untyped-def]
    return value


@pytest.mark.asyncio
async def test_global_publisher_scans_every_completed_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        orch_metrics_events_enabled=True,
        orch_metrics_events_allow_all_workspaces=True,
        orch_metrics_events_workspace_allowlist=(),
        orch_metrics_events_batch_size=100,
        orch_metrics_events_lease_seconds=120,
        orch_metrics_events_max_attempts=12,
    )
    claimed_workspaces: list[str] = []
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "get_session_factory", lambda: (lambda: _Session()))
    monkeypatch.setattr(
        tasks,
        "list_completed_workspaces",
        lambda _session: _async_value(
            [
                {"workspace_uuid": WORKSPACE_UUID},
                {"workspace_uuid": SECOND_WORKSPACE_UUID},
            ]
        ),
    )
    monkeypatch.setattr(
        tasks,
        "bind_workspace_context",
        lambda workspace_uuid: (
            claimed_workspaces.append(workspace_uuid) or workspace_uuid,
            f"ws_{workspace_uuid}",
        ),
    )
    monkeypatch.setattr(
        tasks,
        "claim_metrics_events",
        lambda *_args, **_kwargs: _async_value([]),
    )

    result = await tasks._publish_pending_metrics_events()

    assert result["workspaces_scanned"] == 2
    assert claimed_workspaces == [WORKSPACE_UUID, SECOND_WORKSPACE_UUID]


@pytest.mark.asyncio
async def test_custom_metrics_use_dedicated_singular_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        orch_metrics_events_enabled=True,
        orch_metrics_events_allow_all_workspaces=True,
        orch_metrics_events_workspace_allowlist=(),
        orch_metrics_events_batch_size=100,
        orch_metrics_events_lease_seconds=120,
        orch_metrics_events_max_attempts=12,
        orch_metrics_events_retry_initial_seconds=5,
        orch_metrics_events_retry_max_seconds=900,
    )
    regular = SimpleNamespace(
        id=1,
        event_type="flow.dispatch.updated.v1",
        envelope={"event_type": "flow.dispatch.updated.v1"},
        attempts=1,
    )
    custom = SimpleNamespace(
        id=2,
        event_type="metric.custom.emitted.v1",
        envelope={"event_type": "metric.custom.emitted.v1"},
        attempts=1,
    )
    batch_calls: list[list[dict[str, str]]] = []
    custom_calls: list[dict[str, str]] = []
    published_ids: list[list[int]] = []

    async def _to_thread(function, **kwargs):  # type: ignore[no-untyped-def]
        return function(**kwargs)

    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "get_session_factory", lambda: (lambda: _Session()))
    monkeypatch.setattr(
        tasks,
        "list_completed_workspaces",
        lambda _session: _async_value([{"workspace_uuid": WORKSPACE_UUID}]),
    )
    monkeypatch.setattr(
        tasks,
        "bind_workspace_context",
        lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"),
    )
    monkeypatch.setattr(
        tasks,
        "claim_metrics_events",
        lambda *_args, **_kwargs: _async_value([regular, custom]),
    )
    monkeypatch.setattr(tasks.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(
        tasks,
        "publish_metrics_events_batch",
        lambda *, events, **_kwargs: (
            batch_calls.append(events)
            or MetricsPublishResult(202, "accepted", False, True)
        ),
    )
    monkeypatch.setattr(
        tasks,
        "publish_custom_metric_event",
        lambda *, event, **_kwargs: (
            custom_calls.append(event)
            or MetricsPublishResult(202, "accepted", False, True)
        ),
    )
    monkeypatch.setattr(
        tasks,
        "mark_metrics_events_published",
        lambda _session, *, item_ids, **_kwargs: (
            published_ids.append(item_ids) or _async_value(len(item_ids))
        ),
    )

    result = await tasks._publish_pending_metrics_events()

    assert batch_calls == [[regular.envelope]]
    assert custom_calls == [custom.envelope]
    assert published_ids == [[regular.id], [custom.id]]
    assert result["claimed"] == 2
    assert result["published"] == 2
