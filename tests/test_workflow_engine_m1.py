from __future__ import annotations

from app.services.workflow_engine import (
    build_adjacency,
    build_bootstrap,
    resolve_next_card_uuid,
    resolve_next_card_uuid_by_branch,
    resolve_start_card_uuid,
)


def test_resolve_start_card_by_indegree_from_branches() -> None:
    definition = {
        "components": [
            {"uuid": "card-a"},
            {"uuid": "card-b"},
            {"uuid": "card-c"},
        ],
        "branches": [
            {"from": "card-a", "to": "card-b"},
            {"from": "card-b", "to": "card-c"},
        ],
    }

    assert resolve_start_card_uuid(definition) == "card-a"


def test_resolve_start_card_with_explicit_entrypoint() -> None:
    definition = {
        "start_card_uuid": "card-z",
        "components": [{"uuid": "card-a"}, {"uuid": "card-z"}],
    }

    assert resolve_start_card_uuid(definition) == "card-z"


def test_resolve_next_card_from_adjacency() -> None:
    definition = {
        "components": [{"uuid": "1"}, {"uuid": "2"}, {"uuid": "3"}],
        "branches": {
            "1": "2",
            "2": [{"to": "3"}],
        },
    }

    adjacency = build_adjacency(definition)
    assert adjacency["1"] == ["2"]
    assert adjacency["2"] == ["3"]
    assert resolve_next_card_uuid(definition, "1") == "2"
    assert resolve_next_card_uuid(definition, "2") == "3"
    assert resolve_next_card_uuid(definition, "3") is None


def test_bootstrap_points_to_first_card() -> None:
    definition = {
        "components": [{"uuid": "x-1"}, {"uuid": "x-2"}],
        "branches": [{"from": "x-1", "to": "x-2"}],
    }

    bootstrap = build_bootstrap(definition)
    assert bootstrap.start_card_uuid == "x-1"
    assert bootstrap.next_card_uuid == "x-1"


def test_resolve_next_card_by_branch_label() -> None:
    definition = {
        "components": [{"uuid": "card-1"}, {"uuid": "card-2"}, {"uuid": "card-3"}],
        "branches": [
            {"from": "card-1", "to": "card-2", "label": "true"},
            {"from": "card-1", "to": "card-3", "label": "false"},
        ],
    }

    assert (
        resolve_next_card_uuid_by_branch(definition, current_card_uuid="card-1", branch_label="true")
        == "card-2"
    )
    assert (
        resolve_next_card_uuid_by_branch(definition, current_card_uuid="card-1", branch_label="false")
        == "card-3"
    )


def test_ref_id_is_resolved_to_component_uuid_in_start_and_edges() -> None:
    definition = {
        "trigger_start_by_ref_id": "set-1",
        "components": [
            {"uuid": "11111111-1111-1111-1111-111111111111", "ref_id": "set-1"},
            {"uuid": "22222222-2222-2222-2222-222222222222", "ref_id": "code-1"},
        ],
        "branches": [
            {"from": "set-1", "to": "code-1", "branch": "success"},
        ],
    }

    start = resolve_start_card_uuid(definition)
    next_card = resolve_next_card_uuid(definition, start)

    assert start == "11111111-1111-1111-1111-111111111111"
    assert next_card == "22222222-2222-2222-2222-222222222222"
