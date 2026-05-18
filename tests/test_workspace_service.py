from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.services.workspace_service as workspace_service


@pytest.mark.asyncio
async def test_ensure_workspace_ready_for_orch_migrate_accepts_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_fetch_active_workspace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return {
            "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            "provision_status": "completed",
            "provision_step": None,
        }

    monkeypatch.setattr(workspace_service, "fetch_active_workspace", _fake_fetch_active_workspace)

    row = await workspace_service.ensure_workspace_ready_for_orch_migrate(
        object(),  # type: ignore[arg-type]
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
    )
    assert row["provision_status"] == "completed"


@pytest.mark.asyncio
async def test_ensure_workspace_ready_for_orch_migrate_accepts_running_orch_migrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_fetch_active_workspace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return {
            "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            "provision_status": "running",
            "provision_step": "orch_migrate",
        }

    monkeypatch.setattr(workspace_service, "fetch_active_workspace", _fake_fetch_active_workspace)

    row = await workspace_service.ensure_workspace_ready_for_orch_migrate(
        object(),  # type: ignore[arg-type]
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
    )
    assert row["provision_status"] == "running"
    assert row["provision_step"] == "orch_migrate"


@pytest.mark.asyncio
async def test_ensure_workspace_ready_for_orch_migrate_rejects_non_eligible_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_fetch_active_workspace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return {
            "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            "provision_status": "running",
            "provision_step": "contacts_sync",
        }

    monkeypatch.setattr(workspace_service, "fetch_active_workspace", _fake_fetch_active_workspace)

    with pytest.raises(HTTPException) as exc:
        await workspace_service.ensure_workspace_ready_for_orch_migrate(
            object(),  # type: ignore[arg-type]
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_ensure_workspace_ready_for_orch_migrate_returns_404_when_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_fetch_active_workspace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(workspace_service, "fetch_active_workspace", _fake_fetch_active_workspace)

    with pytest.raises(HTTPException) as exc:
        await workspace_service.ensure_workspace_ready_for_orch_migrate(
            object(),  # type: ignore[arg-type]
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        )
    assert exc.value.status_code == 404
