from __future__ import annotations

import io
from urllib.error import HTTPError, URLError

import pytest

import app.services.fileapp_tipo1_manual_pipeline_service as service


class _DummySettings:
    sync_webhook_base_url = "http://target-core-api.otima.io"
    sync_ws_timeout_seconds = 5.0
    target_core_api_bearer_token = "token-123"
    sync_ws_client_id = "client-id"
    sync_ws_client_secret = "client-secret"


@pytest.mark.asyncio
async def test_run_tipo1_manual_pipeline_executes_7_steps(monkeypatch) -> None:
    json_calls: list[dict] = []

    def _fake_download(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        assert url == "https://sync-core-api.otima.io/files/v1/files/content/file-123"
        assert headers["x-client-id"] == "client-id"
        assert headers["x-client-secret"] == "client-secret"
        return b"CPF,telefone\n20000000000,5521975670000\n"

    def _fake_multipart_request(*, url, headers, upload, timeout_seconds):  # type: ignore[no-untyped-def]
        assert url == "http://target-core-api.otima.io/v2/mailings/upload"
        assert headers["authorization"] == "Bearer token-123"
        assert headers["idempotency-key"] == (
            "fileapp:flow-uuid-1:2f388d0f-5519-4e30-99ad-de34c96b9a59"
        )
        assert upload.file_name == "mailing.csv"
        assert upload.description == "Carga via evento de cópia de arquivo no SFTP - mailing"
        return 200, '{"data":{"mailing_id":"mailing-uuid-1"}}'

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        json_calls.append({"method": method, "url": url, "payload": payload, "headers": headers})
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"719cbdca-ec3c-4213-9112-96d9a53cb68a"}]}'
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"put_suggestion":{"mappings":[{"id":10,"contact_system_field_id":"a26cbb26-2126-46f8-907c-757ab6dc2790","is_ignored":false,"dialer_label":null,"field_type":"text"}]}}}'
        if method == "PATCH" and url.endswith("/v2/mailings/mailing-uuid-1"):
            assert payload == {
                "mapping_template_id": "719cbdca-ec3c-4213-9112-96d9a53cb68a",
                "name": "mailing",
                "description": "Carga via evento de cópia de arquivo no SFTP - mailing",
            }
            return 200, '{"data":{"ok":true}}'
        if method == "PUT" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"status":"READY_TO_INGEST"}}'
        if method == "POST" and url.endswith("/v2/mailings/mailing-uuid-1/import"):
            return 200, '{"data":{"task_id":"import-task-1"}}'
        if method == "POST" and url.endswith("/v2/flow/flow-uuid-1/mailings"):
            return 200, '{"data":[{"results":{"linked":["mailing-uuid-1"],"unassigned":[],"errors":{}}}]}'
        raise AssertionError(f"Unexpected call: {method} {url}")

    monkeypatch.setattr(service, "_download_file_bytes", _fake_download)
    monkeypatch.setattr(service, "_multipart_request", _fake_multipart_request)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    result = await service.run_tipo1_manual_pipeline(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="flow-uuid-1",
        payload={
            "file": {
                "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                "url": "https://sync-core-api.otima.io/files/v1/files/content/file-123",
                "original_name": "mailing.csv",
                "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            }
        },
        mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
        workspace_api_key=None,
    )

    assert result["status"] == "done"
    assert result["mailing_uuid"] == "mailing-uuid-1"
    assert result["import_task_id"] == "import-task-1"
    assert len(result["steps"]) == 7
    final_call = json_calls[-1]
    assert final_call["method"] == "POST"
    assert final_call["url"].endswith("/v2/flow/flow-uuid-1/mailings")
    assert final_call["payload"] == {
        "mailing_ids_added": ["mailing-uuid-1"],
        "mailing_ids_removed": [],
        "linked_by": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
        "call_origin": "file_event",
    }
    assert final_call["headers"]["idempotency-key"] == (
        "fileapp:flow-uuid-1:2f388d0f-5519-4e30-99ad-de34c96b9a59"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mailing_status", ["INGESTING", "PROCESSED"])
async def test_run_tipo1_manual_pipeline_accepts_target_auto_ingest_race(
    monkeypatch,
    mailing_status: str,
) -> None:
    json_calls: list[tuple[str, str]] = []

    def _fake_download(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        return b"CPF,telefone\n20000000000,5521975670000\n"

    def _fake_multipart_request(*, url, headers, upload, timeout_seconds):  # type: ignore[no-untyped-def]
        return 200, '{"data":{"mailing_id":"mailing-uuid-1"}}'

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        json_calls.append((method, url))
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"719cbdca-ec3c-4213-9112-96d9a53cb68a"}]}'
        if method == "PATCH" and url.endswith("/v2/mailings/mailing-uuid-1"):
            return 200, '{"data":{"ok":true}}'
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"put_suggestion":{"mappings":[{"id":10,"is_ignored":false}]}}}'
        if method == "PUT" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, f'{{"data":{{"status":"{mailing_status}"}}}}'
        raise AssertionError(f"Unexpected call: {method} {url}")

    monkeypatch.setattr(service, "_download_file_bytes", _fake_download)
    monkeypatch.setattr(service, "_multipart_request", _fake_multipart_request)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    result = await service.run_tipo1_manual_pipeline(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="flow-uuid-1",
        payload={
            "file": {
                "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                "url": "https://sync-core-api.otima.io/files/v1/files/content/file-123",
                "original_name": "mailing.csv",
                "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            }
        },
        mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
        workspace_api_key=None,
        defer_step7_link_flow=True,
    )

    assert result["status"] == "done"
    assert result["import_task_id"] is None
    assert len(result["steps"]) == 7
    assert result["steps"][5] == {
        "step": "step6_import",
        "status": "skipped",
        "reason": "target_core_auto_ingest_active_or_done",
        "mailing_status": mailing_status,
    }
    assert all(not url.endswith("/import") for _, url in json_calls)


@pytest.mark.asyncio
async def test_run_tipo1_manual_pipeline_rejects_non_progressing_mapping_status(monkeypatch) -> None:
    def _fake_download(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        return b"CPF,telefone\n20000000000,5521975670000\n"

    def _fake_multipart_request(*, url, headers, upload, timeout_seconds):  # type: ignore[no-untyped-def]
        return 200, '{"data":{"mailing_id":"mailing-uuid-1"}}'

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1"):
            return 200, '{"data":{"status":"PENDING_FIELD_MAPPING"}}'
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"719cbdca-ec3c-4213-9112-96d9a53cb68a"}]}'
        if method == "PATCH" and url.endswith("/v2/mailings/mailing-uuid-1"):
            return 200, '{"data":{"ok":true}}'
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"put_suggestion":{"mappings":[{"id":10,"is_ignored":false}]}}}'
        if method == "PUT" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"status":"PENDING_FIELD_MAPPING"}}'
        raise AssertionError(f"Unexpected call: {method} {url}")

    monkeypatch.setattr(service, "_download_file_bytes", _fake_download)
    monkeypatch.setattr(service, "_multipart_request", _fake_multipart_request)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    with pytest.raises(service.FileAppTipo1ManualPipelineError) as exc_info:
        await service.run_tipo1_manual_pipeline(
            settings=_DummySettings(),
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            flow_uuid="flow-uuid-1",
            payload={
                "file": {
                    "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                    "url": "https://sync-core-api.otima.io/files/v1/files/content/file-123",
                    "original_name": "mailing.csv",
                    "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
                }
            },
            mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
            workspace_api_key=None,
            defer_step7_link_flow=True,
        )

    assert exc_info.value.step == "step5_put_field_mappings"
    assert exc_info.value.details["mailing_status"] == "PENDING_FIELD_MAPPING"


@pytest.mark.asyncio
async def test_run_tipo1_manual_pipeline_fail_fast_when_template_not_found(monkeypatch) -> None:
    def _fake_download(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        return b"CPF,telefone\n20000000000,5521975670000\n"

    def _fake_multipart_request(*, url, headers, upload, timeout_seconds):  # type: ignore[no-untyped-def]
        return 200, '{"data":{"mailing_id":"mailing-uuid-1"}}'

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"other-template"}]}'
        raise AssertionError(f"Should not proceed after step2 failure: {method} {url}")

    monkeypatch.setattr(service, "_download_file_bytes", _fake_download)
    monkeypatch.setattr(service, "_multipart_request", _fake_multipart_request)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    with pytest.raises(service.FileAppTipo1ManualPipelineError) as exc_info:
        await service.run_tipo1_manual_pipeline(
            settings=_DummySettings(),
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            flow_uuid="flow-uuid-1",
            payload={
                "file": {
                    "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                    "url": "https://sync-core-api.otima.io/files/v1/files/content/file-123",
                    "original_name": "mailing.csv",
                    "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
                }
            },
            mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
            workspace_api_key=None,
        )
    assert exc_info.value.step == "step2_templates"


@pytest.mark.asyncio
async def test_run_tipo1_manual_pipeline_falls_back_to_uuid_from_file_url(monkeypatch) -> None:
    def _fake_download(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        return b"CPF,telefone\n20000000000,5521975670000\n"

    def _fake_multipart_request(*, url, headers, upload, timeout_seconds):  # type: ignore[no-untyped-def]
        return 200, '{"data":{"mailing_id":"mailing-uuid-1"}}'

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"719cbdca-ec3c-4213-9112-96d9a53cb68a"}]}'
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"put_suggestion":{"mappings":[{"id":10,"contact_system_field_id":"a26cbb26-2126-46f8-907c-757ab6dc2790","is_ignored":false,"dialer_label":null,"field_type":"text"}]}}}'
        if method == "PATCH" and url.endswith("/v2/mailings/mailing-uuid-1"):
            return 200, '{"data":{"ok":true}}'
        if method == "PUT" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"status":"READY_TO_INGEST"}}'
        if method == "POST" and url.endswith("/v2/mailings/mailing-uuid-1/import"):
            return 200, '{"data":{"task_id":"import-task-1"}}'
        if method == "POST" and url.endswith("/v2/flow/flow-uuid-1/mailings"):
            assert payload is not None
            assert payload["linked_by"] == "9a6b8198-8f26-4dca-8b3d-43b9c801f1ec"
            return 200, '{"data":[{"results":{"linked":["mailing-uuid-1"],"unassigned":[],"errors":{}}}]}'
        raise AssertionError(f"Unexpected call: {method} {url}")

    monkeypatch.setattr(service, "_download_file_bytes", _fake_download)
    monkeypatch.setattr(service, "_multipart_request", _fake_multipart_request)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    result = await service.run_tipo1_manual_pipeline(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="flow-uuid-1",
        payload={
            "file": {
                "id": "file-event-without-uuid",
                "url": "https://sync-core-api.otima.io/files/v1/files/content/9a6b8198-8f26-4dca-8b3d-43b9c801f1ec",
                "original_name": "mailing.csv",
                "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            }
        },
        mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
        workspace_api_key=None,
    )
    assert result["status"] == "done"


@pytest.mark.asyncio
async def test_run_tipo1_manual_pipeline_defers_step7_when_requested(monkeypatch) -> None:
    json_calls: list[dict] = []

    def _fake_download(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        return b"CPF,telefone\n20000000000,5521975670000\n"

    def _fake_multipart_request(*, url, headers, upload, timeout_seconds):  # type: ignore[no-untyped-def]
        return 200, '{"data":{"mailing_id":"mailing-uuid-1"}}'

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        json_calls.append({"method": method, "url": url, "payload": payload})
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"719cbdca-ec3c-4213-9112-96d9a53cb68a"}]}'
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"put_suggestion":{"mappings":[{"id":10,"contact_system_field_id":"a26cbb26-2126-46f8-907c-757ab6dc2790","is_ignored":false,"dialer_label":null,"field_type":"text"}]}}}'
        if method == "PATCH" and url.endswith("/v2/mailings/mailing-uuid-1"):
            return 200, '{"data":{"ok":true}}'
        if method == "PUT" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"status":"READY_TO_INGEST"}}'
        if method == "POST" and url.endswith("/v2/mailings/mailing-uuid-1/import"):
            return 200, '{"data":{"task_id":"import-task-1"}}'
        raise AssertionError(f"Unexpected call: {method} {url}")

    monkeypatch.setattr(service, "_download_file_bytes", _fake_download)
    monkeypatch.setattr(service, "_multipart_request", _fake_multipart_request)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    result = await service.run_tipo1_manual_pipeline(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="flow-uuid-1",
        payload={
            "file": {
                "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                "url": "https://sync-core-api.otima.io/files/v1/files/content/file-123",
                "original_name": "mailing.csv",
                "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            }
        },
        mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
        workspace_api_key=None,
        defer_step7_link_flow=True,
    )
    assert result["status"] == "done"
    assert len(result["steps"]) == 7
    assert result["steps"][-1] == {"step": "step7_link_flow", "status": "deferred", "mode": "async_celery"}
    assert all(not call["url"].endswith("/v2/flow/flow-uuid-1/mailings") for call in json_calls)


@pytest.mark.asyncio
async def test_run_tipo1_manual_pipeline_fetches_field_mappings_after_patch(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def _fake_download(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        return b"CPF,telefone\n20000000000,5521975670000\n"

    def _fake_multipart_request(*, url, headers, upload, timeout_seconds):  # type: ignore[no-untyped-def]
        return 200, '{"data":{"mailing_id":"mailing-uuid-1"}}'

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        calls.append((method, url))
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"719cbdca-ec3c-4213-9112-96d9a53cb68a"}]}'
        if method == "PATCH" and url.endswith("/v2/mailings/mailing-uuid-1"):
            return 200, '{"data":{"ok":true}}'
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"put_suggestion":{"mappings":[{"id":99,"contact_system_field_id":"94949070-f4b5-48a7-a0a0-5179aa04451a","is_ignored":false,"dialer_label":null,"field_type":"text"}]}}}'
        if method == "PUT" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            assert payload == {
                "mappings": [
                    {
                        "id": 99,
                        "contact_system_field_id": "94949070-f4b5-48a7-a0a0-5179aa04451a",
                        "is_ignored": False,
                        "dialer_label": None,
                        "field_type": "text",
                    }
                ]
            }
            return 200, '{"data":{"status":"READY_TO_INGEST"}}'
        if method == "POST" and url.endswith("/v2/mailings/mailing-uuid-1/import"):
            return 200, '{"data":{"task_id":"import-task-1"}}'
        if method == "POST" and url.endswith("/v2/flow/flow-uuid-1/mailings"):
            return 200, '{"data":[{"results":{"linked":["mailing-uuid-1"],"unassigned":[],"errors":{}}}]}'
        raise AssertionError(f"Unexpected call: {method} {url}")

    monkeypatch.setattr(service, "_download_file_bytes", _fake_download)
    monkeypatch.setattr(service, "_multipart_request", _fake_multipart_request)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    await service.run_tipo1_manual_pipeline(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="flow-uuid-1",
        payload={
            "file": {
                "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                "url": "https://sync-core-api.otima.io/files/v1/files/content/file-123",
                "original_name": "mailing.csv",
                "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            }
        },
        mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
        workspace_api_key=None,
    )

    patch_idx = calls.index(("PATCH", "http://target-core-api.otima.io/v2/mailings/mailing-uuid-1"))
    get_mappings_idx = calls.index(("GET", "http://target-core-api.otima.io/v2/mailings/mailing-uuid-1/field-mappings"))
    assert get_mappings_idx > patch_idx


@pytest.mark.asyncio
async def test_run_tipo1_manual_pipeline_recovers_transients_without_reupload(monkeypatch) -> None:
    calls = {"upload": 0, "patch": 0, "get_mappings": 0, "put_mappings": 0}
    sleep_calls: list[float] = []

    def _fake_download(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        return b"CPF,telefone\n20000000000,5521975670000\n"

    def _fake_multipart_request(*, url, headers, upload, timeout_seconds):  # type: ignore[no-untyped-def]
        calls["upload"] += 1
        return 200, '{"data":{"mailing_id":"mailing-uuid-1"}}'

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"719cbdca-ec3c-4213-9112-96d9a53cb68a"}]}'
        if method == "PATCH" and url.endswith("/v2/mailings/mailing-uuid-1"):
            calls["patch"] += 1
            if calls["patch"] == 1:
                raise HTTPError(url, 503, "unavailable", {}, io.BytesIO(b'{"detail":"busy"}'))
            return 200, '{"data":{"ok":true}}'
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            calls["get_mappings"] += 1
            if calls["get_mappings"] == 1:
                return 200, '{"data":{"put_suggestion":{"mappings":[]}}}'
            return 200, '{"data":{"put_suggestion":{"mappings":[{"id":10,"is_ignored":false}]}}}'
        if method == "PUT" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            calls["put_mappings"] += 1
            if calls["put_mappings"] == 1:
                return 200, '{"data":{"status":"PENDING_FIELD_MAPPING"}}'
            return 200, '{"data":{"status":"READY_TO_INGEST"}}'
        if method == "POST" and url.endswith("/v2/mailings/mailing-uuid-1/import"):
            return 200, '{"data":{"task_id":"import-task-1"}}'
        raise AssertionError(f"Unexpected call: {method} {url}")

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(service, "_download_file_bytes", _fake_download)
    monkeypatch.setattr(service, "_multipart_request", _fake_multipart_request)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)
    monkeypatch.setattr(service.asyncio, "sleep", _fake_sleep)

    result = await service.run_tipo1_manual_pipeline(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="flow-uuid-1",
        payload={
            "file": {
                "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                "url": "https://sync-core-api.otima.io/files/v1/files/content/file-123",
                "original_name": "mailing.csv",
                "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            }
        },
        mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
        workspace_api_key=None,
        defer_step7_link_flow=True,
    )

    assert result["status"] == "done"
    assert calls == {"upload": 1, "patch": 2, "get_mappings": 2, "put_mappings": 2}
    assert sleep_calls == [0.25, 0.25, 0.25]
    steps = {step["step"]: step for step in result["steps"]}
    assert steps["step4_patch_mailing"]["attempts"] == 2
    assert steps["step3_field_mappings_get"]["attempts"] == 2
    assert steps["step5_put_field_mappings"]["attempts"] == 2


@pytest.mark.asyncio
async def test_run_tipo1_manual_pipeline_resumes_receipt_mailing_without_upload(monkeypatch) -> None:
    calls = {"upload": 0, "resolved": []}

    def _unexpected_upload(**_kwargs):  # type: ignore[no-untyped-def]
        calls["upload"] += 1
        raise AssertionError("mailing já persistido no receipt não pode ser reenviado")

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1"):
            return 200, '{"data":{"status":"PENDING_FIELD_MAPPING"}}'
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"719cbdca-ec3c-4213-9112-96d9a53cb68a"}]}'
        if method == "PATCH" and url.endswith("/v2/mailings/mailing-uuid-1"):
            return 200, '{"data":{"ok":true}}'
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"put_suggestion":{"mappings":[{"id":10,"is_ignored":false}]}}}'
        if method == "PUT" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"status":"READY_TO_INGEST"}}'
        if method == "POST" and url.endswith("/v2/mailings/mailing-uuid-1/import"):
            return 200, '{"data":{"task_id":"import-task-1"}}'
        raise AssertionError(f"Unexpected call: {method} {url}")

    async def _on_resolved(mailing_uuid: str) -> None:
        calls["resolved"].append(mailing_uuid)

    monkeypatch.setattr(service, "_multipart_request", _unexpected_upload)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    result = await service.run_tipo1_manual_pipeline(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="flow-uuid-1",
        payload={
            "file": {
                "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                "original_name": "mailing.csv",
            }
        },
        mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
        existing_mailing_uuid="mailing-uuid-1",
        on_mailing_resolved=_on_resolved,
        defer_step7_link_flow=True,
    )

    assert result["status"] == "done"
    assert result["mailing_uuid"] == "mailing-uuid-1"
    assert result["steps"][0] == {
        "step": "step1_upload",
        "status": "resumed",
        "reason": "receipt_mailing_provenance",
        "mailing_id": "mailing-uuid-1",
    }
    assert calls == {"upload": 0, "resolved": ["mailing-uuid-1"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observed_status", "expects_conflict"),
    [("INGESTING", True), ("READY_TO_INGEST", False)],
)
async def test_ambiguous_import_response_converges_or_requests_idempotent_retry(
    monkeypatch,
    observed_status: str,
    expects_conflict: bool,
) -> None:
    status_reads = 0

    def _unexpected_upload(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("mailing persistido no receipt não pode ser reenviado")

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        nonlocal status_reads
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1"):
            status_reads += 1
            status = "PENDING_FIELD_MAPPING" if status_reads == 1 else observed_status
            return 200, f'{{"data":{{"status":"{status}"}}}}'
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"719cbdca-ec3c-4213-9112-96d9a53cb68a"}]}'
        if method == "PATCH" and url.endswith("/v2/mailings/mailing-uuid-1"):
            return 200, '{"data":{"ok":true}}'
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"put_suggestion":{"mappings":[{"id":10,"is_ignored":false}]}}}'
        if method == "PUT" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"status":"READY_TO_INGEST"}}'
        if method == "POST" and url.endswith("/v2/mailings/mailing-uuid-1/import"):
            raise TimeoutError("response lost after publish")
        raise AssertionError(f"Unexpected call: {method} {url}")

    monkeypatch.setattr(service, "_multipart_request", _unexpected_upload)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    with pytest.raises(service.FileAppTipo1ManualPipelineError) as exc_info:
        await service.run_tipo1_manual_pipeline(
            settings=_DummySettings(),
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            flow_uuid="flow-uuid-1",
            payload={
                "file": {
                    "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                    "original_name": "mailing.csv",
                }
            },
            mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
            existing_mailing_uuid="mailing-uuid-1",
            defer_step7_link_flow=True,
        )

    assert exc_info.value.step == "step6_import"
    assert exc_info.value.details["mailing_uuid"] == "mailing-uuid-1"
    assert exc_info.value.details["retryable_import_transport"] is (not expects_conflict)
    assert exc_info.value.details["mailing_status"] == observed_status
    assert (exc_info.value.details.get("status_code") == 409) is expects_conflict
    assert status_reads == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mailing_status", ["INGESTING", "PROCESSED"])
async def test_resume_after_import_converges_before_mutating_mailing(
    monkeypatch,
    mailing_status: str,
) -> None:
    calls: list[tuple[str, str]] = []

    def _unexpected_upload(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("resume com mailing persistido nao pode fazer upload")

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        calls.append((method, url))
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1"):
            return 200, f'{{"data":{{"status":"{mailing_status}"}}}}'
        raise AssertionError("mailing ativo nao pode repetir PATCH, PUT ou import")

    monkeypatch.setattr(service, "_multipart_request", _unexpected_upload)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    with pytest.raises(service.FileAppTipo1ManualPipelineError) as exc_info:
        await service.run_tipo1_manual_pipeline(
            settings=_DummySettings(),
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            flow_uuid="flow-uuid-1",
            payload={
                "file": {
                    "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                    "original_name": "mailing.csv",
                }
            },
            mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
            existing_mailing_uuid="mailing-uuid-1",
            defer_step7_link_flow=True,
        )

    assert exc_info.value.step == "step6_import"
    assert exc_info.value.details["mailing_uuid"] == "mailing-uuid-1"
    assert exc_info.value.details["mailing_status"] == mailing_status
    assert calls == [
        ("GET", "http://target-core-api.otima.io/v2/mailings/mailing-uuid-1")
    ]


@pytest.mark.asyncio
async def test_idempotent_upload_replay_converges_from_response_status(monkeypatch) -> None:
    seen_headers: list[dict[str, str]] = []

    def _fake_multipart_request(*, url, headers, upload, timeout_seconds):  # type: ignore[no-untyped-def]
        seen_headers.append(dict(headers))
        return 200, (
            '{"data":{"mailing_id":"mailing-uuid-1",'
            '"status":"INGESTING","file":{"status":"existing"}}}'
        )

    def _unexpected_json_request(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("replay ativo nao pode repetir PATCH, PUT ou import")

    monkeypatch.setattr(service, "_multipart_request", _fake_multipart_request)
    monkeypatch.setattr(service, "_json_request", _unexpected_json_request)

    with pytest.raises(service.FileAppTipo1ManualPipelineError) as exc_info:
        await service.run_tipo1_manual_pipeline(
            settings=_DummySettings(),
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            flow_uuid="flow-uuid-1",
            payload={
                "file": {
                    "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                    "original_name": "mailing.csv",
                }
            },
            mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
            predownloaded_file_bytes=b"id,phone\n1,2\n",
            defer_step7_link_flow=True,
        )

    assert exc_info.value.step == "step6_import"
    assert exc_info.value.details["mailing_status"] == "INGESTING"
    assert seen_headers[0]["idempotency-key"] == (
        "fileapp:flow-uuid-1:2f388d0f-5519-4e30-99ad-de34c96b9a59"
    )


@pytest.mark.asyncio
async def test_ambiguous_upload_timeout_keeps_retryable_idempotency_key(monkeypatch) -> None:
    seen_headers: list[dict[str, str]] = []

    def _timeout_after_remote_side_effect(*, url, headers, upload, timeout_seconds):  # type: ignore[no-untyped-def]
        seen_headers.append(dict(headers))
        raise TimeoutError("response lost after commit")

    monkeypatch.setattr(
        service,
        "_multipart_request",
        _timeout_after_remote_side_effect,
    )

    with pytest.raises(service.FileAppTipo1ManualPipelineError) as exc_info:
        await service.run_tipo1_manual_pipeline(
            settings=_DummySettings(),
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            flow_uuid="flow-uuid-1",
            payload={
                "file": {
                    "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                    "original_name": "mailing.csv",
                }
            },
            mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
            predownloaded_file_bytes=b"id,phone\n1,2\n",
            defer_step7_link_flow=True,
        )

    assert exc_info.value.step == "step1_upload"
    assert exc_info.value.details["error_type"] == "TimeoutError"
    assert seen_headers[0]["idempotency-key"] == (
        "fileapp:flow-uuid-1:2f388d0f-5519-4e30-99ad-de34c96b9a59"
    )


@pytest.mark.asyncio
async def test_provenance_failure_stops_pipeline_and_exposes_uploaded_mailing(monkeypatch) -> None:
    calls: list[str] = []

    def _fake_multipart_request(**_kwargs):  # type: ignore[no-untyped-def]
        calls.append("upload")
        return 200, '{"data":{"mailing_id":"mailing-uuid-1"}}'

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"719cbdca-ec3c-4213-9112-96d9a53cb68a"}]}'
        calls.append(f"unexpected:{method}:{url}")
        raise AssertionError("pipeline nao pode avancar sem proveniencia persistida")

    async def _fail_provenance(_mailing_uuid: str) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(service, "_multipart_request", _fake_multipart_request)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    with pytest.raises(service.FileAppTipo1ManualPipelineError) as exc_info:
        await service.run_tipo1_manual_pipeline(
            settings=_DummySettings(),
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            flow_uuid="flow-uuid-1",
            payload={
                "file": {
                    "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                    "original_name": "mailing.csv",
                }
            },
            mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
            predownloaded_file_bytes=b"id,phone\n1,2\n",
            on_mailing_resolved=_fail_provenance,
            defer_step7_link_flow=True,
        )

    assert exc_info.value.step == "step1_provenance"
    assert exc_info.value.details == {
        "mailing_uuid": "mailing-uuid-1",
        "error_type": "RuntimeError",
    }
    assert calls == ["upload"]


@pytest.mark.asyncio
async def test_step5_conflict_preserves_exact_mailing_for_convergence(monkeypatch) -> None:
    def _fake_multipart_request(**_kwargs):  # type: ignore[no-untyped-def]
        return 200, '{"data":{"mailing_id":"mailing-uuid-1"}}'

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"719cbdca-ec3c-4213-9112-96d9a53cb68a"}]}'
        if method == "PATCH" and url.endswith("/v2/mailings/mailing-uuid-1"):
            return 200, '{"data":{"ok":true}}'
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"put_suggestion":{"mappings":[{"id":10,"is_ignored":false}]}}}'
        if method == "PUT" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            raise HTTPError(
                url,
                409,
                "conflict",
                {},
                io.BytesIO(b'{"detail":"Source list ja esta em processo de ingestao."}'),
            )
        raise AssertionError(f"Unexpected call: {method} {url}")

    monkeypatch.setattr(service, "_multipart_request", _fake_multipart_request)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    with pytest.raises(service.FileAppTipo1ManualPipelineError) as exc_info:
        await service.run_tipo1_manual_pipeline(
            settings=_DummySettings(),
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            flow_uuid="flow-uuid-1",
            payload={
                "file": {
                    "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                    "original_name": "mailing.csv",
                }
            },
            mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
            predownloaded_file_bytes=b"id,phone\n1,2\n",
            defer_step7_link_flow=True,
        )

    assert exc_info.value.step == "step5_put_field_mappings"
    assert exc_info.value.details["status_code"] == 409
    assert exc_info.value.details["mailing_uuid"] == "mailing-uuid-1"


@pytest.mark.asyncio
async def test_target_core_retry_fails_fast_for_non_retryable_http_400(monkeypatch) -> None:
    calls = {"count": 0}
    sleep_calls: list[float] = []

    def _fake_json_request(**kwargs):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        raise HTTPError(kwargs["url"], 400, "bad request", {}, io.BytesIO(b'{"detail":"invalid"}'))

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(service, "_json_request", _fake_json_request)
    monkeypatch.setattr(service.asyncio, "sleep", _fake_sleep)

    with pytest.raises(service.FileAppTipo1ManualPipelineError) as exc_info:
        await service._json_request_with_transient_retry(
            method="PATCH",
            url="http://target-core-api.otima.io/v2/mailings/mailing-uuid-1",
            headers={},
            payload={},
            timeout_seconds=5,
            step="step4_patch_mailing",
            failure_message="PATCH do mailing falhou",
        )

    assert calls["count"] == 1
    assert sleep_calls == []
    assert exc_info.value.step == "step4_patch_mailing"
    assert exc_info.value.details["status_code"] == 400
    assert exc_info.value.details["attempts"] == 1


def test_build_file_event_mailing_identity_uses_slug_and_suffix() -> None:
    identity = service.build_file_event_mailing_identity(file_name="Mailing 7_CPFs com telefones 024.csv")
    assert identity.name == "mailing_7_cpfs_com_telefones_024"
    assert identity.description == "Carga via evento de cópia de arquivo no SFTP - mailing_7_cpfs_com_telefones_024"

    second = service.build_file_event_mailing_identity(
        file_name="Mailing 7_CPFs com telefones 024.csv",
        existing_names=[
            "mailing_7_cpfs_com_telefones_024",
            "mailing_7_cpfs_com_telefones_024_001",
        ],
    )
    assert second.name == "mailing_7_cpfs_com_telefones_024_002"


@pytest.mark.asyncio
async def test_run_tipo1_manual_pipeline_retries_download_before_success(monkeypatch) -> None:
    attempts = {"count": 0}
    sleep_calls: list[float] = []

    def _fake_download(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise URLError("scan pending")
        return b"CPF,telefone\n20000000000,5521975670000\n"

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    def _fake_multipart_request(*, url, headers, upload, timeout_seconds):  # type: ignore[no-untyped-def]
        return 200, '{"data":{"mailing_id":"mailing-uuid-1"}}'

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"719cbdca-ec3c-4213-9112-96d9a53cb68a"}]}'
        if method == "PATCH" and url.endswith("/v2/mailings/mailing-uuid-1"):
            return 200, '{"data":{"ok":true}}'
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"put_suggestion":{"mappings":[{"id":99,"contact_system_field_id":"94949070-f4b5-48a7-a0a0-5179aa04451a","is_ignored":false,"dialer_label":null,"field_type":"text"}]}}}'
        if method == "PUT" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"status":"READY_TO_INGEST"}}'
        if method == "POST" and url.endswith("/v2/mailings/mailing-uuid-1/import"):
            return 200, '{"data":{"task_id":"import-task-1"}}'
        if method == "POST" and url.endswith("/v2/flow/flow-uuid-1/mailings"):
            return 200, '{"data":[{"results":{"linked":["mailing-uuid-1"],"unassigned":[],"errors":{}}}]}'
        raise AssertionError(f"Unexpected call: {method} {url}")

    monkeypatch.setattr(service, "_download_file_bytes", _fake_download)
    monkeypatch.setattr(service.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(service, "_multipart_request", _fake_multipart_request)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    result = await service.run_tipo1_manual_pipeline(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="flow-uuid-1",
        payload={
            "file": {
                "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                "url": "https://sync-core-api.otima.io/files/v1/files/content/file-123",
                "original_name": "mailing.csv",
                "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            }
        },
        mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
        workspace_api_key=None,
    )

    assert result["status"] == "done"
    assert attempts["count"] == 3
    assert sleep_calls == [15.0, 15.0]


@pytest.mark.asyncio
async def test_run_tipo1_manual_pipeline_fails_after_download_retry_exhaustion(monkeypatch) -> None:
    attempts = {"count": 0}
    sleep_calls: list[float] = []

    def _fake_download(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        attempts["count"] += 1
        raise URLError("scan pending")

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(service, "_download_file_bytes", _fake_download)
    monkeypatch.setattr(service.asyncio, "sleep", _fake_sleep)

    with pytest.raises(service.FileAppTipo1ManualPipelineError) as exc_info:
        await service.run_tipo1_manual_pipeline(
            settings=_DummySettings(),
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            flow_uuid="flow-uuid-1",
            payload={
                "file": {
                    "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                    "url": "https://sync-core-api.otima.io/files/v1/files/content/file-123",
                    "original_name": "mailing.csv",
                    "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
                }
            },
            mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
            workspace_api_key=None,
        )

    assert exc_info.value.step == "step1_upload"
    assert exc_info.value.details["attempts"] == 5
    assert exc_info.value.details["retry_interval_seconds"] == 15.0
    assert exc_info.value.details["error_type"] == "URLError"
    assert attempts["count"] == 5
    assert len(sleep_calls) == 4


@pytest.mark.asyncio
async def test_run_tipo1_manual_pipeline_uses_predownloaded_bytes_and_upload_name_override(monkeypatch) -> None:
    multipart_seen: dict[str, object] = {}

    def _fake_download(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        raise AssertionError("download não deve ser chamado quando bytes são fornecidos")

    def _fake_multipart_request(*, url, headers, upload, timeout_seconds):  # type: ignore[no-untyped-def]
        multipart_seen["file_name"] = upload.file_name
        multipart_seen["file_bytes"] = upload.file_bytes
        return 200, '{"data":{"mailing_id":"mailing-uuid-1"}}'

    def _fake_json_request(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        if method == "GET" and url.endswith("/v2/mailings/mapping-templates"):
            return 200, '{"data":[{"id":"719cbdca-ec3c-4213-9112-96d9a53cb68a"}]}'
        if method == "PATCH" and url.endswith("/v2/mailings/mailing-uuid-1"):
            return 200, '{"data":{"ok":true}}'
        if method == "GET" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"put_suggestion":{"mappings":[{"id":10,"contact_system_field_id":"a26cbb26-2126-46f8-907c-757ab6dc2790","is_ignored":false,"dialer_label":null,"field_type":"text"}]}}}'
        if method == "PUT" and url.endswith("/v2/mailings/mailing-uuid-1/field-mappings"):
            return 200, '{"data":{"status":"READY_TO_INGEST"}}'
        if method == "POST" and url.endswith("/v2/mailings/mailing-uuid-1/import"):
            return 200, '{"data":{"task_id":"import-task-1"}}'
        if method == "POST" and url.endswith("/v2/flow/flow-uuid-1/mailings"):
            return 200, '{"data":[{"results":{"linked":["mailing-uuid-1"],"unassigned":[],"errors":{}}}]}'
        raise AssertionError(f"Unexpected call: {method} {url}")

    monkeypatch.setattr(service, "_download_file_bytes", _fake_download)
    monkeypatch.setattr(service, "_multipart_request", _fake_multipart_request)
    monkeypatch.setattr(service, "_json_request", _fake_json_request)

    result = await service.run_tipo1_manual_pipeline(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        flow_uuid="flow-uuid-1",
        payload={
            "file": {
                "id": "2f388d0f-5519-4e30-99ad-de34c96b9a59",
                "url": "https://sync-core-api.otima.io/files/v1/files/content/file-123",
                "original_name": "mailing.csv",
                "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            }
        },
        mapping_template_uuid="719cbdca-ec3c-4213-9112-96d9a53cb68a",
        workspace_api_key=None,
        predownloaded_file_bytes=b"cpf,telefone\n1,2\n",
        upload_file_name_override="mailing_20260622T222200Z.csv",
    )

    assert result["status"] == "done"
    assert multipart_seen["file_name"] == "mailing_20260622T222200Z.csv"
    assert multipart_seen["file_bytes"] == b"cpf,telefone\n1,2\n"
