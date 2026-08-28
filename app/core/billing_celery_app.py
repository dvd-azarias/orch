from __future__ import annotations

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

billing_beat_schedule: dict[str, dict] = {}
if settings.orch_billing_enabled:
    billing_beat_schedule = {
        "orch-billing-aggregate": {
            "task": "app.tasks.billing.aggregate",
            "schedule": settings.billing_flush_interval_seconds,
            "options": {"queue": settings.celery_billing_queue},
        },
        "orch-billing-publish-due": {
            "task": "app.tasks.billing.publish_due",
            "schedule": settings.billing_retry_scan_interval_seconds,
            "options": {"queue": settings.celery_billing_queue},
        },
        "orch-billing-reconcile": {
            "task": "app.tasks.billing.reconcile",
            "schedule": settings.billing_reconcile_interval_seconds,
            "options": {"queue": settings.celery_billing_queue},
        },
        "orch-billing-scan-reprocess": {
            "task": "app.tasks.billing.scan_reprocess",
            "schedule": settings.billing_reprocess_scan_interval_seconds,
            "options": {"queue": settings.celery_billing_queue},
        },
    }

billing_celery_app = Celery(
    "orch-billing",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.billing_batch_tasks"],
)

billing_celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_ignore_result=True,
    task_always_eager=settings.celery_task_always_eager,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
    task_soft_time_limit=240,
    task_time_limit=300,
    task_default_queue=settings.celery_billing_queue,
    task_routes={"app.tasks.billing.*": {"queue": settings.celery_billing_queue}},
    beat_schedule=billing_beat_schedule,
)
