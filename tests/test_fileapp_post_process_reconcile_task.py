from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.tasks.fileapp_ingest_tasks as tasks


@pytest.mark.asyncio
async def test_reconcile_fileapp_post_process_moves_candidate(monkeypatch) -> None:
    persisted: dict[str, str] = {}

    class _DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def _fake_get_session_factory():
        return lambda: _DummySession()

    async def _fake_list_completed_workspaces(_db_session):
        return [{"workspace_uuid": "f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26"}]

    async def _fake_fetch_candidates(_db_session, *, workspace_schema: str, limit: int):
        assert workspace_schema == "ws_f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26"
        assert limit == 50
        return [
            {
                "id": 47,
                "mailing_uuid": "11111111-1111-1111-1111-111111111111",
                "file_name": "arquivo_F.csv",
                "file_path": "ACAN_CONTATOS/entrada",
                "file_url": "https://sync-core-api.otima.io/files/v1/files/content/file-uuid-47",
            }
        ]

    async def _fake_move_processed(*, settings, workspace_uuid: str, payload: dict):
        assert workspace_uuid == "f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26"
        assert payload["file"]["id"] == "file-uuid-47"
        return {"status": "done", "target_folder": "ACAN_CONTATOS/entrada/processados"}

    async def _fake_persist_alarm(_db_session, **kwargs):
        persisted["code"] = kwargs.get("code", "")

    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=True,
            celery_fileapp_ingest_enabled=True,
            celery_result_backend=None,
            celery_fileapp_post_process_reconcile_workspace_uuid=None,
            celery_fileapp_post_process_reconcile_batch_size=50,
            celery_fileapp_post_process_reconcile_cooldown_seconds=120,
        ),
    )
    monkeypatch.setattr(tasks, "get_session_factory", _fake_get_session_factory)
    monkeypatch.setattr(tasks, "list_completed_workspaces", _fake_list_completed_workspaces)
    monkeypatch.setattr(
        tasks,
        "bind_workspace_context",
        lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"),
    )
    monkeypatch.setattr(tasks, "_fetch_fileapp_post_process_candidates", _fake_fetch_candidates)
    monkeypatch.setattr(tasks, "fetch_workspace_otima_billing_api_key", AsyncMock(return_value="wk"))
    monkeypatch.setattr(
        tasks,
        "_fetch_fileapp_rescue_flow_targets",
        AsyncMock(return_value=[{"flow_uuid": "flow-1", "monitored_folders": ["ACAN_CONTATOS/entrada"]}]),
    )
    monkeypatch.setattr(tasks, "resolve_detach_all_files", AsyncMock(return_value=False))
    monkeypatch.setattr(
        tasks,
        "associate_mailing_to_flow_from_file_event",
        AsyncMock(return_value={"status": "done"}),
    )
    monkeypatch.setattr(tasks, "_fetch_exhausted_quarantine_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(tasks, "move_processed_file_to_processados", _fake_move_processed)
    monkeypatch.setattr(tasks, "persist_alarm", _fake_persist_alarm)
    monkeypatch.setattr(tasks, "_try_acquire_fileapp_post_process_lock", lambda *_args, **_kwargs: True)

    result = await tasks._reconcile_fileapp_post_process_task()

    assert result["workspaces_scanned"] == 1
    assert result["candidates_scanned"] == 1
    assert result["moved"] == 1
    assert result["quarantined"] == 0
    assert result["warnings"] == 0
    assert persisted == {}


@pytest.mark.asyncio
async def test_reconcile_fileapp_post_process_records_warning_on_error(monkeypatch) -> None:
    persisted: dict[str, str] = {}

    class _DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def _fake_get_session_factory():
        return lambda: _DummySession()

    async def _fake_list_completed_workspaces(_db_session):
        return [{"workspace_uuid": "f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26"}]

    async def _fake_fetch_candidates(_db_session, *, workspace_schema: str, limit: int):
        return [
            {
                "id": 99,
                "mailing_uuid": "11111111-1111-1111-1111-111111111111",
                "file_name": "arquivo_X.csv",
                "file_path": "ACAN_CONTATOS/entrada",
                "file_url": "https://sync-core-api.otima.io/files/v1/files/content/file-uuid-99",
            }
        ]

    async def _fake_move_processed(*, settings, workspace_uuid: str, payload: dict):
        raise tasks.FileAppProcessedFileError(code="move_failed", message="falhou", details={})

    async def _fake_persist_alarm(_db_session, **kwargs):
        persisted["code"] = kwargs.get("code", "")

    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=True,
            celery_fileapp_ingest_enabled=True,
            celery_result_backend=None,
            celery_fileapp_post_process_reconcile_workspace_uuid=None,
            celery_fileapp_post_process_reconcile_batch_size=10,
            celery_fileapp_post_process_reconcile_cooldown_seconds=30,
        ),
    )
    monkeypatch.setattr(tasks, "get_session_factory", _fake_get_session_factory)
    monkeypatch.setattr(tasks, "list_completed_workspaces", _fake_list_completed_workspaces)
    monkeypatch.setattr(
        tasks,
        "bind_workspace_context",
        lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"),
    )
    monkeypatch.setattr(tasks, "_fetch_fileapp_post_process_candidates", _fake_fetch_candidates)
    monkeypatch.setattr(tasks, "fetch_workspace_otima_billing_api_key", AsyncMock(return_value="wk"))
    monkeypatch.setattr(
        tasks,
        "_fetch_fileapp_rescue_flow_targets",
        AsyncMock(return_value=[{"flow_uuid": "flow-1", "monitored_folders": ["ACAN_CONTATOS/entrada"]}]),
    )
    monkeypatch.setattr(tasks, "resolve_detach_all_files", AsyncMock(return_value=False))
    monkeypatch.setattr(
        tasks,
        "associate_mailing_to_flow_from_file_event",
        AsyncMock(return_value={"status": "done"}),
    )
    monkeypatch.setattr(tasks, "_fetch_exhausted_quarantine_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(tasks, "move_processed_file_to_processados", _fake_move_processed)
    monkeypatch.setattr(tasks, "persist_alarm", _fake_persist_alarm)
    monkeypatch.setattr(tasks, "_try_acquire_fileapp_post_process_lock", lambda *_args, **_kwargs: True)

    result = await tasks._reconcile_fileapp_post_process_task()

    assert result["workspaces_scanned"] == 1
    assert result["candidates_scanned"] == 1
    assert result["moved"] == 0
    assert result["warnings"] == 1
    assert persisted["code"] == "fileapp_post_process_reconcile_failed"


@pytest.mark.asyncio
async def test_reconcile_fileapp_post_process_quarantines_step6_exhausted(monkeypatch) -> None:
    class _DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def _fake_get_session_factory():
        return lambda: _DummySession()

    async def _fake_list_completed_workspaces(_db_session):
        return [{"workspace_uuid": "f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26"}]

    async def _fake_quarantine(*, settings, workspace_uuid: str, payload: dict):
        assert workspace_uuid == "f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26"
        assert payload["file"]["id"] == "file-uuid-step6"
        return {"status": "done", "target_folder": "ACAN_CONTATOS/entrada/falha"}

    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=True,
            celery_fileapp_ingest_enabled=True,
            celery_result_backend=None,
            celery_fileapp_post_process_reconcile_workspace_uuid=None,
            celery_fileapp_post_process_reconcile_batch_size=10,
            celery_fileapp_post_process_reconcile_cooldown_seconds=30,
        ),
    )
    monkeypatch.setattr(tasks, "get_session_factory", _fake_get_session_factory)
    monkeypatch.setattr(tasks, "list_completed_workspaces", _fake_list_completed_workspaces)
    monkeypatch.setattr(
        tasks,
        "bind_workspace_context",
        lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"),
    )
    monkeypatch.setattr(tasks, "_fetch_fileapp_post_process_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(tasks, "fetch_workspace_otima_billing_api_key", AsyncMock(return_value="wk"))
    monkeypatch.setattr(tasks, "_fetch_fileapp_rescue_flow_targets", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        tasks,
        "_fetch_exhausted_quarantine_candidates",
        AsyncMock(
            return_value=[
                {
                    "alarm_id": 1001,
                    "alarm_code": "fileapp_tipo1_step6_import_retry_exhausted",
                    "file_id": "file-uuid-step6",
                    "folder_path": "ACAN_CONTATOS/entrada",
                    "original_name": "arquivo_O.csv",
                    "file_url": "https://sync-core-api.otima.io/files/v1/files/content/file-uuid-step6",
                    "retry_step": "step6_import",
                }
            ]
        ),
    )
    monkeypatch.setattr(tasks, "quarantine_file_to_falha", _fake_quarantine)
    monkeypatch.setattr(tasks, "persist_alarm", AsyncMock())
    monkeypatch.setattr(tasks, "_try_acquire_fileapp_post_process_lock", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(tasks, "_try_acquire_fileapp_post_process_file_lock", lambda *_args, **_kwargs: True)

    result = await tasks._reconcile_fileapp_post_process_task()

    assert result["workspaces_scanned"] == 1
    assert result["exhausted_quarantined"] == 1
    assert result["warnings"] == 0


@pytest.mark.asyncio
async def test_reconcile_fileapp_post_process_blocks_move_when_association_pending(monkeypatch) -> None:
    class _DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def _fake_get_session_factory():
        return lambda: _DummySession()

    async def _fake_list_completed_workspaces(_db_session):
        return [{"workspace_uuid": "f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26"}]

    async def _fake_fetch_candidates(_db_session, *, workspace_schema: str, limit: int):
        return [
            {
                "id": 101,
                "mailing_uuid": "11111111-1111-1111-1111-111111111111",
                "file_name": "carga-0012.csv",
                "file_path": "ACAN_CONTATOS/entrada",
                "file_url": "https://sync-core-api.otima.io/files/v1/files/content/file-uuid-101",
            }
        ]

    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=True,
            celery_fileapp_ingest_enabled=True,
            celery_result_backend=None,
            celery_fileapp_post_process_reconcile_workspace_uuid=None,
            celery_fileapp_post_process_reconcile_batch_size=10,
            celery_fileapp_post_process_reconcile_cooldown_seconds=30,
        ),
    )
    monkeypatch.setattr(tasks, "get_session_factory", _fake_get_session_factory)
    monkeypatch.setattr(tasks, "list_completed_workspaces", _fake_list_completed_workspaces)
    monkeypatch.setattr(tasks, "bind_workspace_context", lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"))
    monkeypatch.setattr(tasks, "_fetch_fileapp_post_process_candidates", _fake_fetch_candidates)
    monkeypatch.setattr(tasks, "_fetch_exhausted_quarantine_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(tasks, "fetch_workspace_otima_billing_api_key", AsyncMock(return_value="wk"))
    monkeypatch.setattr(
        tasks,
        "_fetch_fileapp_rescue_flow_targets",
        AsyncMock(return_value=[{"flow_uuid": "flow-1", "monitored_folders": ["ACAN_CONTATOS/entrada"]}]),
    )
    monkeypatch.setattr(tasks, "resolve_detach_all_files", AsyncMock(return_value=False))
    monkeypatch.setattr(
        tasks,
        "associate_mailing_to_flow_from_file_event",
        AsyncMock(return_value={"status": "pending", "reason": "mailing_import_not_ready"}),
    )
    monkeypatch.setattr(
        tasks,
        "move_processed_file_to_processados",
        AsyncMock(side_effect=AssertionError("move não deveria ser chamado quando associação está pendente")),
    )
    monkeypatch.setattr(tasks, "persist_alarm", AsyncMock())
    monkeypatch.setattr(tasks, "_try_acquire_fileapp_post_process_lock", lambda *_args, **_kwargs: True)

    result = await tasks._reconcile_fileapp_post_process_task()

    assert result["candidates_scanned"] == 0
    assert result["moved"] == 0
    assert result["associations_blocked"] == 1
    assert result["warnings"] == 1


@pytest.mark.asyncio
async def test_reconcile_fileapp_post_process_resolves_empty_file_path_from_metadata(monkeypatch) -> None:
    class _DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def _fake_get_session_factory():
        return lambda: _DummySession()

    async def _fake_list_completed_workspaces(_db_session):
        return [{"workspace_uuid": "f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26"}]

    async def _fake_fetch_candidates(_db_session, *, workspace_schema: str, limit: int):
        return [
            {
                "id": 202,
                "mailing_uuid": "22222222-2222-2222-2222-222222222222",
                "file_name": "carga-7777.csv",
                "file_path": "",
                "file_url": "https://sync-core-api.otima.io/files/v1/files/content/file-uuid-202",
            }
        ]

    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=True,
            celery_fileapp_ingest_enabled=True,
            celery_result_backend=None,
            celery_fileapp_post_process_reconcile_workspace_uuid=None,
            celery_fileapp_post_process_reconcile_batch_size=10,
            celery_fileapp_post_process_reconcile_cooldown_seconds=30,
        ),
    )
    monkeypatch.setattr(tasks, "get_session_factory", _fake_get_session_factory)
    monkeypatch.setattr(tasks, "list_completed_workspaces", _fake_list_completed_workspaces)
    monkeypatch.setattr(tasks, "bind_workspace_context", lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"))
    monkeypatch.setattr(tasks, "_fetch_fileapp_post_process_candidates", _fake_fetch_candidates)
    monkeypatch.setattr(tasks, "_fetch_exhausted_quarantine_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(tasks, "fetch_workspace_otima_billing_api_key", AsyncMock(return_value="wk"))
    monkeypatch.setattr(
        tasks,
        "_fetch_fileapp_rescue_flow_targets",
        AsyncMock(return_value=[{"flow_uuid": "flow-1", "monitored_folders": ["ACAN_CONTATOS/entrada"]}]),
    )
    monkeypatch.setattr(tasks, "resolve_detach_all_files", AsyncMock(return_value=False))
    monkeypatch.setattr(
        tasks,
        "associate_mailing_to_flow_from_file_event",
        AsyncMock(return_value={"status": "done"}),
    )
    monkeypatch.setattr(
        tasks,
        "_fetch_file_metadata_by_id",
        AsyncMock(return_value={"folder_path": "ACAN_CONTATOS/entrada"}),
    )
    monkeypatch.setattr(tasks, "move_processed_file_to_processados", AsyncMock(return_value={"status": "done"}))
    monkeypatch.setattr(tasks, "persist_alarm", AsyncMock())
    monkeypatch.setattr(tasks, "_try_acquire_fileapp_post_process_lock", lambda *_args, **_kwargs: True)

    result = await tasks._reconcile_fileapp_post_process_task()

    assert result["candidates_scanned"] == 1
    assert result["moved"] == 1


@pytest.mark.asyncio
async def test_reconcile_fileapp_post_process_skips_unmonitored_candidate(monkeypatch) -> None:
    class _DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def _fake_get_session_factory():
        return lambda: _DummySession()

    async def _fake_list_completed_workspaces(_db_session):
        return [{"workspace_uuid": "f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26"}]

    async def _fake_fetch_candidates(_db_session, *, workspace_schema: str, limit: int):
        return [
            {
                "id": 303,
                "mailing_uuid": "33333333-3333-3333-3333-333333333333",
                "file_name": "carga-8888.csv",
                "file_path": "system/mailings",
                "file_url": "https://sync-core-api.otima.io/files/v1/files/content/file-uuid-303",
            }
        ]

    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            celery_enabled=True,
            celery_fileapp_ingest_enabled=True,
            celery_result_backend=None,
            celery_fileapp_post_process_reconcile_workspace_uuid=None,
            celery_fileapp_post_process_reconcile_batch_size=10,
            celery_fileapp_post_process_reconcile_cooldown_seconds=30,
        ),
    )
    monkeypatch.setattr(tasks, "get_session_factory", _fake_get_session_factory)
    monkeypatch.setattr(tasks, "list_completed_workspaces", _fake_list_completed_workspaces)
    monkeypatch.setattr(tasks, "bind_workspace_context", lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"))
    monkeypatch.setattr(tasks, "_fetch_fileapp_post_process_candidates", _fake_fetch_candidates)
    monkeypatch.setattr(tasks, "_fetch_exhausted_quarantine_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(tasks, "fetch_workspace_otima_billing_api_key", AsyncMock(return_value="wk"))
    monkeypatch.setattr(
        tasks,
        "_fetch_fileapp_rescue_flow_targets",
        AsyncMock(return_value=[{"flow_uuid": "flow-1", "monitored_folders": ["ACAN_CONTATOS/entrada"]}]),
    )
    monkeypatch.setattr(tasks, "resolve_detach_all_files", AsyncMock(return_value=False))
    monkeypatch.setattr(
        tasks,
        "associate_mailing_to_flow_from_file_event",
        AsyncMock(side_effect=AssertionError("associação não deveria ser chamada para pasta não monitorada")),
    )
    monkeypatch.setattr(
        tasks,
        "move_processed_file_to_processados",
        AsyncMock(side_effect=AssertionError("move não deveria ser chamado para pasta não monitorada")),
    )
    monkeypatch.setattr(tasks, "persist_alarm", AsyncMock())
    monkeypatch.setattr(tasks, "_try_acquire_fileapp_post_process_lock", lambda *_args, **_kwargs: True)

    result = await tasks._reconcile_fileapp_post_process_task()

    assert result["candidates_scanned"] == 0
    assert result["moved"] == 0
