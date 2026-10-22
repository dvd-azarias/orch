from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from app.services import workflow_m2_service as workflow_m2


@pytest.mark.asyncio
async def test_emit_metrics_persists_whatsapp_status_metric_in_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_uuid = str(uuid4())
    flow_uuid = str(uuid4())
    session_uuid = str(uuid4())
    received: list[dict] = []

    async def fake_enqueue(_db_session, **kwargs):  # noqa: ANN001
        received.append(kwargs)
        return str(uuid4())

    monkeypatch.setattr(workflow_m2, "enqueue_metrics_event", fake_enqueue)
    runtime_variables = {
        "whatsapp_last_response": {"status": "read", "branch": "read"},
        "variables": {
            "customs": {"metric_value": "1"},
            "system": {},
        },
    }
    component = {
        "ref_id": "emit-whatsapp-read",
        "parameters": {
            "metrics_list": [
                {
                    "metric_id": "whatsapp.read",
                    "metric_type": "counter",
                    "value": "{{customs.metric_value}}",
                }
            ]
        },
    }

    await workflow_m2._run_emit_metrics(
        db_session=object(),
        workspace_uuid=workspace_uuid,
        flow_uuid=flow_uuid,
        session_uuid=session_uuid,
        revision_id=str(uuid4()),
        component=component,
        runtime_variables=runtime_variables,
    )

    assert len(received) == 1
    event = received[0]
    assert event["event_type"] == "metric.custom.emitted.v1"
    assert event["context"] == {
        "flow_id": flow_uuid,
        "interaction_id": session_uuid,
        "channel": "WHATSAPP",
        "agent_id": f"WHATSAPP-{hashlib.sha1(session_uuid.encode('utf-8')).hexdigest()[:8].upper()}",
        "message_status": "read",
    }
    assert event["payload"] == {
        "metric_id": "whatsapp.read",
        "metric_type": "counter",
        "value": 1.0,
        "operation": "increment",
    }
    assert event["idempotency_key"].startswith(f"emit_metrics:{session_uuid}:")
    assert runtime_variables["variables"]["customs"]["emit_metrics"]["last"]["enqueued"] == 1


@pytest.mark.asyncio
async def test_emit_metrics_is_a_safe_noop_when_metrics_are_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def disabled_enqueue(_db_session, **_kwargs):  # noqa: ANN001
        return None

    monkeypatch.setattr(workflow_m2, "enqueue_metrics_event", disabled_enqueue)
    runtime_variables = {"variables": {"customs": {}, "system": {}}}

    await workflow_m2._run_emit_metrics(
        db_session=object(),
        workspace_uuid=str(uuid4()),
        flow_uuid=str(uuid4()),
        session_uuid=str(uuid4()),
        revision_id=None,
        component={
            "parameters": {
                "metrics_list": [
                    {"metric_id": "messages.sent", "metric_type": "counter", "value": 1}
                ]
            }
        },
        runtime_variables=runtime_variables,
    )

    assert runtime_variables["emit_metrics_last_result"]["enqueued"] == 0
    assert runtime_variables["emit_metrics_last_result"]["skipped"] == 1
