from __future__ import annotations

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
