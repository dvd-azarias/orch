from __future__ import annotations

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


async def _ensure_metrics_table() -> None:
    schema = get_settings().database_schema.replace('"', '""')
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            await db_session.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS "{schema}".orch_session_metrics (
                        id BIGSERIAL PRIMARY KEY,
                        uuid UUID NOT NULL DEFAULT gen_random_uuid(),
                        session_id BIGINT NOT NULL,
                        session_uuid UUID,
                        flow_uuid UUID,
                        revision_id UUID,
                        metric_type TEXT NOT NULL,
                        step_index INTEGER,
                        card_uuid UUID,
                        card_cursor TEXT,
                        component_kind TEXT,
                        status TEXT NOT NULL,
                        stopped_reason TEXT,
                        latency_ms DOUBLE PRECISION NOT NULL,
                        started_at TIMESTAMPTZ NOT NULL,
                        finished_at TIMESTAMPTZ NOT NULL,
                        details JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
            )


async def _insert_flow_with_revision(*, flow_uuid: str, definition: dict) -> tuple[str, str]:
    schema = get_settings().database_schema.replace('"', '""')
    session_factory = get_session_factory()
    revision_uuid = str(uuid4())
    slug = f"test-metric-{flow_uuid[:8]}-{uuid4().hex[:6]}"
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
                    "display_name": f"Flow Metric {flow_uuid[:8]}",
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


@pytest.mark.asyncio
async def test_deve_persistir_metricas_por_card_e_workflow(monkeypatch) -> None:
    await _ensure_flow_tables()
    await _ensure_metrics_table()

    flow_uuid = str(uuid4())
    definition = {
        "trigger_start_by_ref_id": "set-1",
        "components": [
            {
                "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "ref_id": "set-1",
                "component": "set_variables",
                "parameters": {"instructions": [{"variable": "resultado", "value": "123"}]},
            },
            {
                "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "ref_id": "finish-1",
                "component": "finish_flow",
                "parameters": {},
            },
        ],
        "branches": [
            {"from": "set-1", "to": "finish-1"},
        ],
    }

    inserted_flow_uuid, revision_uuid = await _insert_flow_with_revision(flow_uuid=flow_uuid, definition=definition)
    monkeypatch.setattr(workflow_runtime_service, "_read_flag_true", lambda _settings: True)
    monkeypatch.setattr(workflow_m2_service, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(orch_api, "get_settings", lambda: SimpleNamespace(celery_enabled=False))

    try:
        session_factory = get_session_factory()
        async with session_factory() as db_session:
            response = await trigger_orch(
                flow_uuid=UUID(flow_uuid),
                payload={"external_id": f"metric-{flow_uuid[:8]}", "valor_recebido": 11},
                db_session=db_session,
            )

        schema = get_settings().database_schema.replace('"', '""')
        async with session_factory() as db_session:
            rows = (
                await db_session.execute(
                    text(
                        f"""
                        SELECT metric_type, component_kind, status, stopped_reason, latency_ms
                        FROM "{schema}".orch_session_metrics
                        WHERE session_id = :session_id
                        ORDER BY id
                        """
                    ),
                    {"session_id": response.session_id},
                )
            ).mappings().all()

        assert len(rows) >= 3
        metric_types = [str(row["metric_type"]) for row in rows]
        assert "workflow" in metric_types
        assert metric_types.count("card") >= 2
        workflow_row = [row for row in rows if row["metric_type"] == "workflow"][-1]
        assert workflow_row["latency_ms"] >= 0
    finally:
        await _cleanup_flow_with_revision(flow_uuid=inserted_flow_uuid, revision_uuid=revision_uuid)
