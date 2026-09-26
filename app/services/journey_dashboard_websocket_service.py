from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as async_redis

from app.core.logging import get_logger
from app.core.workspace import normalize_workspace_uuid


logger = get_logger(__name__)
NOTIFICATION_CHANNEL_PREFIX = "orch:journey-dashboard"
TICKET_KEY_PREFIX = "orch:journey-dashboard:ticket"
MAX_CONNECTIONS_PER_WORKSPACE = 64
MAX_CONNECTIONS_PER_REPLICA = 512


def journey_workspace_notification_channel(workspace_uuid: str) -> str:
    return f"{NOTIFICATION_CHANNEL_PREFIX}:{normalize_workspace_uuid(workspace_uuid)}"


def _ticket_key(ticket: str) -> str:
    digest = hashlib.sha256(str(ticket).encode("utf-8")).hexdigest()
    return f"{TICKET_KEY_PREFIX}:{digest}"


@dataclass(frozen=True)
class JourneyDashboardTicket:
    workspace_uuid: str
    workspace_name: str
    principal: str
    issued_at: datetime


async def create_journey_dashboard_ticket(
    *,
    redis_url: str,
    workspace_uuid: str,
    workspace_name: str,
    principal: str,
    ttl_seconds: int,
) -> str:
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    ticket = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "workspace_uuid": safe_workspace_uuid,
            "workspace_name": str(workspace_name or safe_workspace_uuid)[:200],
            "principal": str(principal or "observability-reader")[:200],
            "issued_at": datetime.now(UTC).isoformat(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    client = async_redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        created = await client.set(
            _ticket_key(ticket),
            payload,
            ex=int(ttl_seconds),
            nx=True,
        )
    finally:
        await client.aclose()
    if not created:
        raise RuntimeError("journey_dashboard_ticket_collision")
    return ticket


async def consume_journey_dashboard_ticket(
    *,
    redis_url: str,
    ticket: str,
) -> JourneyDashboardTicket | None:
    safe_ticket = str(ticket or "").strip()
    if len(safe_ticket) < 32 or len(safe_ticket) > 256:
        return None
    client = async_redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        raw = await client.getdel(_ticket_key(safe_ticket))
    finally:
        await client.aclose()
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        issued_at = datetime.fromisoformat(str(payload["issued_at"]))
        if issued_at.tzinfo is None or issued_at.utcoffset() is None:
            return None
        return JourneyDashboardTicket(
            workspace_uuid=normalize_workspace_uuid(payload["workspace_uuid"]),
            workspace_name=str(payload.get("workspace_name") or "")[:200],
            principal=str(payload.get("principal") or "")[:200],
            issued_at=issued_at,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


class JourneyDashboardConnectionLimitError(RuntimeError):
    pass


class JourneyDashboardWorkspaceHub:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._queues: dict[str, set[asyncio.Queue[int]]] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._redis_url: str | None = None

    async def register(
        self,
        *,
        redis_url: str,
        workspace_uuid: str,
    ) -> asyncio.Queue[int]:
        safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
        async with self._lock:
            total = sum(len(items) for items in self._queues.values())
            workspace_count = len(self._queues.get(safe_workspace_uuid, set()))
            if (
                total >= MAX_CONNECTIONS_PER_REPLICA
                or workspace_count >= MAX_CONNECTIONS_PER_WORKSPACE
            ):
                raise JourneyDashboardConnectionLimitError(
                    "journey_dashboard_connection_limit"
                )
            if self._redis_url not in (None, redis_url):
                raise RuntimeError("journey_dashboard_redis_url_changed")
            self._redis_url = redis_url
            queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
            self._queues.setdefault(safe_workspace_uuid, set()).add(queue)
            if self._listener_task is None or self._listener_task.done():
                self._listener_task = asyncio.create_task(self._listen())
            return queue

    async def unregister(
        self,
        *,
        workspace_uuid: str,
        queue: asyncio.Queue[int],
    ) -> None:
        safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
        async with self._lock:
            workspace_queues = self._queues.get(safe_workspace_uuid)
            if workspace_queues is None:
                return
            workspace_queues.discard(queue)
            if not workspace_queues:
                self._queues.pop(safe_workspace_uuid, None)

    async def _dispatch(self, workspace_uuid: str, sequence: int) -> None:
        safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
        async with self._lock:
            queues = tuple(self._queues.get(safe_workspace_uuid, ()))
        for queue in queues:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(int(sequence))
            except asyncio.QueueFull:
                pass

    async def _listen(self) -> None:
        retry_seconds = 1
        while True:
            redis_url = self._redis_url
            if not redis_url:
                return
            client = async_redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=5,
            )
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.psubscribe(f"{NOTIFICATION_CHANNEL_PREFIX}:*")
                retry_seconds = 1
                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=5,
                    )
                    if not message:
                        await asyncio.sleep(0)
                        continue
                    await self._handle_notification(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "journey dashboard redis listener failed",
                    extra={
                        "event": "orch.journey_dashboard.websocket.redis_failed",
                        "exception_type": type(exc).__name__,
                        "retry_seconds": retry_seconds,
                    },
                )
                await asyncio.sleep(retry_seconds)
                retry_seconds = min(30, retry_seconds * 2)
            finally:
                await pubsub.aclose()
                await client.aclose()

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        try:
            channel = str(message.get("channel") or "")
            prefix = f"{NOTIFICATION_CHANNEL_PREFIX}:"
            if not channel.startswith(prefix):
                return
            channel_workspace_uuid = normalize_workspace_uuid(
                channel[len(prefix) :]
            )
            payload = json.loads(str(message.get("data") or "{}"))
            payload_workspace_uuid = normalize_workspace_uuid(
                payload["workspace_uuid"]
            )
            sequence = int(payload["snapshot_sequence"])
            if channel_workspace_uuid != payload_workspace_uuid or sequence < 1:
                return
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        await self._dispatch(channel_workspace_uuid, sequence)


journey_dashboard_workspace_hub = JourneyDashboardWorkspaceHub()
