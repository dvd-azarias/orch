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


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        celery_enabled=True,
        celery_fileapp_ingest_enabled=True,
        celery_result_backend=None,
        celery_s3_files_ingest_queue="orch_fileapp_ingest_events",
        celery_fileapp_entrada_hygiene_workspace_uuid=None,
        celery_fileapp_entrada_hygiene_batch_size=100,
        celery_fileapp_entrada_hygiene_lock_seconds=120,
        celery_fileapp_entrada_hygiene_no_source_list_sla_seconds=600,
        celery_fileapp_entrada_hygiene_ready_to_ingest_sla_seconds=1200,
        celery_fileapp_entrada_hygiene_pending_field_mapping_sla_seconds=1800,
        celery_fileapp_entrada_hygiene_root_max_age_seconds=1800,
        celery_fileapp_entrada_hygiene_resubmit_cooldown_seconds=300,
        celery_fileapp_entrada_hygiene_no_source_list_max_resubmits=1,
        arquivos_base_url="https://sync-core-api.otima.io/files/v1",
        sync_ws_timeout_seconds=5,
        target_core_api_bearer_token=None,
        arquivos_client_id="cid",
        arquivos_client_secret="secret",
    )


def _wire_common(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "get_settings", _settings)
    monkeypatch.setattr(tasks, "get_session_factory", lambda: (lambda: _DummySession()))
    monkeypatch.setattr(tasks, "list_completed_workspaces", AsyncMock(return_value=[{"workspace_uuid": "w1"}]))
    monkeypatch.setattr(tasks, "bind_workspace_context", lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"))
    monkeypatch.setattr(tasks, "fetch_workspace_otima_billing_api_key", AsyncMock(return_value="wk"))
    monkeypatch.setattr(
        tasks,
        "_fetch_fileapp_rescue_flow_targets",
        AsyncMock(return_value=[{"flow_uuid": "flow-1", "monitored_folders": ["ACAN_CONTATOS/entrada"]}]),
    )
    monkeypatch.setattr(tasks, "_try_acquire_fileapp_entrada_hygiene_file_lock", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(tasks, "persist_alarm", AsyncMock())


@pytest.mark.asyncio
async def test_hygiene_moves_processed_to_processados(monkeypatch) -> None:
    _wire_common(monkeypatch)
    monkeypatch.setattr(
        tasks,
        "_list_files_in_folder",
        AsyncMock(
            return_value=[
                {
                    "id": "file-1",
                    "original_name": "carga-ok.csv",
                    "folder_path": "ACAN_CONTATOS/entrada",
                    "url": "https://sync-core-api.otima.io/files/v1/files/content/file-1",
                    "created_at": (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat(),
                }
            ]
        ),
    )
    monkeypatch.setattr(
        tasks,
        "_fetch_fileapp_source_list_state",
        AsyncMock(return_value={"status": "PROCESSED", "id": 1}),
    )
    monkeypatch.setattr(
        tasks,
        "move_processed_file_to_processados",
        AsyncMock(return_value={"status": "done", "target_folder": "ACAN_CONTATOS/entrada/processados"}),
    )
    monkeypatch.setattr(tasks, "quarantine_file_to_falha", AsyncMock())
    monkeypatch.setattr(tasks, "_clear_fileapp_entrada_hygiene_resubmit_attempts", lambda *_args, **_kwargs: None)

    result = await tasks._reconcile_fileapp_entrada_hygiene_task()
    assert result["moved_to_processados"] == 1
    assert result["quarantined_to_falha"] == 0


@pytest.mark.asyncio
async def test_hygiene_resubmits_old_no_source_list(monkeypatch) -> None:
    _wire_common(monkeypatch)
    captured: dict[str, object] = {}

    def _fake_apply_async(*, kwargs, queue, routing_key):
        captured["kwargs"] = kwargs
        captured["queue"] = queue
        captured["routing_key"] = routing_key
        return SimpleNamespace(id="ingest-123")

    monkeypatch.setattr(
        tasks,
        "_list_files_in_folder",
        AsyncMock(
            return_value=[
                {
                    "id": "file-2",
                    "original_name": "orphan.csv",
                    "folder_path": "ACAN_CONTATOS/entrada",
                    "url": "https://sync-core-api.otima.io/files/v1/files/content/file-2",
                    "created_at": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
                }
            ]
        ),
    )
    monkeypatch.setattr(tasks, "_fetch_fileapp_source_list_state", AsyncMock(return_value=None))
    monkeypatch.setattr(tasks, "_get_fileapp_entrada_hygiene_resubmit_attempts", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(tasks, "_set_fileapp_entrada_hygiene_resubmit_attempts", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tasks, "_get_fileapp_entrada_rescue_flow_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tasks, "resolve_mapping_template_uuid", AsyncMock(return_value="tmpl-1"))
    monkeypatch.setattr(tasks, "_try_mark_fileapp_entrada_rescue_flow_in_flight", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.ingest_fileapp_tipo1_event_task.apply_async", _fake_apply_async)
    monkeypatch.setattr(tasks, "move_processed_file_to_processados", AsyncMock())
    monkeypatch.setattr(tasks, "quarantine_file_to_falha", AsyncMock())

    result = await tasks._reconcile_fileapp_entrada_hygiene_task()
    assert result["resubmitted"] == 1
    assert captured["kwargs"]["flow_uuid"] == "flow-1"
    assert captured["queue"] == "orch_fileapp_ingest_events"


@pytest.mark.asyncio
async def test_hygiene_quarantines_exhausted_no_source_list(monkeypatch) -> None:
    _wire_common(monkeypatch)
    monkeypatch.setattr(
        tasks,
        "_list_files_in_folder",
        AsyncMock(
            return_value=[
                {
                    "id": "file-3",
                    "original_name": "old-orphan.csv",
                    "folder_path": "ACAN_CONTATOS/entrada",
                    "url": "https://sync-core-api.otima.io/files/v1/files/content/file-3",
                    "created_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                }
            ]
        ),
    )
    monkeypatch.setattr(tasks, "_fetch_fileapp_source_list_state", AsyncMock(return_value=None))
    monkeypatch.setattr(tasks, "_get_fileapp_entrada_hygiene_resubmit_attempts", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(tasks, "_clear_fileapp_entrada_hygiene_resubmit_attempts", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        tasks,
        "quarantine_file_to_falha",
        AsyncMock(return_value={"status": "done", "target_folder": "ACAN_CONTATOS/entrada/falha"}),
    )
    monkeypatch.setattr(tasks, "move_processed_file_to_processados", AsyncMock())
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.ingest_fileapp_tipo1_event_task.apply_async",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("não deveria enfileirar ingest")),
    )

    result = await tasks._reconcile_fileapp_entrada_hygiene_task()
    assert result["quarantined_to_falha"] == 1
    assert result["resubmitted"] == 0
