from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.tasks.fileapp_ingest_tasks as tasks


class _DummySession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_reconcile_entrada_rescue_reingests_stale_file(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_get_session_factory():
        return lambda: _DummySession()

    def _fake_apply_async(*, kwargs, queue, routing_key):
        captured["kwargs"] = kwargs
        captured["queue"] = queue
        captured["routing_key"] = routing_key
        return SimpleNamespace(id="ingest-123")

    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=True,
            celery_fileapp_ingest_enabled=True,
            celery_result_backend=None,
            celery_s3_files_ingest_queue="orch_fileapp_ingest_events",
            celery_fileapp_entrada_rescue_workspace_uuid=None,
            celery_fileapp_entrada_rescue_grace_seconds=600,
            celery_fileapp_entrada_rescue_fail_after_seconds=3600,
            celery_fileapp_entrada_rescue_max_retries=3,
            celery_fileapp_entrada_rescue_lock_seconds=120,
            celery_fileapp_entrada_rescue_batch_size=50,
            arquivos_base_url="https://sync-core-api.otima.io/files/v1",
            sync_ws_timeout_seconds=5,
            target_core_api_bearer_token=None,
            arquivos_client_id="cid",
            arquivos_client_secret="secret",
        ),
    )
    monkeypatch.setattr(tasks, "get_session_factory", _fake_get_session_factory)
    monkeypatch.setattr(tasks, "list_completed_workspaces", AsyncMock(return_value=[{"workspace_uuid": "w1"}]))
    monkeypatch.setattr(tasks, "bind_workspace_context", lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"))
    monkeypatch.setattr(tasks, "fetch_workspace_otima_billing_api_key", AsyncMock(return_value="wk"))
    monkeypatch.setattr(
        tasks,
        "_fetch_fileapp_rescue_flow_targets",
        AsyncMock(return_value=[{"flow_uuid": "flow-1", "monitored_folders": ["ACAN_CONTATOS/entrada"]}]),
    )
    monkeypatch.setattr(
        tasks,
        "_list_files_in_folder",
        AsyncMock(
            return_value=[
                {
                    "id": "file-1",
                    "original_name": "carga-orfa.csv",
                    "folder_path": "ACAN_CONTATOS/entrada",
                    "url": "https://sync-core-api.otima.io/files/v1/files/content/file-1",
                    "created_at": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
                }
            ]
        ),
    )
    monkeypatch.setattr(tasks, "_has_fileapp_ingest_evidence", AsyncMock(return_value=False))
    monkeypatch.setattr(tasks, "_try_acquire_fileapp_entrada_rescue_file_lock", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(tasks, "_get_fileapp_entrada_rescue_attempts", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(tasks, "_set_fileapp_entrada_rescue_attempts", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tasks, "resolve_mapping_template_uuid", AsyncMock(return_value="tmpl-1"))
    monkeypatch.setattr(tasks, "persist_alarm", AsyncMock())
    monkeypatch.setattr(tasks, "quarantine_file_to_falha", AsyncMock())
    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.ingest_fileapp_tipo1_event_task.apply_async", _fake_apply_async)

    result = await tasks._reconcile_fileapp_entrada_rescue_task()

    assert result["reingested"] == 1
    assert result["quarantined"] == 0
    assert captured["queue"] == "orch_fileapp_ingest_events"
    assert captured["routing_key"] == "orch_fileapp_ingest_events"
    assert captured["kwargs"]["flow_uuid"] == "flow-1"
    assert captured["kwargs"]["mapping_template_uuid"] == "tmpl-1"


@pytest.mark.asyncio
async def test_reconcile_entrada_rescue_quarantines_when_exhausted(monkeypatch) -> None:
    def _fake_get_session_factory():
        return lambda: _DummySession()

    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=True,
            celery_fileapp_ingest_enabled=True,
            celery_result_backend=None,
            celery_s3_files_ingest_queue="orch_fileapp_ingest_events",
            celery_fileapp_entrada_rescue_workspace_uuid=None,
            celery_fileapp_entrada_rescue_grace_seconds=600,
            celery_fileapp_entrada_rescue_fail_after_seconds=1200,
            celery_fileapp_entrada_rescue_max_retries=3,
            celery_fileapp_entrada_rescue_lock_seconds=120,
            celery_fileapp_entrada_rescue_batch_size=50,
            arquivos_base_url="https://sync-core-api.otima.io/files/v1",
            sync_ws_timeout_seconds=5,
            target_core_api_bearer_token=None,
            arquivos_client_id="cid",
            arquivos_client_secret="secret",
        ),
    )
    monkeypatch.setattr(tasks, "get_session_factory", _fake_get_session_factory)
    monkeypatch.setattr(tasks, "list_completed_workspaces", AsyncMock(return_value=[{"workspace_uuid": "w1"}]))
    monkeypatch.setattr(tasks, "bind_workspace_context", lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"))
    monkeypatch.setattr(tasks, "fetch_workspace_otima_billing_api_key", AsyncMock(return_value="wk"))
    monkeypatch.setattr(
        tasks,
        "_fetch_fileapp_rescue_flow_targets",
        AsyncMock(return_value=[{"flow_uuid": "flow-1", "monitored_folders": ["ACAN_CONTATOS/entrada"]}]),
    )
    monkeypatch.setattr(
        tasks,
        "_list_files_in_folder",
        AsyncMock(
            return_value=[
                {
                    "id": "file-2",
                    "original_name": "carga-velha.csv",
                    "folder_path": "ACAN_CONTATOS/entrada",
                    "url": "https://sync-core-api.otima.io/files/v1/files/content/file-2",
                    "created_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                }
            ]
        ),
    )
    monkeypatch.setattr(tasks, "_has_fileapp_ingest_evidence", AsyncMock(return_value=False))
    monkeypatch.setattr(tasks, "_try_acquire_fileapp_entrada_rescue_file_lock", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(tasks, "_get_fileapp_entrada_rescue_attempts", lambda *_args, **_kwargs: 3)
    monkeypatch.setattr(tasks, "_set_fileapp_entrada_rescue_attempts", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tasks, "_clear_fileapp_entrada_rescue_attempts", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tasks, "resolve_mapping_template_uuid", AsyncMock(return_value="tmpl-1"))
    monkeypatch.setattr(
        tasks,
        "quarantine_file_to_falha",
        AsyncMock(return_value={"status": "done", "target_folder": "ACAN_CONTATOS/entrada/falha"}),
    )
    monkeypatch.setattr(tasks, "persist_alarm", AsyncMock())
    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.ingest_fileapp_tipo1_event_task.apply_async", lambda **_kwargs: None)

    result = await tasks._reconcile_fileapp_entrada_rescue_task()

    assert result["reingested"] == 0
    assert result["quarantined"] == 1
