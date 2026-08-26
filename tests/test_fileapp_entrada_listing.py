from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

import app.tasks.fileapp_ingest_tasks as tasks


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


@pytest.mark.asyncio
async def test_list_files_paginates_beyond_action_batch(monkeypatch) -> None:
    requests: list[str] = []
    pages = [
        [{"id": f"new-{index}", "original_name": f"new-{index}.csv"} for index in range(100)],
        [{"id": f"old-{index}", "original_name": f"old-{index}.csv"} for index in range(100)],
        [{"id": "oldest", "original_name": "oldest.csv"}],
    ]

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        requests.append(request.full_url)
        return _Response(tasks.json.dumps({"items": pages.pop(0)}).encode())

    monkeypatch.setattr(tasks, "urlopen", fake_urlopen)
    listed_files = await tasks._list_files_in_folder(
        settings=SimpleNamespace(
            arquivos_base_url="https://files.example",
            sync_ws_timeout_seconds=5,
            arquivos_client_id="client",
            arquivos_client_secret="secret",
            target_core_api_bearer_token=None,
        ),
        workspace_uuid="workspace-1",
        folder_path="monitoramento/upload",
        workspace_api_key=None,
        limit=2,
    )

    offsets = [parse_qs(urlparse(url).query)["offset"][0] for url in requests]
    assert offsets == ["0", "100", "200"]
    assert len(listed_files) == 201
    assert listed_files[-1]["id"] == "oldest"


def test_select_oldest_files_applies_batch_after_listing_all_pages() -> None:
    listed_files = [
        {
            "id": "newest",
            "original_name": "newest.csv",
            "folder_path": "monitoramento/upload",
            "created_at": "2026-08-26T11:13:41.624725Z",
        },
        {
            "id": "middle",
            "original_name": "middle.csv",
            "folder_path": "monitoramento/upload",
            "created_at": "2026-08-26T10:00:00Z",
        },
        {
            "id": "oldest",
            "original_name": "oldest.csv",
            "folder_path": "monitoramento/upload",
            "created_at": "2026-08-26T09:50:47.454933Z",
        },
    ]

    selected_files = tasks._select_oldest_files_in_folder(
        listed_files,
        folder_path="monitoramento/upload",
        batch_size=2,
    )

    assert [item["id"] for item in selected_files] == ["oldest", "middle"]
