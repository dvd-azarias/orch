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

celery_app = Celery(
    "orch",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.workflow_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_ignore_result=True,
    task_always_eager=settings.celery_task_always_eager,
    beat_schedule={
        "orch-dispatch-pending-sessions": {
            "task": "app.tasks.workflow.dispatch_pending_sessions",
            "schedule": max(1, settings.celery_dispatch_interval_seconds),
        },
        "orch-beat-heartbeat": {
            "task": "app.tasks.workflow.beat_heartbeat",
            "schedule": max(2, settings.celery_dispatch_interval_seconds),
        },
    },
)
