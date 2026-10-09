from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.services.metrics_event_outbox_service import (
    build_metrics_event,
    metrics_events_enabled_for_workspace,
    metrics_retry_delay_seconds,
    publish_metrics_events_batch,
)


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"


def test_event_builder_forces_orchestration_flow_type() -> None:
    event = build_metrics_event(
        workspace_uuid=WORKSPACE_UUID,
        event_type="interaction.session.started.v1",
        context={"flow_type": "attendance", "interaction_id": str(uuid4())},
        payload={"direction": "OUTBOUND"},
    )
    assert event["workspace_id"] == WORKSPACE_UUID
    assert event["source"] == "orch"
    assert event["context"]["flow_type"] == "orchestration"


def test_workspace_gate_is_fail_closed() -> None:
    settings = get_settings()
    disabled = replace(
        settings,
        orch_metrics_events_enabled=False,
        orch_metrics_events_workspace_allowlist=(WORKSPACE_UUID,),
    )
    assert not metrics_events_enabled_for_workspace(
        WORKSPACE_UUID,
        settings=disabled,
    )

    enabled = replace(
        settings,
        orch_metrics_events_enabled=True,
        orch_metrics_events_workspace_allowlist=(WORKSPACE_UUID,),
    )
    assert metrics_events_enabled_for_workspace(
        WORKSPACE_UUID,
        settings=enabled,
    )
    assert not metrics_events_enabled_for_workspace(
        str(uuid4()),
        settings=enabled,
    )

    global_enabled = replace(
        settings,
        orch_metrics_events_enabled=True,
        orch_metrics_events_allow_all_workspaces=True,
        orch_metrics_events_workspace_allowlist=(),
    )
    assert metrics_events_enabled_for_workspace(
        str(uuid4()),
        settings=global_enabled,
    )
    assert not metrics_events_enabled_for_workspace(
        "not-a-uuid",
        settings=global_enabled,
    )


def test_retry_backoff_is_exponential_and_bounded() -> None:
    assert metrics_retry_delay_seconds(
        attempt=1,
        initial_seconds=5,
        maximum_seconds=900,
    ) == 5
    assert metrics_retry_delay_seconds(
        attempt=4,
        initial_seconds=5,
        maximum_seconds=900,
    ) == 40
    assert metrics_retry_delay_seconds(
        attempt=100,
        initial_seconds=5,
        maximum_seconds=900,
    ) == 900


def test_publish_uses_documented_ingest_path_and_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Response:
        status = 202

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def read(_limit: int) -> bytes:
            return b'{"accepted":1}'

    def _urlopen(request: object, *, timeout: float) -> _Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(
        "app.services.metrics_event_outbox_service.urlopen",
        _urlopen,
    )
    settings = replace(
        get_settings(),
        orch_metrics_api_base_url="https://metrics.example.test/api",
        orch_metrics_api_key="metrics-test-key",
        orch_metrics_events_http_timeout_seconds=3.0,
    )
    event = build_metrics_event(
        workspace_uuid=WORKSPACE_UUID,
        event_type="interaction.session.started.v1",
        context={"interaction_id": str(uuid4())},
        payload={"direction": "OUTBOUND"},
    )
    claim_token = str(uuid4())

    result = publish_metrics_events_batch(
        events=[event],
        claim_token=claim_token,
        settings=settings,
    )

    request = captured["request"]
    assert request.full_url == "https://metrics.example.test/api/v1/events/ingest"
    assert request.get_header("X-api-key") == "metrics-test-key"
    assert request.get_header("X-idempotency-key") == claim_token
    assert json.loads(request.data) == {"events": [event]}
    assert captured["timeout"] == 3.0
    assert result.success is True
    assert result.status_code == 202
