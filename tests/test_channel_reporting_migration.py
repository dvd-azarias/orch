from __future__ import annotations

from pathlib import Path

from app.services.migration_service import MIGRATIONS


def test_channel_reporting_migration_is_registered_last() -> None:
    versions = [version for version, _path in MIGRATIONS]

    assert versions[-1] == "0023_create_orch_channel_reporting"


def test_channel_reporting_migration_is_future_only_and_starts_pending() -> None:
    sql = Path("sql/023_create_orch_channel_reporting.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS orch_channel_reporting_state" in sql
    assert "CREATE TABLE IF NOT EXISTS orch_channel_actions" in sql
    assert "ADD COLUMN IF NOT EXISTS action_id" in sql
    assert "'pending'" in sql
    assert "coverage_started_at TIMESTAMPTZ" in sql
    assert "flow_revision_id UUID NOT NULL" in sql
    assert "native_outcome_at TIMESTAMPTZ" in sql
    assert "INSERT INTO orch_channel_actions" not in sql
    assert "UPDATE orch_channel_reporting_state" not in sql
    assert "ALTER TABLE orch_sessions" not in sql


def test_channel_reporting_migration_has_action_identity_and_query_indexes() -> None:
    sql = Path("sql/023_create_orch_channel_reporting.sql").read_text(encoding="utf-8")

    assert "UNIQUE (source_kind, source_id)" in sql
    assert "session_uuid," in sql
    assert "flow_revision_id," in sql
    assert "component_ref_id," in sql
    assert "action_sequence" in sql
    assert "idx_orch_channel_actions_flow_requested" in sql
    assert "idx_orch_channel_actions_session_requested" in sql
    assert "idx_orch_channel_actions_channel_requested" in sql
    assert "idx_orch_channel_events_action_timeline" in sql
    assert "WHERE action_id IS NOT NULL" in sql
