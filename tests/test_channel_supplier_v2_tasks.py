from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.tasks import channel_supplier_v2_tasks as tasks


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
FLOW_UUID = "c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152"


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "channel_supplier_v2_enabled": True,
        "channel_supplier_v2_workspace_allowlist": (WORKSPACE_UUID,),
        "channel_supplier_v2_flow_allowlist": (FLOW_UUID,),
        "channel_supplier_v2_registration_lease_seconds": 120,
        "channel_supplier_v2_reconcile_batch_size": 100,
        "celery_channel_supplier_v2_queue": "orch_channel_supplier_v2_test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Transaction:
    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_args):  # noqa: ANN204, ANN002
        return None


class _Mappings:
    def all(self) -> list[dict]:
        return [{"id": 71, "flow_uuid": FLOW_UUID, "attempts": 2}]


class _Rows:
    def mappings(self) -> _Mappings:
        return _Mappings()


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

    async def execute(self, statement, _parameters=None):  # noqa: ANN001, ANN201
        if 'FROM "orch_sessions"' in str(statement):
            return _Rows()
        return object()


@pytest.mark.asyncio
async def test_reconciler_recovers_only_allowlisted_workspace_on_dedicated_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    enqueued: list[dict] = []
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "get_session_factory", lambda: (lambda: _Session()))
    monkeypatch.setattr(
        tasks,
        "list_completed_workspaces",
        lambda _session: _async_value(
            [
                {"workspace_uuid": WORKSPACE_UUID},
                {"workspace_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
            ]
        ),
    )
    monkeypatch.setattr(
        tasks,
        "bind_workspace_context",
        lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"),
    )
    monkeypatch.setattr(
        tasks.register_channel_supplier_v2_dispatch_task,
        "apply_async",
        lambda **kwargs: enqueued.append(kwargs),
    )

    result = await tasks._reconcile_pending_channel_supplier_v2_dispatches_task()

    assert result == {"scanned": 1, "enqueued": 1}
    assert enqueued[0]["queue"] == "orch_channel_supplier_v2_test"
    assert enqueued[0]["kwargs"] == {
        "workspace_uuid": WORKSPACE_UUID,
        "flow_uuid": FLOW_UUID,
        "session_id": 71,
        "recovery_attempt": 3,
    }


@pytest.mark.asyncio
async def test_reconciler_is_inert_when_feature_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(channel_supplier_v2_enabled=False)
    )

    assert await tasks._reconcile_pending_channel_supplier_v2_dispatches_task() == {
        "scanned": 0,
        "enqueued": 0,
    }


async def _async_value(value):  # type: ignore[no-untyped-def]
    return value
