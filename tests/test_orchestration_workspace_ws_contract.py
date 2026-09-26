from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path


FIXTURE_PATH = Path(
    "docs/project-knowledge/ORCHESTRATION_WORKSPACE_WS_EXAMPLES.json"
)
CANONICAL_STAGES = [
    "entrada",
    "identificacao",
    "qualificacao",
    "abordagem",
    "proposta",
    "decisao",
    "desfecho",
]


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_workspace_ws_fixture_uses_workspace_snapshot_contract() -> None:
    fixture = _fixture()
    snapshot = fixture["snapshot"]

    assert snapshot["type"] == "orchestration_workspace_snapshot"
    assert snapshot["meta"]["version"] == "1.0"
    assert snapshot["meta"]["snapshot_sequence"] > 0
    assert snapshot["meta"]["source_application"] == "orch"
    assert "target_application" not in snapshot
    assert "broadcast:dashboard" not in json.dumps(snapshot)


def test_workspace_ws_fixture_always_exposes_seven_canonical_stages() -> None:
    payload = _fixture()["snapshot"]["payload"]

    catalog_stage_ids = [item["id"] for item in payload["catalog"]["stages"]]
    funnel_stage_ids = [
        item["stage_id"] for item in payload["workspace_view"]["funnel"]
    ]

    assert catalog_stage_ids == CANONICAL_STAGES
    assert funnel_stage_ids == CANONICAL_STAGES
    assert [item["ordinal"] for item in payload["catalog"]["stages"]] == list(
        range(1, 8)
    )


def test_workspace_ws_fixture_contains_no_session_or_person_rows() -> None:
    payload = _fixture()["snapshot"]["payload"]
    serialized = json.dumps(payload).lower()

    assert "session_uuid" not in serialized
    assert "person_uuid" not in serialized
    assert "entity_address" not in serialized
    assert "phone" not in serialized
    assert "message_body" not in serialized
    assert "callback_payload" not in serialized


def test_workspace_ws_fixture_keeps_detailed_filter_out_of_broadcast() -> None:
    fixture = _fixture()
    snapshot_payload = fixture["snapshot"]["payload"]
    view_request = fixture["view_request"]
    view = fixture["view"]

    assert "filter_cube" not in snapshot_payload
    assert "flow_views" not in snapshot_payload
    assert snapshot_payload["flow_summaries"]
    assert view_request["type"] == "orchestration_view_request"
    assert view["type"] == "orchestration_workspace_view"
    assert view["request_id"] == view_request["request_id"]
    assert view["filters"] == view_request["filters"]
    assert view["snapshot_sequence"] == fixture["snapshot"]["meta"]["snapshot_sequence"]
    assert all(item["grain"] == "actions" for item in view["view"]["channels"])


def test_workspace_ws_authentication_does_not_select_workspace() -> None:
    fixture = _fixture()
    authenticate = fixture["authenticate"]
    ticket_response = fixture["ticket_response"]

    assert authenticate == {
        "type": "authenticate",
        "version": "1.0",
        "ticket": "opaque-single-use-ticket",
    }
    assert "workspace_uuid" not in authenticate
    assert ticket_response == {
        "data": {
            "ticket": "opaque-single-use-ticket",
            "expires_in_seconds": 30,
        }
    }
    assert "workspace_uuid" not in ticket_response["data"]


def test_workspace_ws_example_frames_respect_initial_payload_budget() -> None:
    fixture = _fixture()

    for frame_name in ("snapshot", "view"):
        encoded = json.dumps(
            fixture[frame_name], separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        assert len(encoded) <= 256 * 1024


def test_workspace_broadcast_remains_compact_with_one_hundred_flows() -> None:
    snapshot = deepcopy(_fixture()["snapshot"])
    payload = snapshot["payload"]
    source_flow = payload["catalog"]["flows"][0]
    source_revision = payload["catalog"]["revisions"][0]
    source_summary = payload["flow_summaries"][0]

    payload["catalog"]["flows"] = []
    payload["catalog"]["revisions"] = []
    payload["flow_summaries"] = []
    for index in range(100):
        suffix = f"{index + 1:012d}"
        flow_uuid = f"f77b70f0-849b-4d11-9ccc-{suffix}"
        revision_uuid = f"10000000-0000-0000-0000-{suffix}"

        flow = deepcopy(source_flow)
        flow["uuid"] = flow_uuid
        flow["name"] = f"Fluxo sintetico {index + 1:03d}"
        payload["catalog"]["flows"].append(flow)

        revision = deepcopy(source_revision)
        revision["uuid"] = revision_uuid
        revision["flow_uuid"] = flow_uuid
        payload["catalog"]["revisions"].append(revision)

        summary = deepcopy(source_summary)
        summary["flow"] = deepcopy(flow)
        payload["flow_summaries"].append(summary)

    encoded = json.dumps(
        snapshot, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    assert len(encoded) <= 256 * 1024
