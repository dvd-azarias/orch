from __future__ import annotations

import asyncio

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.workspace import normalize_workspace_uuid
from app.services.generate_file_dispatch_service import list_due_job_ids, process_generate_file_job
from app.services.workspace_service import list_completed_workspaces

logger = get_logger(__name__)


@celery_app.task(name="app.tasks.component_generate_file.scan_due", ignore_result=True)
def scan_due_generate_file_jobs_task() -> dict[str, int]:
    scanned, enqueued = asyncio.run(_scan_due_generate_file_jobs_task())
    return {"workspaces_scanned": scanned, "jobs_enqueued": enqueued}


@celery_app.task(name="app.tasks.component_generate_file.run", ignore_result=True)
def generate_file_run_task(*, workspace_uuid: str, job_id: str) -> dict:
    return asyncio.run(_generate_file_run_task(workspace_uuid=workspace_uuid, job_id=job_id))


async def _scan_due_generate_file_jobs_task() -> tuple[int, int]:
    settings = get_settings()
    if not settings.celery_enabled or not settings.celery_generate_file_enabled:
        return 0, 0

    from app.core.database import get_session_factory

    scanned = 0
    enqueued = 0
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        workspaces = await list_completed_workspaces(db_session)
        workspace_scope = (
            normalize_workspace_uuid(settings.celery_generate_file_workspace_uuid)
            if settings.celery_generate_file_workspace_uuid
            else None
        )
        for row in workspaces:
            workspace_uuid = normalize_workspace_uuid(str(row["workspace_uuid"]))
            if workspace_scope and workspace_uuid != workspace_scope:
                continue
            scanned += 1
            due_job_ids = await list_due_job_ids(
                db_session,
                workspace_uuid=workspace_uuid,
                limit=settings.celery_generate_file_scan_batch_size,
            )
            for job_id in due_job_ids:
                generate_file_run_task.delay(workspace_uuid=workspace_uuid, job_id=job_id)
                enqueued += 1
            if due_job_ids:
                logger.info(
                    "generate_file scan_due enqueued workspace=%s jobs=%s",
                    workspace_uuid,
                    len(due_job_ids),
                )

    logger.info(
        "generate_file scan_due finished",
        extra={
            "event": "orch.generate_file.scan_due",
            "workspaces_scanned": scanned,
            "jobs_enqueued": enqueued,
        },
    )
    return scanned, enqueued


async def _generate_file_run_task(*, workspace_uuid: str, job_id: str) -> dict:
    settings = get_settings()
    if not settings.celery_enabled or not settings.celery_generate_file_enabled:
        return {"status": "disabled"}

    from app.core.database import get_session_factory

    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    workspace_scope = (
        normalize_workspace_uuid(settings.celery_generate_file_workspace_uuid)
        if settings.celery_generate_file_workspace_uuid
        else None
    )
    if workspace_scope and safe_workspace_uuid != workspace_scope:
        logger.warning(
            "generate_file run skipped by workspace scope workspace=%s scope=%s job=%s",
            safe_workspace_uuid,
            workspace_scope,
            job_id,
        )
        return {
            "status": "workspace_scope_mismatch",
            "workspace_uuid": safe_workspace_uuid,
            "workspace_scope": workspace_scope,
            "job_id": job_id,
        }

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        result = await process_generate_file_job(
            db_session,
            workspace_uuid=safe_workspace_uuid,
            job_id=job_id,
        )
        await db_session.commit()
    logger.info(
        "generate_file run finished workspace=%s job=%s status=%s rows_selected=%s rows_sent=%s rows_failed=%s",
        safe_workspace_uuid,
        job_id,
        result.get("status"),
        result.get("rows_selected"),
        result.get("rows_sent"),
        result.get("rows_failed"),
    )
    logger.info(
        "generate_file run finished",
        extra={
            "event": "orch.generate_file.run",
            "workspace_uuid": safe_workspace_uuid,
            "job_id": job_id,
            "status": result.get("status"),
            "rows_selected": result.get("rows_selected"),
            "rows_sent": result.get("rows_sent"),
        },
    )
    return result
