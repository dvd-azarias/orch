from __future__ import annotations

from contextvars import ContextVar

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_workspace_uuid_var: ContextVar[str | None] = ContextVar("workspace_uuid", default=None)
_workspace_schema_var: ContextVar[str | None] = ContextVar("workspace_schema", default=None)


def set_request_id(request_id: str) -> None:
    _request_id_var.set(request_id)


def get_request_id() -> str | None:
    return _request_id_var.get()


def set_workspace_context(*, workspace_uuid: str, workspace_schema: str) -> None:
    _workspace_uuid_var.set(workspace_uuid)
    _workspace_schema_var.set(workspace_schema)


def get_workspace_uuid() -> str | None:
    return _workspace_uuid_var.get()


def get_workspace_schema() -> str | None:
    return _workspace_schema_var.get()
