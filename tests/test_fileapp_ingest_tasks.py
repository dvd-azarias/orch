from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.core.config import get_settings
from app.services.fileapp_processed_file_service import FileAppProcessedFileError
from app.tasks.fileapp_ingest_tasks import (
    _build_files_api_headers,
    _handle_step6_import_conflict_without_reupload,
    _is_retryable_step6_import_conflict,
    _is_retryable_step1_upload_failure,
    _list_files_in_folder,
    _process_fileapp_tipo1_event_task,
    ingest_fileapp_event_task,
    process_fileapp_tipo1_event_task,
)


class _DummyTask:
    id = "task-123"


def test_build_files_api_headers_prefers_client_credentials() -> None:
    class _DummySettings:
        target_core_api_bearer_token = "bearer-token"
        arquivos_client_id = "client-id"
        arquivos_client_secret = "client-secret"

    headers = _build_files_api_headers(
        settings=_DummySettings(),
        workspace_uuid="f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26",
        workspace_api_key=None,
    )

    assert headers["x-client-id"] == "client-id"
    assert headers["x-client-secret"] == "client-secret"
    assert headers["x-workspace-uuid"] == "f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26"
    assert "authorization" not in headers
    assert "x-api-key" not in headers
    assert "x-workspace-api-key" not in headers


def test_build_files_api_headers_uses_bearer_without_client_credentials() -> None:
    class _DummySettings:
        target_core_api_bearer_token = "bearer-token"
        arquivos_client_id = None
        arquivos_client_secret = None

    headers = _build_files_api_headers(
        settings=_DummySettings(),
        workspace_uuid="f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26",
        workspace_api_key=None,
    )

    assert headers["authorization"] == "Bearer bearer-token"
    assert headers["x-workspace-uuid"] == "f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26"
    assert "x-client-id" not in headers
    assert "x-client-secret" not in headers
    assert "x-api-key" not in headers
    assert "x-workspace-api-key" not in headers


def test_ingest_fileapp_event_task_enqueues_processing(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_apply_async(*, kwargs, queue, routing_key):  # type: ignore[no-untyped-def]
        captured["kwargs"] = kwargs
        captured["queue"] = queue
        captured["routing_key"] = routing_key
        return _DummyTask()

    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.process_fileapp_event_task.apply_async",
        _fake_apply_async,
    )

    result = ingest_fileapp_event_task.run(
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="706c6fef-85f2-4276-bcfd-eb28f75acde2",
        payload={"file": {"id": "f1", "original_name": "x.csv", "folder_path": "dev-orch/mailing"}},
    )

    assert result["status"] == "queued"
    assert result["task_id"] == "task-123"
    settings = get_settings()
    assert captured["queue"] == settings.celery_source_list_ingest_queue
    assert captured["routing_key"] == settings.celery_source_list_ingest_queue


@pytest.mark.asyncio
async def test_process_tipo1_event_continues_when_post_process_fails(monkeypatch) -> None:
    class _DummySessionCtx:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return object()

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

    class _DummySettings:
        celery_fileapp_mailing_assoc_queue = "q.mail.assoc"
        celery_fileapp_mailing_assoc_delay_seconds = 0

    class _DummyAssocTask:
        id = "assoc-123"

    persisted: dict[str, object] = {}

    def _fake_get_session_factory():  # type: ignore[no-untyped-def]
        return lambda: _DummySessionCtx()

    async def _fake_fetch_workspace_key(db_session, *, workspace_uuid):  # type: ignore[no-untyped-def]
        return "wk-key"

    async def _fake_run_pipeline(**kwargs):  # type: ignore[no-untyped-def]
        return {"mailing_uuid": "9d927acb-8494-4e9f-bb53-5df406d033d0"}

    async def _fake_download_bytes(**kwargs):  # type: ignore[no-untyped-def]
        return b"cpf,telefone\n1,2\n"

    async def _fake_move_processed(**kwargs):  # type: ignore[no-untyped-def]
        raise FileAppProcessedFileError(code="move_file_to_processados_failed", message="boom")

    async def _fake_persist_alarm(db_session, **kwargs):  # type: ignore[no-untyped-def]
        persisted.update(kwargs)

    def _fake_apply_async(*, kwargs, queue, routing_key, countdown):  # type: ignore[no-untyped-def]
        return _DummyAssocTask()

    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.get_settings", lambda: _DummySettings())
    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.get_session_factory", _fake_get_session_factory)
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.fetch_workspace_otima_billing_api_key",
        _fake_fetch_workspace_key,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.run_tipo1_manual_pipeline",
        _fake_run_pipeline,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.download_file_bytes_for_file_event",
        _fake_download_bytes,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks._fetch_existing_source_list_names",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.move_processed_file_to_processados",
        _fake_move_processed,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.associate_fileapp_mailing_task.apply_async",
        _fake_apply_async,
    )
    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.persist_alarm", _fake_persist_alarm)
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.bind_workspace_context",
        lambda workspace_uuid: (workspace_uuid, "ws_schema"),
    )

    result = await _process_fileapp_tipo1_event_task(
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="706c6fef-85f2-4276-bcfd-eb28f75acde2",
        payload={"file": {"id": "f1", "original_name": "x.csv", "folder_path": "mailings/dev"}},
        mapping_template_uuid="6fa7bde7-fa2f-49fd-9de8-d1969f6e835b",
    )

    assert result["status"] == "done"
    assert result["post_process_file"]["status"] == "warning"
    assert persisted["code"] == "fileapp_tipo1_post_process_file_failed"


@pytest.mark.asyncio
async def test_process_tipo1_event_runs_pipeline_then_post_processes_file(monkeypatch) -> None:
    class _DummySessionCtx:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return object()

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

    class _DummySettings:
        celery_fileapp_mailing_assoc_queue = "q.mail.assoc"
        celery_fileapp_mailing_assoc_delay_seconds = 0

    class _DummyAssocTask:
        id = "assoc-123"

    call_order: list[str] = []
    pipeline_kwargs_seen: dict[str, object] = {}

    def _fake_get_session_factory():  # type: ignore[no-untyped-def]
        return lambda: _DummySessionCtx()

    async def _fake_fetch_workspace_key(db_session, *, workspace_uuid):  # type: ignore[no-untyped-def]
        return "wk-key"

    async def _fake_download_bytes(**kwargs):  # type: ignore[no-untyped-def]
        call_order.append("download")
        return b"cpf,telefone\n1,2\n"

    async def _fake_move_processed(**kwargs):  # type: ignore[no-untyped-def]
        call_order.append("move")
        return {
            "status": "done",
            "target_name": "contato_deivid_tim_silver_20260622T230000Z.csv",
            "target_folder": "mailings/AeC/tim-portabilidade/processados",
        }

    async def _fake_run_pipeline(**kwargs):  # type: ignore[no-untyped-def]
        call_order.append("pipeline")
        pipeline_kwargs_seen.update(kwargs)
        return {"mailing_uuid": "9d927acb-8494-4e9f-bb53-5df406d033d0"}

    def _fake_apply_async(*, kwargs, queue, routing_key, countdown):  # type: ignore[no-untyped-def]
        call_order.append("associate")
        return _DummyAssocTask()

    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.get_settings", lambda: _DummySettings())
    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.get_session_factory", _fake_get_session_factory)
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.fetch_workspace_otima_billing_api_key",
        _fake_fetch_workspace_key,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks._fetch_existing_source_list_names",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.download_file_bytes_for_file_event",
        _fake_download_bytes,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.move_processed_file_to_processados",
        _fake_move_processed,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.run_tipo1_manual_pipeline",
        _fake_run_pipeline,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.associate_fileapp_mailing_task.apply_async",
        _fake_apply_async,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.bind_workspace_context",
        lambda workspace_uuid: (workspace_uuid, "ws_schema"),
    )

    result = await _process_fileapp_tipo1_event_task(
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="706c6fef-85f2-4276-bcfd-eb28f75acde2",
        payload={"file": {"id": "f1", "original_name": "x.csv", "folder_path": "mailings/dev"}},
        mapping_template_uuid="6fa7bde7-fa2f-49fd-9de8-d1969f6e835b",
    )

    assert result["status"] == "done"
    assert call_order == ["download", "pipeline", "associate", "move"]
    assert pipeline_kwargs_seen["predownloaded_file_bytes"] == b"cpf,telefone\n1,2\n"
    assert pipeline_kwargs_seen["upload_file_name_override"] == "x.csv"


@pytest.mark.asyncio
async def test_list_files_in_folder_accepts_files_key(monkeypatch) -> None:
    class _FakeResponse:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

        def read(self) -> bytes:
            return b'{"files":[{"id":"f1","name":"x.csv"}]}'

    class _DummySettings:
        arquivos_base_url = "https://sync-core-api.otima.io/files/v1"
        sync_ws_timeout_seconds = 5
        target_core_api_bearer_token = None
        arquivos_client_id = "cid"
        arquivos_client_secret = "secret"

    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.urlopen", lambda *_args, **_kwargs: _FakeResponse())

    files = await _list_files_in_folder(
        settings=_DummySettings(),
        workspace_uuid="f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26",
        folder_path="ACAN_CONTATOS/entrada",
        workspace_api_key=None,
        limit=10,
    )

    assert len(files) == 1
    assert files[0]["id"] == "f1"


def test_process_tipo1_wrapper_marks_inflight_then_done(monkeypatch) -> None:
    state_changes: list[str] = []

    async def _fake_process(**_kwargs):  # type: ignore[no-untyped-def]
        return {"status": "done"}

    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks._process_fileapp_tipo1_event_task", _fake_process)
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks._persist_process_tipo1_rescue_flow_state",
        lambda **kwargs: state_changes.append(str(kwargs["state"])),
    )

    result = process_fileapp_tipo1_event_task.run(
        workspace_uuid="w1",
        flow_uuid="flow-1",
        payload={"file": {"id": "f-1", "original_name": "x.csv", "folder_path": "ACAN_CONTATOS/entrada"}},
        mapping_template_uuid="tmpl-1",
    )

    assert result["status"] == "done"
    assert state_changes == ["in_flight", "done"]


def test_process_tipo1_wrapper_marks_inflight_then_failed(monkeypatch) -> None:
    state_changes: list[str] = []

    async def _fake_process(**_kwargs):  # type: ignore[no-untyped-def]
        return {"status": "failed", "reason": "unexpected"}

    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks._process_fileapp_tipo1_event_task", _fake_process)
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks._persist_process_tipo1_rescue_flow_state",
        lambda **kwargs: state_changes.append(str(kwargs["state"])),
    )

    result = process_fileapp_tipo1_event_task.run(
        workspace_uuid="w1",
        flow_uuid="flow-1",
        payload={"file": {"id": "f-2", "original_name": "x.csv", "folder_path": "ACAN_CONTATOS/entrada"}},
        mapping_template_uuid="tmpl-1",
    )

    assert result["status"] == "failed"
    assert state_changes == ["in_flight", "failed"]


def test_process_tipo1_wrapper_step6_conflict_defers_without_retrying_pipeline(monkeypatch) -> None:
    state_changes: list[str] = []
    process_calls = {"count": 0}

    async def _fake_process(**_kwargs):  # type: ignore[no-untyped-def]
        process_calls["count"] += 1
        return {
            "status": "failed",
            "reason": "step6_import",
            "details": {
                "status_code": 409,
                "response_body": '{"detail":"Source list já está em processo de ingestão."}',
                "mailing_uuid": "11111111-1111-1111-1111-111111111111",
            },
        }

    async def _fake_handle(**_kwargs):  # type: ignore[no-untyped-def]
        return {
            "status": "done",
            "reason": "step6_import_conflict_deferred",
            "retry_strategy": "association_only_no_reupload",
        }

    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks._process_fileapp_tipo1_event_task", _fake_process)
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks._handle_step6_import_conflict_without_reupload",
        _fake_handle,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks._persist_process_tipo1_rescue_flow_state",
        lambda **kwargs: state_changes.append(str(kwargs["state"])),
    )

    result = process_fileapp_tipo1_event_task.run(
        workspace_uuid="w1",
        flow_uuid="flow-1",
        payload={"file": {"id": "f-3", "original_name": "x.csv", "folder_path": "ACAN_CONTATOS/entrada"}},
        mapping_template_uuid="tmpl-1",
    )

    assert process_calls["count"] == 1
    assert result["status"] == "done"
    assert result["reason"] == "step6_import_conflict_deferred"
    assert result["retry_strategy"] == "association_only_no_reupload"
    assert state_changes == ["in_flight", "done"]


@pytest.mark.asyncio
async def test_step6_conflict_handler_prefers_canonical_mailing_uuid(monkeypatch) -> None:
    class _DummySettings:
        celery_fileapp_mailing_assoc_queue = "q.mail.assoc"
        celery_fileapp_mailing_assoc_delay_seconds = 0

    class _DummySessionCtx:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return object()

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

    captured: dict[str, object] = {}

    def _fake_get_session_factory():  # type: ignore[no-untyped-def]
        return lambda: _DummySessionCtx()

    async def _fake_resolve_canonical(_db_session, **_kwargs):  # type: ignore[no-untyped-def]
        return "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    def _fake_apply_async(*, kwargs, queue, routing_key, countdown):  # type: ignore[no-untyped-def]
        captured["kwargs"] = kwargs
        return _DummyTask()

    async def _fake_move_processed(**_kwargs):  # type: ignore[no-untyped-def]
        return {"status": "done"}

    async def _fake_persist_alarm(_db_session, **kwargs):  # type: ignore[no-untyped-def]
        captured["alarm_details"] = kwargs.get("details")

    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.get_settings", lambda: _DummySettings())
    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.get_session_factory", _fake_get_session_factory)
    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.bind_workspace_context", lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"))
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks._resolve_canonical_mailing_uuid_for_file",
        _fake_resolve_canonical,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.associate_fileapp_mailing_task.apply_async",
        _fake_apply_async,
    )
    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.move_processed_file_to_processados", _fake_move_processed)
    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.persist_alarm", _fake_persist_alarm)

    result = await _handle_step6_import_conflict_without_reupload(
        workspace_uuid="f0d1d7cf-8ddd-4dcb-9477-d87c11e81c26",
        flow_uuid="b049eca9-d856-4379-9c83-434b22036a1b",
        payload={
            "file": {
                "id": "file-123",
                "original_name": "carga-0012.csv",
                "folder_path": "ACAN_CONTATOS/entrada",
                "url": "https://sync-core-api.otima.io/files/v1/files/content/file-123",
            }
        },
        mapping_template_uuid="d94523f9-33d0-42db-b5f9-cb3f2c226bfb",
        result={
            "status": "failed",
            "reason": "step6_import",
            "details": {
                "status_code": 409,
                "response_body": '{"detail":"Source list já está em processo de ingestão."}',
                "mailing_uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            },
        },
    )

    assert result["status"] == "done"
    assert result["mailing_uuid"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert captured["kwargs"]["mailing_uuid"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert captured["alarm_details"]["mailing_uuid_from_conflict"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert captured["alarm_details"]["mailing_uuid_canonical"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_process_tipo1_event_continues_when_association_enqueue_fails(monkeypatch) -> None:
    class _DummySessionCtx:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return object()

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

    class _DummySettings:
        celery_fileapp_mailing_assoc_queue = "q.mail.assoc"
        celery_fileapp_mailing_assoc_delay_seconds = 0

    persisted: dict[str, object] = {}

    def _fake_get_session_factory():  # type: ignore[no-untyped-def]
        return lambda: _DummySessionCtx()

    async def _fake_fetch_workspace_key(db_session, *, workspace_uuid):  # type: ignore[no-untyped-def]
        return "wk-key"

    async def _fake_download_bytes(**kwargs):  # type: ignore[no-untyped-def]
        return b"cpf,telefone\n1,2\n"

    async def _fake_run_pipeline(**kwargs):  # type: ignore[no-untyped-def]
        return {"mailing_uuid": "9d927acb-8494-4e9f-bb53-5df406d033d0"}

    async def _fake_move_processed(**kwargs):  # type: ignore[no-untyped-def]
        return {"status": "done", "target_folder": "mailings/dev/processados"}

    async def _fake_persist_alarm(db_session, **kwargs):  # type: ignore[no-untyped-def]
        persisted.update(kwargs)

    def _fake_apply_async(*, kwargs, queue, routing_key, countdown):  # type: ignore[no-untyped-def]
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.get_settings", lambda: _DummySettings())
    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.get_session_factory", _fake_get_session_factory)
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.fetch_workspace_otima_billing_api_key",
        _fake_fetch_workspace_key,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks._fetch_existing_source_list_names",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.download_file_bytes_for_file_event",
        _fake_download_bytes,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.run_tipo1_manual_pipeline",
        _fake_run_pipeline,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.move_processed_file_to_processados",
        _fake_move_processed,
    )
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.associate_fileapp_mailing_task.apply_async",
        _fake_apply_async,
    )
    monkeypatch.setattr("app.tasks.fileapp_ingest_tasks.persist_alarm", _fake_persist_alarm)
    monkeypatch.setattr(
        "app.tasks.fileapp_ingest_tasks.bind_workspace_context",
        lambda workspace_uuid: (workspace_uuid, "ws_schema"),
    )

    result = await _process_fileapp_tipo1_event_task(
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="706c6fef-85f2-4276-bcfd-eb28f75acde2",
        payload={"file": {"id": "f1", "original_name": "x.csv", "folder_path": "mailings/dev"}},
        mapping_template_uuid="6fa7bde7-fa2f-49fd-9de8-d1969f6e835b",
    )

    assert result["status"] == "done"
    assert result["mailing_association"]["status"] == "warning"
    assert result["post_process_file"]["status"] == "done"
    assert persisted["code"] == "fileapp_tipo1_mailing_association_enqueue_failed"


def test_is_retryable_step1_upload_failure_for_http_500() -> None:
    result = {
        "status": "failed",
        "reason": "step1_upload",
        "details": {"status_code": 500, "response_body": '{"detail":"Internal Server Error"}'},
    }
    assert _is_retryable_step1_upload_failure(result) is True


def test_is_retryable_step1_upload_failure_for_http_400() -> None:
    result = {
        "status": "failed",
        "reason": "step1_upload",
        "details": {"status_code": 400, "response_body": '{"detail":"Bad Request"}'},
    }
    assert _is_retryable_step1_upload_failure(result) is False


def test_is_retryable_step6_import_conflict_for_http_409() -> None:
    result = {
        "status": "failed",
        "reason": "step6_import",
        "details": {"status_code": 409, "response_body": '{"detail":"Source list já está em processo de ingestão."}'},
    }
    assert _is_retryable_step6_import_conflict(result) is True


def test_is_retryable_step6_import_conflict_for_http_400() -> None:
    result = {
        "status": "failed",
        "reason": "step6_import",
        "details": {"status_code": 400, "response_body": '{"detail":"Bad Request"}'},
    }
    assert _is_retryable_step6_import_conflict(result) is False
