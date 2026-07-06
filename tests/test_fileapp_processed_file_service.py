from __future__ import annotations

from urllib.error import HTTPError

import pytest

import app.services.fileapp_processed_file_service as service


class _DummySettings:
    arquivos_client_id = "client-id"
    arquivos_client_secret = "client-secret"
    arquivos_base_url = "https://sync-core-api.otima.io/files/v1"
    sync_ws_timeout_seconds = 5.0


def _http_error(code: int) -> HTTPError:
    return HTTPError(url="https://example.test", code=code, msg="error", hdrs=None, fp=None)


@pytest.mark.asyncio
async def test_move_processed_file_to_processados_with_folder_path_move(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def _fake_request_json(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        calls.append((method, url, payload))
        if method == "POST" and url.endswith("/files/folders"):
            raise _http_error(409)
        if method == "PATCH" and payload == {"folder_path": "mailings/AeC/tim-portabilidade/processados"}:
            return 200, "{}"
        raise AssertionError(f"Unexpected call: {method} {url} payload={payload}")

    monkeypatch.setattr(service, "_request_json", _fake_request_json)

    result = await service.move_processed_file_to_processados(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        payload={
            "file": {
                "id": "db1af3c8-fb8c-42dc-8e0b-68c274d5cf59",
                "folder_path": "mailings/AeC/tim-portabilidade",
                "original_name": "contato_deivid_tim_silver.csv",
            }
        },
    )

    assert result["status"] == "done"
    assert result["target_folder"] == "mailings/AeC/tim-portabilidade/processados"
    assert result["target_name"] == "contato_deivid_tim_silver.csv"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_move_processed_file_to_processados_retries_transient_500_on_move(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    sleep_calls: list[float] = []
    def _fake_request_json(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        calls.append((method, url, payload))
        if method == "POST" and url.endswith("/files/folders"):
            return 201, "{}"
        if method == "PATCH" and payload == {"folder_path": "mailings/AeC/tim-portabilidade/processados"}:
            patch_calls = [item for item in calls if item[0] == "PATCH"]
            if len(patch_calls) == 1:
                raise _http_error(500)
            return 200, "{}"
        raise AssertionError(f"Unexpected call: {method} {url} payload={payload}")

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(service, "_request_json", _fake_request_json)
    monkeypatch.setattr(service.asyncio, "sleep", _fake_sleep)

    result = await service.move_processed_file_to_processados(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        payload={
            "file": {
                "id": "db1af3c8-fb8c-42dc-8e0b-68c274d5cf59",
                "folder_path": "mailings/AeC/tim-portabilidade",
                "original_name": "contato_deivid_tim_silver.csv",
            }
        },
    )

    assert result["status"] == "done"
    assert sleep_calls == [15.0]


@pytest.mark.asyncio
async def test_move_processed_file_to_processados_retries_transient_404_on_move(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    sleep_calls: list[float] = []
    def _fake_request_json(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        calls.append((method, url, payload))
        if method == "POST" and url.endswith("/files/folders"):
            return 201, "{}"
        if method == "PATCH" and payload == {"folder_path": "mailings/AeC/tim-portabilidade/processados"}:
            patch_calls = [item for item in calls if item[0] == "PATCH"]
            if len(patch_calls) == 1:
                raise _http_error(404)
            return 200, "{}"
        raise AssertionError(f"Unexpected call: {method} {url} payload={payload}")

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(service, "_request_json", _fake_request_json)
    monkeypatch.setattr(service.asyncio, "sleep", _fake_sleep)

    result = await service.move_processed_file_to_processados(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        payload={
            "file": {
                "id": "db1af3c8-fb8c-42dc-8e0b-68c274d5cf59",
                "folder_path": "mailings/AeC/tim-portabilidade",
                "original_name": "contato_deivid_tim_silver.csv",
            }
        },
    )

    assert result["status"] == "done"
    assert sleep_calls == [15.0]


@pytest.mark.asyncio
async def test_move_processed_file_to_processados_fallback_reupload_when_404_persists(monkeypatch) -> None:
    now_stub = "20260622T204500Z"
    sleep_calls: list[float] = []
    patch_calls = {"count": 0}
    multipart_calls: list[tuple[str, str]] = []

    def _fake_request_json(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        if method == "POST" and url.endswith("/files/folders"):
            return 201, "{}"
        if method == "PATCH":
            patch_calls["count"] += 1
            raise _http_error(404)
        raise AssertionError(f"Unexpected call: {method} {url} payload={payload}")

    def _fake_request_bytes(*, url, headers, timeout_seconds):  # type: ignore[no-untyped-def]
        assert url == "https://storage.example/file.csv"
        assert headers["x-client-id"] == "sync-client-id"
        return b"n;f\nx;y\n"

    def _fake_request_multipart(*, url, headers, file_name, file_bytes, folder_path, timeout_seconds):  # type: ignore[no-untyped-def]
        multipart_calls.append((url, file_name))
        assert folder_path == "mailings/AeC/tim-portabilidade/processados"
        assert file_bytes == b"n;f\nx;y\n"
        return 201, '{"data":{"id":"new-file-id"}}'

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    class _Now:
        @staticmethod
        def now(tz=None):  # type: ignore[no-untyped-def]
            class _DT:
                @staticmethod
                def strftime(fmt: str) -> str:
                    assert fmt == "%Y%m%dT%H%M%SZ"
                    return now_stub

            return _DT()

    class _FallbackSettings(_DummySettings):
        sync_ws_client_id = "sync-client-id"
        sync_ws_client_secret = "sync-client-secret"

    monkeypatch.setattr(service, "_request_json", _fake_request_json)
    monkeypatch.setattr(service, "_request_bytes", _fake_request_bytes)
    monkeypatch.setattr(service, "_request_multipart", _fake_request_multipart)
    monkeypatch.setattr(service.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(service, "datetime", _Now)

    result = await service.move_processed_file_to_processados(
        settings=_FallbackSettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        payload={
            "file": {
                "id": "db1af3c8-fb8c-42dc-8e0b-68c274d5cf59",
                "folder_path": "mailings/AeC/tim-portabilidade",
                "original_name": "contato_deivid_tim_silver.csv",
                "url": "https://storage.example/file.csv",
            }
        },
    )

    assert result["status"] == "done"
    assert result["target_name"] == f"contato_deivid_tim_silver_{now_stub}.csv"
    assert result["fallback_reupload"]["status"] == "done"
    assert multipart_calls[0][0].endswith("/files/upload")
    assert patch_calls["count"] == 5
    assert sleep_calls == [15.0, 15.0, 15.0, 15.0]


@pytest.mark.asyncio
async def test_move_processed_file_to_processados_skips_when_already_processed(monkeypatch) -> None:
    called = {"count": 0}

    def _fake_request_json(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        called["count"] += 1
        return 200, "{}"

    monkeypatch.setattr(service, "_request_json", _fake_request_json)

    result = await service.move_processed_file_to_processados(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        payload={
            "file": {
                "id": "db1af3c8-fb8c-42dc-8e0b-68c274d5cf59",
                "folder_path": "mailings/AeC/tim-portabilidade/processados",
                "original_name": "contato_deivid_tim_silver_20260622T204500Z.csv",
            }
        },
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "already_in_processados"
    assert called["count"] == 0


@pytest.mark.asyncio
async def test_move_processed_file_to_processados_raises_when_move_fails(monkeypatch) -> None:
    sleep_calls: list[float] = []

    def _fake_request_json(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        if method == "POST":
            return 201, "{}"
        if method == "PATCH" and "folder_path" in payload:
            raise _http_error(500)
        return 200, "{}"

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(service, "_request_json", _fake_request_json)
    monkeypatch.setattr(service.asyncio, "sleep", _fake_sleep)

    with pytest.raises(service.FileAppProcessedFileError) as exc_info:
        await service.move_processed_file_to_processados(
            settings=_DummySettings(),
            workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
            payload={
                "file": {
                    "id": "db1af3c8-fb8c-42dc-8e0b-68c274d5cf59",
                    "folder_path": "mailings/AeC/tim-portabilidade",
                    "original_name": "contato_deivid_tim_silver.csv",
                }
            },
        )

    assert exc_info.value.code == "move_file_to_falha_failed"
    assert len(sleep_calls) == 8


@pytest.mark.asyncio
async def test_move_processed_file_to_processados_falls_back_to_falha_folder(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def _fake_request_json(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        calls.append((method, url, payload))
        if method == "POST" and url.endswith("/files/folders"):
            return 201, "{}"
        if method == "PATCH" and payload.get("folder_path") == "mailings/AeC/tim-portabilidade/processados":
            raise _http_error(500)
        if method == "PATCH" and payload.get("folder_path") == "mailings/AeC/tim-portabilidade/falha":
            return 200, "{}"
        raise AssertionError(f"Unexpected call: {method} {url} payload={payload}")

    monkeypatch.setattr(service, "_request_json", _fake_request_json)

    result = await service.move_processed_file_to_processados(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        payload={
            "file": {
                "id": "db1af3c8-fb8c-42dc-8e0b-68c274d5cf59",
                "folder_path": "mailings/AeC/tim-portabilidade",
                "original_name": "contato_deivid_tim_silver.csv",
            }
        },
    )

    assert result["status"] == "done"
    assert result["target_folder"] == "mailings/AeC/tim-portabilidade/falha"
    assert result["quarantine_folder"] == "falha"
    assert result["processados_error"]["code"] == "move_file_to_processados_failed"


def test_build_rename_candidate_avoids_double_timestamp() -> None:
    assert (
        service._build_rename_candidate(
            "contato_deivid_tim_black_20260622T192020Z.csv",
            timestamp="20260622T192022Z",
            index=0,
        )
        == "contato_deivid_tim_black_20260622T192020Z.csv"
    )
    assert (
        service._build_rename_candidate(
            "contato_deivid_tim_black_20260622T192020Z.csv",
            timestamp="20260622T192022Z",
            index=1,
        )
        == "contato_deivid_tim_black_20260622T192020Z_001.csv"
    )


@pytest.mark.asyncio
async def test_quarantine_file_to_falha_moves_directly(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def _fake_request_json(*, method, url, headers, payload, timeout_seconds):  # type: ignore[no-untyped-def]
        calls.append((method, url, payload))
        if method == "POST" and url.endswith("/files/folders"):
            return 201, "{}"
        if method == "PATCH" and payload == {"folder_path": "mailings/AeC/tim-portabilidade/falha"}:
            return 200, "{}"
        raise AssertionError(f"Unexpected call: {method} {url} payload={payload}")

    monkeypatch.setattr(service, "_request_json", _fake_request_json)

    result = await service.quarantine_file_to_falha(
        settings=_DummySettings(),
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        payload={
            "file": {
                "id": "db1af3c8-fb8c-42dc-8e0b-68c274d5cf59",
                "folder_path": "mailings/AeC/tim-portabilidade",
                "original_name": "contato_deivid_tim_silver.csv",
            }
        },
    )

    assert result["status"] == "done"
    assert result["target_folder"] == "mailings/AeC/tim-portabilidade/falha"
    assert len(calls) == 2
