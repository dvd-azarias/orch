from __future__ import annotations

from unittest.mock import Mock

import app.tasks.fileapp_ingest_tasks as tasks


def test_post_process_lock_uses_redis_key() -> None:
    redis_client = Mock()
    redis_client.set.return_value = True

    acquired = tasks._try_acquire_fileapp_post_process_lock(
        redis_client,
        workspace_uuid="workspace-1",
        source_list_id=123,
        cooldown_seconds=60,
    )

    assert acquired is True
    redis_client.set.assert_called_once_with(
        "orch:fileapp:post-process:workspace-1:123",
        "1",
        ex=60,
        nx=True,
    )


def test_rescue_flow_state_allows_atomic_retry_after_failure() -> None:
    redis_client = Mock()
    redis_client.eval.return_value = 1

    acquired = tasks._try_mark_fileapp_entrada_rescue_flow_in_flight(
        redis_client,
        workspace_uuid="workspace-1",
        flow_uuid="flow-1",
        file_id="file-1",
        ttl_seconds=60,
    )

    assert acquired is True
    script, key_count, key, ttl_seconds = redis_client.eval.call_args.args
    assert "current == 'failed'" in script
    assert key_count == 1
    assert key == "orch:fileapp:entrada-rescue:flow-state:workspace-1:flow-1:file-1"
    assert ttl_seconds == 60
