from __future__ import annotations

import ipaddress
import time
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.orch import router as orch_router
from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.logging import configure_logging, get_logger
from app.core.request_context import get_request_id, set_request_id
from app.core.workspace import workspace_schema_from_uuid
from app.services.celery_health_service import check_celery_health

configure_logging()
logger = get_logger(__name__)
app = FastAPI(title="orch", version="0.1.0")
app.include_router(orch_router)

_DOCS_PROTECTED_PREFIXES = ("/docs", "/redoc")
_DOCS_PROTECTED_EXACT = {"/openapi.json"}


def _is_docs_protected_path(path: str) -> bool:
    if path in _DOCS_PROTECTED_EXACT:
        return True
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in _DOCS_PROTECTED_PREFIXES)


def _normalize_host(value: str | None) -> str:
    if not value:
        return ""
    host = value.strip().lower()
    if not host:
        return ""
    if "," in host:
        host = host.split(",", 1)[0].strip()
    if ":" in host:
        host = host.split(":", 1)[0].strip()
    return host


def _is_docs_blocked_by_host(request: Request, blocked_hosts: tuple[str, ...]) -> bool:
    blocked = {_normalize_host(host) for host in blocked_hosts if _normalize_host(host)}
    if not blocked:
        return False

    forwarded_host = _normalize_host(request.headers.get("x-forwarded-host"))
    request_host = _normalize_host(request.headers.get("host"))
    return forwarded_host in blocked or request_host in blocked


def _parse_ip(value: str | None) -> ipaddress._BaseAddress | None:
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _parse_networks(raw_networks: tuple[str, ...]) -> list[ipaddress._BaseNetwork]:
    parsed: list[ipaddress._BaseNetwork] = []
    for network in raw_networks:
        try:
            parsed.append(ipaddress.ip_network(network, strict=False))
        except ValueError:
            continue
    return parsed


def _ip_in_networks(address: ipaddress._BaseAddress | None, networks: list[ipaddress._BaseNetwork]) -> bool:
    if address is None:
        return False
    return any(address in network for network in networks)


def _resolve_request_origin_ip(
    request: Request,
    trusted_proxy_networks: list[ipaddress._BaseNetwork],
) -> ipaddress._BaseAddress | None:
    direct_ip = _parse_ip(request.client.host if request.client else None)
    if not _ip_in_networks(direct_ip, trusted_proxy_networks):
        return direct_ip

    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if not forwarded_for:
        return None

    first_forwarded_ip = _parse_ip(forwarded_for.split(",")[0].strip())
    return first_forwarded_ip

@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    settings = get_settings()
    if settings.docs_access_control_enabled and _is_docs_protected_path(request.url.path):
        if _is_docs_blocked_by_host(request, settings.docs_blocked_hosts):
            return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not Found"})

        internal_networks = _parse_networks(settings.docs_internal_cidrs)
        trusted_proxy_networks = _parse_networks(settings.docs_trusted_proxy_cidrs)
        origin_ip = _resolve_request_origin_ip(request, trusted_proxy_networks)

        if not _ip_in_networks(origin_ip, internal_networks):
            return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not Found"})

    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    set_request_id(request_id)
    started_at = time.perf_counter()

    logger.info(
        "request started",
        extra={
            "event": "request.start",
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
        },
    )

    response = await call_next(request)

    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request finished",
        extra={
            "event": "request.finish",
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    request_id = get_request_id()
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "api_version": "v1",
            "code": "http_error",
            "detail": str(exc.detail),
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id or ""},
    )


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    request_id = get_request_id()
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "api_version": "v1",
            "code": "validation_error",
            "detail": "Dados inválidos para a requisição.",
            "request_id": request_id,
            "errors": exc.errors(),
        },
        headers={"X-Request-ID": request_id or ""},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    request_id = get_request_id()
    logger.exception(
        "unhandled exception",
        extra={"event": "request.error", "request_id": request_id},
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "api_version": "v1",
            "code": "internal_error",
            "detail": "Erro interno inesperado.",
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id or ""},
    )


@app.get("/health/live", tags=["health"])
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/db", tags=["health"])
async def health_db(session: AsyncSession = Depends(get_db_session)) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def health_ready(
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str | bool]:
    settings = get_settings()
    schema = settings.database_schema
    fallback_workspace_uuid = settings.orch_default_workspace_uuid or settings.orch_lab_workspace_uuid
    if fallback_workspace_uuid:
        schema = workspace_schema_from_uuid(fallback_workspace_uuid)

    try:
        safe_schema = schema.replace('"', '""')
        await session.execute(text(f'SET search_path TO "{safe_schema}"'))
        current_db = (await session.execute(text("SELECT current_database()"))).scalar_one()
        current_schema = (await session.execute(text("SELECT current_schema()"))).scalar_one()
        table_name = (
            await session.execute(text("SELECT to_regclass(:full_table_name)"), {"full_table_name": f"{schema}.orch_sessions"})
        ).scalar_one()
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "ready": False, "reason": "db_unreachable"}

    schema_ok = current_schema == schema
    table_ok = table_name is not None
    ready = schema_ok and table_ok

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "error",
            "ready": False,
            "database": str(current_db),
            "schema_expected": schema,
            "schema_current": str(current_schema),
            "table_orch_sessions_found": table_ok,
            "reason": "schema_or_table_not_ready",
        }

    return {
        "status": "ok",
        "ready": True,
        "database": str(current_db),
        "schema_expected": schema,
        "schema_current": str(current_schema),
        "table_orch_sessions_found": table_ok,
    }


@app.get("/health/celery", tags=["health"])
async def health_celery(response: Response) -> dict[str, str | bool | None | list[str] | dict]:
    health = check_celery_health()
    if not bool(health.get("healthy", False)):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if bool(health.get("healthy", False)) else "error",
        **health,
    }
