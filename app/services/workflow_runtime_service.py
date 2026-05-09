from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.repositories.flow_v2_repository import fetch_flow_row, fetch_selected_revision
from app.repositories.orch_sessions_repository import fetch_session_workflow_state, update_session_workflow_position
from app.services.workflow_engine import build_bootstrap


@dataclass(frozen=True)
class WorkflowBootstrapResult:
    enabled: bool
    loaded: bool
    reason: str | None
    flow_id: str | None
    revision_id: str | None
    revision_version: int | None
    revision_mode: str | None
    next_card_uuid: str | None


class WorkflowBootstrapError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _to_uuid_or_none(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    try:
        return str(UUID(str(raw_value)))
    except Exception:
        return None


async def bootstrap_workflow_for_session(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    session_id: int,
    payload: dict[str, Any],
) -> WorkflowBootstrapResult:
    settings = get_settings()
    if not _read_flag_true(settings):
        return WorkflowBootstrapResult(
            enabled=False,
            loaded=False,
            reason="workflow_v2_disabled",
            flow_id=None,
            revision_id=None,
            revision_version=None,
            revision_mode=None,
            next_card_uuid=None,
        )

    safe_schema = settings.database_schema.replace('"', '""')
    tx_context = db_session.begin_nested() if db_session.in_transaction() else db_session.begin()
    async with tx_context:
        await db_session.execute(text(f'SET LOCAL search_path TO "{safe_schema}"'))
        session_state = await fetch_session_workflow_state(db_session, session_id=session_id)
        if session_state is None:
            return WorkflowBootstrapResult(
                enabled=True,
                loaded=False,
                reason="session_not_found",
                flow_id=None,
                revision_id=None,
                revision_version=None,
                revision_mode=None,
                next_card_uuid=None,
            )

        runtime_variables = session_state.get("runtime_variables")
        workflow_meta = runtime_variables.get("workflow_v2") if isinstance(runtime_variables, dict) else None
        if isinstance(workflow_meta, dict):
            current_next = session_state.get("next_card_uuid")
            if current_next is None:
                raw_cursor = workflow_meta.get("next_card_cursor")
                current_next = str(raw_cursor).strip() if raw_cursor is not None and str(raw_cursor).strip() else None

            raw_version = workflow_meta.get("revision_version")
            revision_version: int | None = None
            try:
                revision_version = int(raw_version) if raw_version is not None else None
            except Exception:
                revision_version = None

            return WorkflowBootstrapResult(
                enabled=True,
                loaded=True,
                reason="already_bootstrapped",
                flow_id=(str(workflow_meta.get("flow_id")) if workflow_meta.get("flow_id") is not None else None),
                revision_id=(str(workflow_meta.get("revision_id")) if workflow_meta.get("revision_id") is not None else None),
                revision_version=revision_version,
                revision_mode=(
                    str(workflow_meta.get("revision_mode"))
                    if workflow_meta.get("revision_mode") is not None
                    else None
                ),
                next_card_uuid=current_next,
            )

        flow_row = await fetch_flow_row(db_session, flow_uuid=flow_uuid)
        if flow_row is None:
            return WorkflowBootstrapResult(
                enabled=True,
                loaded=False,
                reason="flow_not_found",
                flow_id=None,
                revision_id=None,
                revision_version=None,
                revision_mode=None,
                next_card_uuid=None,
            )

        selected_revision = await fetch_selected_revision(db_session, flow_id=str(flow_row["id"]))
        if selected_revision is None:
            return WorkflowBootstrapResult(
                enabled=True,
                loaded=False,
                reason="revision_not_found",
                flow_id=str(flow_row["id"]),
                revision_id=None,
                revision_version=None,
                revision_mode=None,
                next_card_uuid=None,
            )

        definition = selected_revision.get("definition")
        if not isinstance(definition, dict):
            raise WorkflowBootstrapError("invalid_definition", "Definição do fluxo inválida.")

        bootstrap = build_bootstrap(definition)

        runtime_patch = {
            "workflow_v2": {
                "flow_id": str(flow_row["id"]),
                "revision_id": str(selected_revision["id"]),
                "revision_version": int(selected_revision["version"]),
                "revision_mode": str(selected_revision["selection_mode"]),
                "definition_loaded_at": datetime.now(timezone.utc).isoformat(),
                "engine_phase": "m1",
                "last_card_cursor": None,
                "next_card_cursor": bootstrap.next_card_uuid,
            },
            "input_payload": payload,
            "variables": {"payload": payload, **payload},
        }

        await update_session_workflow_position(
            db_session,
            session_id=session_id,
            last_card_uuid=None,
            next_card_uuid=_to_uuid_or_none(bootstrap.next_card_uuid),
            runtime_patch_json=json.dumps(runtime_patch, ensure_ascii=False),
        )

        return WorkflowBootstrapResult(
            enabled=True,
            loaded=True,
            reason=None,
            flow_id=str(flow_row["id"]),
            revision_id=str(selected_revision["id"]),
            revision_version=int(selected_revision["version"]),
            revision_mode=str(selected_revision["selection_mode"]),
            next_card_uuid=bootstrap.next_card_uuid,
        )


def _read_flag_true(settings: Any) -> bool:
    raw = getattr(settings, "workflow_v2_enabled", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}
