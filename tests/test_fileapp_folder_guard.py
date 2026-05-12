from __future__ import annotations

from app.services.fileapp_tipo1_service import (
    extract_monitored_folders_from_orchestration_trigger,
    is_file_event_in_monitored_folder,
)


def test_extract_monitored_folders_from_orchestration_trigger() -> None:
    trigger = {
        "folder_paths": ["dev-orch/mailing/demo06", "system/mailings"],
        "metadata": {"watch_folder": "imports/base"},
    }
    result = extract_monitored_folders_from_orchestration_trigger(trigger)
    assert result == {"dev-orch/mailing/demo06", "system/mailings", "imports/base"}


def test_is_file_event_in_monitored_folder_accepts_exact_and_child_path() -> None:
    payload = {
        "file": {
            "id": "f-1",
            "original_name": "x.csv",
            "folder_path": "dev-orch/mailing/demo06/subfolder",
        }
    }
    assert is_file_event_in_monitored_folder(
        payload=payload,
        monitored_folders={"dev-orch/mailing/demo06"},
    )


def test_is_file_event_in_monitored_folder_rejects_unmonitored_folder() -> None:
    payload = {
        "file": {
            "id": "f-2",
            "original_name": "x.csv",
            "folder_path": "system/mailings",
        }
    }
    assert not is_file_event_in_monitored_folder(
        payload=payload,
        monitored_folders={"dev-orch/mailing/demo06"},
    )

