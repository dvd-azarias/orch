from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

import app.services.workflow_m2_service as workflow_m2_service
import app.services.workflow_runtime_service as workflow_runtime_service
import app.api.v1.orch as orch_api
from app.api.v1.orch import trigger_orch
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.services.workflow_m2_service import execute_workflow_m2_for_session


async def _ensure_flow_tables() -> None:
    schema = get_settings().database_schema
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        flow_table = (
            await db_session.execute(text("SELECT to_regclass(:full_name)"), {"full_name": f"{schema}.flow_v2"})
        ).scalar_one()
        revision_table = (
            await db_session.execute(text("SELECT to_regclass(:full_name)"), {"full_name": f"{schema}.flow_v2_revision"})
        ).scalar_one()
    if flow_table is None or revision_table is None:
        pytest.skip("Tabelas flow_v2/flow_v2_revision não disponíveis no schema configurado.")


async def _insert_flow_with_revision(*, flow_uuid: str, definition: dict) -> tuple[str, str]:
    schema = get_settings().database_schema.replace('"', '""')
    session_factory = get_session_factory()
    revision_uuid = str(uuid4())
    slug = f"test-m2-conc-{flow_uuid[:8]}-{uuid4().hex[:6]}"
    checksum = f"chk-{uuid4().hex}"

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".flow_v2 (id, slug, display_name, status, is_active)
                    VALUES (CAST(:id AS uuid), :slug, :display_name, 'draft', TRUE)
                    """
                ),
                {
                    "id": flow_uuid,
                    "slug": slug,
                    "display_name": f"Flow M2 Conc {flow_uuid[:8]}",
                },
            )
            await db_session.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".flow_v2_revision (
                        id,
                        flow_id,
                        version,
                        definition,
                        checksum,
                        authored_by,
                        published_at,
                        is_draft
                    )
                    VALUES (
                        CAST(:id AS uuid),
                        CAST(:flow_id AS uuid),
                        1,
                        CAST(:definition AS jsonb),
                        :checksum,
                        'tests',
                        NOW(),
                        FALSE
                    )
                    """
                ),
                {
                    "id": revision_uuid,
                    "flow_id": flow_uuid,
                    "definition": json.dumps(definition, ensure_ascii=False),
                    "checksum": checksum,
                },
            )
            await db_session.execute(
                text(
                    f"""
                    UPDATE "{schema}".flow_v2
                    SET current_revision_id = CAST(:revision_id AS uuid), updated_at = NOW()
                    WHERE id = CAST(:flow_id AS uuid)
                    """
                ),
                {
                    "flow_id": flow_uuid,
                    "revision_id": revision_uuid,
                },
            )

    return flow_uuid, revision_uuid


async def _cleanup_flow_with_revision(*, flow_uuid: str, revision_uuid: str) -> None:
    schema = get_settings().database_schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(f'DELETE FROM "{schema}".flow_v2_revision WHERE id = CAST(:id AS uuid)'),
                {"id": revision_uuid},
            )
            await db_session.execute(
                text(f'DELETE FROM "{schema}".flow_v2 WHERE id = CAST(:id AS uuid)'),
                {"id": flow_uuid},
            )


async def _fetch_session_runtime(*, session_id: int) -> dict:
    schema = get_settings().database_schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        row = (
            await db_session.execute(
                text(
                    f"""
                    SELECT
                        state,
                        ended_at,
                        runtime_variables
                    FROM "{schema}".orch_sessions
                    WHERE id = :session_id
                    LIMIT 1
                    """
                ),
                {"session_id": session_id},
            )
        ).mappings().first()
    return dict(row) if row is not None else {}


@pytest.mark.asyncio
async def test_m2_deve_evitar_dupla_execucao_de_card_em_concorrencia(monkeypatch) -> None:
    await _ensure_flow_tables()

    flow_uuid = str(uuid4())
    definition = {
        "trigger_start_by_ref_id": "code-1",
        "components": [
            {
                "uuid": "10101010-1010-1010-1010-101010101010",
                "ref_id": "code-1",
                "component": "code_editor",
                "parameters": {
                    "timeout_ms": 500,
                    "code": """
export default async function main(ctx) {
  const current = Number(ctx.variables.customs.card_counter || 0);
  ctx.variables.customs.card_counter = current + 1;
  return { branch: ctx.branches.success, payload: { counter: ctx.variables.customs.card_counter } };
}
""",
                },
            },
            {
                "uuid": "20202020-2020-2020-2020-202020202020",
                "ref_id": "finish-1",
                "component": "finish_flow",
                "parameters": {},
            },
        ],
        "branches": [
            {"from": "code-1", "to": "finish-1", "branch": "success"},
            {"from": "code-1", "to": "finish-1", "branch": "error"},
        ],
    }

    inserted_flow_uuid, revision_uuid = await _insert_flow_with_revision(flow_uuid=flow_uuid, definition=definition)

    monkeypatch.setattr(workflow_runtime_service, "_read_flag_true", lambda _settings: True)
    monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: False)
    monkeypatch.setattr(orch_api, "get_settings", lambda: SimpleNamespace(celery_enabled=False))

    original_replace = workflow_m2_service.replace_session_workflow_state

    async def _slow_replace(*args, **kwargs):  # noqa: ANN001
        await asyncio.sleep(0.15)
        return await original_replace(*args, **kwargs)

    monkeypatch.setattr(workflow_m2_service, "replace_session_workflow_state", _slow_replace)

    try:
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            created = await trigger_orch(
                flow_uuid=UUID(flow_uuid),
                payload={"external_id": f"conc-{flow_uuid[:8]}"},
                db_session=db_session,
            )

        monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: True)

        async def _run_once():
            async with session_factory() as db_session:
                return await execute_workflow_m2_for_session(
                    db_session,
                    flow_uuid=flow_uuid,
                    session_id=created.session_id,
                )

        result_a, result_b = await asyncio.gather(_run_once(), _run_once())
        stopped_reasons = {result_a.stopped_reason, result_b.stopped_reason}

        assert "session_execution_locked" in stopped_reasons
        assert "finished_by_component" in stopped_reasons

        row = await _fetch_session_runtime(session_id=created.session_id)
        assert row["state"] == 3
        assert row["ended_at"] is not None

        runtime = row["runtime_variables"]
        assert runtime["variables"]["customs"]["card_counter"] == 1
    finally:
        await _cleanup_flow_with_revision(flow_uuid=inserted_flow_uuid, revision_uuid=revision_uuid)
