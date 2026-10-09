from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.tasks import metrics_event_tasks as tasks


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
