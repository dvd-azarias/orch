from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.flow_v2_repository import fetch_revision_by_id, fetch_selected_revision


@dataclass(frozen=True)
class WorkflowRevisionResolution:
    revision: dict[str, Any] | None
    source: str
    requested_revision_id: str | None
    failure_reason: str | None

    @property
    def used_legacy_fallback(self) -> bool:
        return self.source == "selected"


def _declared_revision_id(runtime_variables: dict[str, Any]) -> tuple[bool, str | None]:
    workflow_meta = runtime_variables.get("workflow_v2")
    if not isinstance(workflow_meta, dict) or "revision_id" not in workflow_meta:
        return False, None

    raw_revision_id = workflow_meta.get("revision_id")
    if raw_revision_id is None:
        return True, None
    return True, str(raw_revision_id).strip() or None


async def resolve_workflow_revision_for_session(
    db_session: AsyncSession,
    *,
    flow_id: str,
    runtime_variables: dict[str, Any],
) -> WorkflowRevisionResolution:
    revision_was_declared, raw_revision_id = _declared_revision_id(runtime_variables)
    if revision_was_declared:
        try:
            pinned_revision_id = str(UUID(str(raw_revision_id)))
        except (TypeError, ValueError, AttributeError):
            return WorkflowRevisionResolution(
                revision=None,
                source="pinned",
                requested_revision_id=raw_revision_id,
                failure_reason="pinned_revision_invalid",
            )

        revision = await fetch_revision_by_id(
            db_session,
            flow_id=flow_id,
            revision_id=pinned_revision_id,
        )
        return WorkflowRevisionResolution(
            revision=revision,
            source="pinned",
            requested_revision_id=pinned_revision_id,
            failure_reason=None if revision is not None else "pinned_revision_not_found",
        )

    selected_revision = await fetch_selected_revision(db_session, flow_id=flow_id)
    return WorkflowRevisionResolution(
        revision=selected_revision,
        source="selected",
        requested_revision_id=None,
        failure_reason=None if selected_revision is not None else "revision_not_found",
    )
