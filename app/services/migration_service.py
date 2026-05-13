from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workspace import workspace_schema_from_uuid
from app.services.workspace_service import list_completed_workspaces


@dataclass(frozen=True)
class WorkspaceMigrationResult:
    workspace_uuid: str
    schema: str
    applied_versions: list[str]
    skipped_versions: list[str]


MIGRATIONS: list[tuple[str, str]] = [
    ("0001_create_orch_sessions", "sql/001_create_orch_sessions.sql"),
    ("0002_add_entity_origin_app", "sql/002_add_entity_origin_app.sql"),
    ("0003_create_orch_sessions_alarms", "sql/003_create_orch_sessions_alarms.sql"),
    ("0004_create_orch_session_metrics", "sql/004_create_orch_session_metrics.sql"),
    ("0005_update_orch_session_metrics_for_async", "sql/005_update_orch_session_metrics_for_async.sql"),
    ("0006_create_orch_generate_file_tables", "sql/006_create_orch_generate_file_tables.sql"),
    ("0007_add_assigned_fields_to_orch_sessions", "sql/007_add_assigned_fields_to_orch_sessions.sql"),
    ("0008_fix_assigned_fields_to_timestamps", "sql/008_fix_assigned_fields_to_timestamps.sql"),
    ("0009_add_orch_sessions_flow_entity_index", "sql/009_add_orch_sessions_flow_entity_index.sql"),
    ("0010_create_orch_discarded_events", "sql/010_create_orch_discarded_events.sql"),
    ("0011_allow_stopped_after_unassign_state", "sql/011_allow_stopped_after_unassign_state.sql"),
    ("0012_create_orch_channel_events", "sql/012_create_orch_channel_events.sql"),
    ("0013_fix_orch_channel_events_dedupe_scope", "sql/013_fix_orch_channel_events_dedupe_scope.sql"),
]


def _split_sql_statements(sql_text: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_single_quote = False

    for raw_line in sql_text.splitlines():
        line = raw_line
        if not in_single_quote:
            line = line.split("--", 1)[0]
        if not line.strip():
            continue
        for char in line:
            if char == "'":
                in_single_quote = not in_single_quote
            if char == ";" and not in_single_quote:
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
            else:
                current.append(char)
        current.append("\n")

    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _workspace_tablespace_from_schema(schema: str) -> str:
    raw = str(schema or "").strip()
    if not raw:
        raise ValueError("Schema vazio para resolver tablespace.")
    if raw.startswith("ws_") and len(raw) > 3:
        return raw[3:]
    return raw


def _render_migration_sql(sql_text: str, *, schema: str) -> str:
    tablespace = _workspace_tablespace_from_schema(schema)
    return sql_text.replace("__WORKSPACE_TABLESPACE__", tablespace)


async def _ensure_orch_version_table(
    db_session: AsyncSession,
    *,
    schema: str,
) -> None:
    safe_schema = schema.replace('"', '""')
    await db_session.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS "{safe_schema}".orch_alembic_version (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )


async def _fetch_applied_versions(
    db_session: AsyncSession,
    *,
    schema: str,
) -> set[str]:
    safe_schema = schema.replace('"', '""')
    result = await db_session.execute(
        text(f'SELECT version FROM "{safe_schema}".orch_alembic_version')
    )
    return {str(row[0]) for row in result.fetchall()}


async def _record_applied_version(
    db_session: AsyncSession,
    *,
    schema: str,
    version: str,
) -> None:
    safe_schema = schema.replace('"', '""')
    await db_session.execute(
        text(
            f"""
            INSERT INTO "{safe_schema}".orch_alembic_version (version, applied_at)
            VALUES (:version, NOW())
            ON CONFLICT (version) DO NOTHING
            """
        ),
        {"version": version},
    )


async def _run_migration_file(
    db_session: AsyncSession,
    *,
    schema: str,
    migration_path: str,
) -> None:
    safe_schema = schema.replace('"', '""')
    sql_text = Path(migration_path).read_text(encoding="utf-8")
    sql_text = _render_migration_sql(sql_text, schema=schema)
    statements = _split_sql_statements(sql_text)
    await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
    for statement in statements:
        await db_session.execute(text(statement))


async def migrate_workspace(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
) -> WorkspaceMigrationResult:
    schema = workspace_schema_from_uuid(workspace_uuid)
    if db_session.in_transaction():
        await db_session.commit()

    async with db_session.begin():
        await _ensure_orch_version_table(db_session, schema=schema)
        applied = await _fetch_applied_versions(db_session, schema=schema)
        applied_versions: list[str] = []
        skipped_versions: list[str] = []

        for version, path in MIGRATIONS:
            if version in applied:
                skipped_versions.append(version)
                continue
            await _run_migration_file(
                db_session,
                schema=schema,
                migration_path=path,
            )
            await _record_applied_version(
                db_session,
                schema=schema,
                version=version,
            )
            applied_versions.append(version)

    return WorkspaceMigrationResult(
        workspace_uuid=workspace_uuid,
        schema=schema,
        applied_versions=applied_versions,
        skipped_versions=skipped_versions,
    )


async def migrate_all_active_workspaces(
    db_session: AsyncSession,
) -> list[WorkspaceMigrationResult]:
    rows = await list_completed_workspaces(db_session)
    results: list[WorkspaceMigrationResult] = []
    for row in rows:
        workspace_uuid = str(row["workspace_uuid"])
        results.append(await migrate_workspace(db_session, workspace_uuid=workspace_uuid))
    return results
