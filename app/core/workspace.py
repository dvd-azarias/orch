from __future__ import annotations

from uuid import UUID

from app.core.config import get_settings
from app.core.request_context import get_workspace_schema, get_workspace_uuid


def normalize_workspace_uuid(raw_workspace_uuid: str) -> str:
    return str(UUID(str(raw_workspace_uuid)))


def workspace_schema_from_uuid(workspace_uuid: str) -> str:
    safe_uuid = normalize_workspace_uuid(workspace_uuid)
    return f"ws_{safe_uuid}"


def get_current_workspace_uuid() -> str:
    current = get_workspace_uuid()
    if current:
        return current
    settings = get_settings()
    fallback = settings.orch_default_workspace_uuid or settings.orch_lab_workspace_uuid
    if fallback:
        return normalize_workspace_uuid(fallback)
    raise ValueError("Workspace não definido no contexto da requisição.")


def get_current_workspace_schema() -> str:
    current = get_workspace_schema()
    if current:
        return current
    settings = get_settings()
    fallback = settings.orch_default_workspace_uuid or settings.orch_lab_workspace_uuid
    if fallback:
        return workspace_schema_from_uuid(fallback)
    return settings.database_schema
