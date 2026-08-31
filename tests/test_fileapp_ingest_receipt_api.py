from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import orch as orch_api


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
FLOW_UUID = "706c6fef-85f2-4276-bcfd-eb28f75acde2"
FILE_UUID = "8ea2b45d-85e6-442c-b365-9355ae2cc2b8"


class _DbSession:
    def __init__(self) -> None:
        self.execute = AsyncMock()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    def in_transaction(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_tipo1_replay_does_not_enqueue_again(monkeypatch: pytest.MonkeyPatch) -> None:
    db_session = _DbSession()
    enqueue = SimpleNamespace(apply_async=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("não deve reenfileirar")))
    monkeypatch.setattr(orch_api, "detect_app", lambda _payload: orch_api.APP_ARQUIVOS)
    monkeypatch.setattr(orch_api, "resolve_monitored_folders", AsyncMock(return_value={"monitoramento/upload"}))
    monkeypatch.setattr(orch_api, "is_file_event_in_monitored_folder", lambda **_kwargs: True)
    monkeypatch.setattr(orch_api, "resolve_mapping_template_uuid", AsyncMock(return_value="template-123"))
    monkeypatch.setattr(orch_api, "claim_fileapp_ingest_receipt", AsyncMock(return_value={"id": 19, "status": "enqueued", "task_id": "93e65e1a-7461-48f0-91d5-33ba27491f54", "should_enqueue": False}))
    monkeypatch.setattr(orch_api, "ingest_fileapp_tipo1_event_task", enqueue)
    monkeypatch.setattr(orch_api, "get_settings", lambda: SimpleNamespace(celery_enabled=True, celery_fileapp_ingest_enabled=True, celery_s3_files_ingest_queue="orch_fileapp_ingest_f5_local"))

    response = await orch_api._trigger_orch_for_workspace(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=UUID(FLOW_UUID),
        payload={"file": {"id": FILE_UUID, "folder_path": "monitoramento/upload", "original_name": "critical.csv"}},
        db_session=db_session,  # type: ignore[arg-type]
        validate_workspace=False,
    )

    assert response.accepted is True
    assert response.persistence == "idempotent_replay"
    assert response.workflow_execution["receipt_id"] == 19
    assert db_session.commit.await_count == 1


@pytest.mark.asyncio
async def test_tipo1_failed_receipt_is_claimed_and_reenqueued(monkeypatch: pytest.MonkeyPatch) -> None:
    db_session = _DbSession()
    enqueue = SimpleNamespace(apply_async=lambda **_kwargs: SimpleNamespace(id="93e65e1a-7461-48f0-91d5-33ba27491f54"))
    monkeypatch.setattr(orch_api, "detect_app", lambda _payload: orch_api.APP_ARQUIVOS)
    monkeypatch.setattr(orch_api, "resolve_monitored_folders", AsyncMock(return_value={"monitoramento/upload"}))
    monkeypatch.setattr(orch_api, "is_file_event_in_monitored_folder", lambda **_kwargs: True)
    monkeypatch.setattr(orch_api, "resolve_mapping_template_uuid", AsyncMock(return_value="template-123"))
    monkeypatch.setattr(orch_api, "claim_fileapp_ingest_receipt", AsyncMock(return_value={"id": 19, "status": "accepted", "task_id": None, "should_enqueue": True}))
    monkeypatch.setattr(orch_api, "mark_fileapp_ingest_receipt_enqueued", AsyncMock())
    monkeypatch.setattr(orch_api, "ingest_fileapp_tipo1_event_task", enqueue)
    monkeypatch.setattr(orch_api, "get_settings", lambda: SimpleNamespace(celery_enabled=True, celery_fileapp_ingest_enabled=True, celery_s3_files_ingest_queue="orch_fileapp_ingest_f5_local"))

    response = await orch_api._trigger_orch_for_workspace(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=UUID(FLOW_UUID),
        payload={"file": {"id": FILE_UUID, "folder_path": "monitoramento/upload", "original_name": "critical.csv"}},
        db_session=db_session,  # type: ignore[arg-type]
        validate_workspace=False,
    )

    assert response.accepted is True
    assert response.persistence == "queued"
    orch_api.mark_fileapp_ingest_receipt_enqueued.assert_awaited_once_with(  # type: ignore[attr-defined]
        db_session,
        receipt_id=19,
        task_id="93e65e1a-7461-48f0-91d5-33ba27491f54",
    )
