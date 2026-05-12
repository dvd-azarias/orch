from __future__ import annotations

import app.services.migration_service as migration_service


def test_workspace_tablespace_from_ws_schema() -> None:
    value = migration_service._workspace_tablespace_from_schema("ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60")
    assert value == "ba7eb0ec-e565-447c-8c11-8f870cf72a60"


def test_render_migration_sql_replaces_workspace_tablespace_placeholder() -> None:
    rendered = migration_service._render_migration_sql(
        'CREATE INDEX x ON orch_sessions (flow_uuid, entity) TABLESPACE "__WORKSPACE_TABLESPACE__";',
        schema="ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60",
    )
    assert '"ba7eb0ec-e565-447c-8c11-8f870cf72a60"' in rendered
    assert "__WORKSPACE_TABLESPACE__" not in rendered
