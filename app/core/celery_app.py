from __future__ import annotations

from typing import Any, Callable

try:
    from celery import Celery
except ModuleNotFoundError:  # pragma: no cover
    class _DummyTask:
        def __init__(self, func: Callable[..., Any]) -> None:
            self._func = func

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            return self._func(*args, **kwargs)

        def delay(self, *args: Any, **kwargs: Any) -> Any:
            return self._func(*args, **kwargs)

    class Celery:  # type: ignore[override]
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.conf: dict[str, Any] = {}

        def task(self, **_kwargs: Any) -> Callable[[Callable[..., Any]], _DummyTask]:
            def _decorator(func: Callable[..., Any]) -> _DummyTask:
                return _DummyTask(func)

            return _decorator

from app.core.config import get_settings

settings = get_settings()

beat_schedule: dict[str, dict[str, Any]] = {}
if settings.celery_beat_heartbeat_enabled:
    beat_schedule["orch-beat-heartbeat"] = {
        "task": "app.tasks.workflow.beat_heartbeat",
        "schedule": max(2, settings.celery_dispatch_interval_seconds),
        "options": {"queue": settings.celery_heartbeat_queue},
    }
if settings.celery_beat_dispatch_enabled:
    beat_schedule["orch-dispatch-pending-sessions"] = {
        "task": "app.tasks.workflow.dispatch_pending_sessions",
        "schedule": max(1, settings.celery_dispatch_interval_seconds),
        "options": {"queue": settings.celery_dispatch_queue},
    }
if settings.celery_generate_file_enabled and settings.celery_generate_file_scan_enabled:
    beat_schedule["orch-generate-file-scan-due"] = {
        "task": "app.tasks.component_generate_file.scan_due",
        "schedule": max(5, settings.celery_generate_file_scan_interval_seconds),
        "options": {"queue": settings.celery_generate_file_scan_queue},
    }

celery_app = Celery(
    "orch",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.workflow_tasks", "app.tasks.generate_file_tasks", "app.tasks.fileapp_ingest_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_ignore_result=True,
    task_always_eager=settings.celery_task_always_eager,
    task_routes={
        "app.tasks.workflow.dispatch_pending_sessions": {"queue": settings.celery_dispatch_queue},
        "app.tasks.workflow.beat_heartbeat": {"queue": settings.celery_heartbeat_queue},
        "app.tasks.workflow.advance_session": {"queue": settings.celery_execute_queue},
        "app.tasks.component_generate_file.scan_due": {"queue": settings.celery_generate_file_scan_queue},
        "app.tasks.component_generate_file.run": {"queue": settings.celery_generate_file_run_queue},
        "app.tasks.fileapp.ingest_event": {"queue": settings.celery_s3_files_ingest_queue},
        "app.tasks.fileapp.ingest_tipo1_event": {"queue": settings.celery_s3_files_ingest_queue},
        "app.tasks.fileapp.process_event": {"queue": settings.celery_source_list_ingest_queue},
        "app.tasks.fileapp.process_tipo1_event": {"queue": settings.celery_source_list_ingest_queue},
        "app.tasks.fileapp.associate_mailing": {"queue": settings.celery_fileapp_mailing_assoc_queue},
    },
    beat_schedule=beat_schedule,
)
