from __future__ import annotations

from app.tasks.fileapp_ingest_tasks import ingest_fileapp_event_task


class _DummyTask:
    id = "task-123"


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
    assert captured["queue"] == "orch_fileapp_source_list_ingest"
    assert captured["routing_key"] == "orch_fileapp_source_list_ingest"
