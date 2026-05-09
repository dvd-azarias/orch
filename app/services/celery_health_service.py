from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import redis

from app.core.celery_app import celery_app
from app.core.config import get_settings


def _read_beat_heartbeat(
    *,
    redis_url: str | None,
    heartbeat_key: str,
) -> tuple[bool, datetime | None]:
    if not redis_url:
        return False, None
    client = redis.Redis.from_url(redis_url)
    raw = client.get(heartbeat_key)
    if raw is None:
        return False, None
    try:
        timestamp = float(raw.decode("utf-8"))
    except Exception:
        return False, None
    return True, datetime.fromtimestamp(timestamp, tz=timezone.utc)


def check_celery_health() -> dict[str, Any]:
    settings = get_settings()

    broker_ok = False
    worker_ok = False
    beat_ok = False
    worker_nodes: list[str] = []
    beat_last_seen_at: datetime | None = None
    details: dict[str, Any] = {}

    if not settings.celery_enabled:
        return {
            "enabled": False,
            "healthy": True,
            "broker_ok": None,
            "worker_ok": None,
            "beat_ok": None,
            "worker_nodes": [],
            "beat_last_seen_at": None,
            "details": {"reason": "celery_disabled"},
        }

    try:
        with celery_app.connection_for_read() as conn:
            conn.ensure_connection(max_retries=0)
        broker_ok = True
    except Exception as exc:
        details["broker_error"] = str(exc)

    if broker_ok:
        try:
            insp = celery_app.control.inspect(timeout=1.5)
            ping_data = insp.ping() or {}
            worker_nodes = sorted(str(node) for node in ping_data.keys())
            worker_ok = len(worker_nodes) > 0
        except Exception as exc:
            details["worker_error"] = str(exc)

    try:
        beat_seen, beat_at = _read_beat_heartbeat(
            redis_url=settings.celery_result_backend,
            heartbeat_key=settings.celery_health_heartbeat_key,
        )
        beat_last_seen_at = beat_at
        if beat_seen and beat_at is not None:
            age_seconds = (datetime.now(timezone.utc) - beat_at).total_seconds()
            beat_ok = age_seconds <= max(5, settings.celery_health_heartbeat_ttl_seconds)
            details["beat_heartbeat_age_seconds"] = round(age_seconds, 2)
        else:
            beat_ok = False
    except Exception as exc:
        details["beat_error"] = str(exc)

    healthy = broker_ok and worker_ok and beat_ok
    return {
        "enabled": True,
        "healthy": healthy,
        "broker_ok": broker_ok,
        "worker_ok": worker_ok,
        "beat_ok": beat_ok,
        "worker_nodes": worker_nodes,
        "beat_last_seen_at": beat_last_seen_at.isoformat() if beat_last_seen_at else None,
        "details": details,
    }
