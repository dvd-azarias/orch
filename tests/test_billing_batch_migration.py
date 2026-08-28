from __future__ import annotations

from pathlib import Path

from app.services.migration_service import MIGRATIONS


def test_billing_batch_migration_is_registered_after_legacy() -> None:
    versions = [version for version, _path in MIGRATIONS]
    assert "0020_create_orch_billing_usage_snapshots" in versions
    assert versions[-1] == "0022_create_orch_billing_batch_tables"


def test_billing_batch_migration_has_required_tables_constraints_and_indexes() -> None:
    sql = Path("sql/022_create_orch_billing_batch_tables.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS orch_billing_events" in sql
    assert "CREATE TABLE IF NOT EXISTS orch_billing_snapshots" in sql
    assert "CREATE TABLE IF NOT EXISTS orch_billing_reprocess_requests" in sql
    assert "UNIQUE (workspace_uuid, source_session_uuid, billing_period, metric_code)" in sql
    assert "WHERE status IN ('pending', 'failed')" in sql
    assert "WHERE status = 'processing'" in sql
    assert "UNIQUE (workspace_uuid, idempotency_key)" in sql
    assert "jsonb_typeof(payload) = 'object'" in sql
    assert "last_enqueued_at TIMESTAMPTZ" in sql
    assert "cursor_session_id BIGINT" in sql
    assert "idx_orch_sessions_billing_created_at" in sql
