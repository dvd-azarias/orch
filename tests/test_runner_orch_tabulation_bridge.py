from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.api.v1.orch as orch_api
from app.repositories.orch_sessions_repository import (
    persist_callback_event_for_exact_session,
)
from app.services.migration_service import MIGRATIONS
from app.schemas.orch import (
    OrchRunnerBridgeBindRequest,
    OrchRunnerBridgeTabulationRequest,
)
from app.services.runner_orch_tabulation_bridge_service import (
    bind_runner_session,
    register_runner_tabulation,
)


RUNNER_SESSION_ID = "3801a10c-fb08-4889-bf8a-431671ae92c0"
RUNNER_FLOW_UUID = "6dec25e2-3f2a-4331-956d-9d5e313f5118"
ORCH_FLOW_UUID = "96cd32a7-8736-4312-a871-3e3eb3a3c5c7"
ORCH_SESSION_UUID = "1de666d4-05ee-419a-b9f7-a71fc0d3b764"


class _Result:
    def __init__(self, *, row: dict | None = None, rows: list[dict] | None = None):
        self.row = row
        self.rows = rows if rows is not None else ([] if row is None else [row])

    def mappings(self) -> "_Result":
        return self

    def first(self):  # noqa: ANN201
        return self.row

    def one(self):  # noqa: ANN201
        assert self.row is not None
        return self.row

    def all(self) -> list[dict]:
        return self.rows


class _ScriptedSession:
    def __init__(self, results: list[_Result]):
        self.results = list(results)
        self.statements: list[str] = []
        self.parameters: list[dict] = []

    async def execute(self, statement, parameters=None):  # noqa: ANN001, ANN201
        self.statements.append(str(statement))
        self.parameters.append(parameters or {})
        return self.results.pop(0)


def test_migration_is_additive_registered_and_uses_session_scoped_idempotency() -> None:
    migration_path = "sql/027_create_runner_orch_tabulation_bridge.sql"
    assert MIGRATIONS[-1] == (
        "0027_create_runner_orch_tabulation_bridge",
        migration_path,
    )
    sql = Path(migration_path).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS orch_runner_session_links" in sql
    assert "CREATE TABLE IF NOT EXISTS orch_runner_tabulation_events" in sql
    assert "UNIQUE (runner_session_id, event_key)" in sql
    assert "DROP TABLE" not in sql
    assert "DROP COLUMN" not in sql


def test_internal_auth_is_fail_closed_and_uses_dedicated_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        orch_api,
        "get_settings",
        lambda: SimpleNamespace(
            runner_orch_bridge_client_id="runner-v5",
            runner_orch_bridge_client_secret="bridge-secret",
        ),
    )

    orch_api._require_runner_orch_bridge_client(
        client_id="runner-v5",
        client_secret="bridge-secret",
    )
    with pytest.raises(HTTPException) as exc_info:
        orch_api._require_runner_orch_bridge_client(
            client_id="runner-v5",
            client_secret="wrong",
        )
    assert getattr(exc_info.value, "status_code", None) == 401


def test_internal_contract_rejects_blank_correlation_fields() -> None:
    with pytest.raises(ValidationError):
        OrchRunnerBridgeBindRequest(
            runner_session_id=RUNNER_SESSION_ID,
            runner_flow_uuid=RUNNER_FLOW_UUID,
            provider_context_message_id="   ",
        )
    with pytest.raises(ValidationError):
        OrchRunnerBridgeTabulationRequest(
            runner_session_id=RUNNER_SESSION_ID,
            runner_flow_uuid=RUNNER_FLOW_UUID,
            event_key="   ",
            outcome="recusa",
        )


@pytest.mark.asyncio
async def test_exact_callback_wakes_only_the_linked_active_waiting_session() -> None:
    runtime_variables = {
        "workflow_v2": {
            "blocking_stop_reason": "blocked_wait_for_event",
            "wait_for_event": {
                "event_source": "callback",
                "event_result": "tabulation",
            },
        }
    }
    session = _ScriptedSession(
        [
            _Result(),
            _Result(),
            _Result(
                row={
                    "id": 365,
                    "uuid": ORCH_SESSION_UUID,
                    "flow_uuid": ORCH_FLOW_UUID,
                    "state": 1,
                    "entity": "person-1",
                    "entity_type": "person",
                    "entity_address": "5511975620806",
                    "entity_session_id": ORCH_SESSION_UUID,
                    "runtime_variables": runtime_variables,
                }
            ),
            _Result(
                row={
                    "id": 365,
                    "uuid": ORCH_SESSION_UUID,
                    "flow_uuid": ORCH_FLOW_UUID,
                    "state": 0,
                }
            ),
        ]
    )

    result = await persist_callback_event_for_exact_session(
        session,  # type: ignore[arg-type]
        session_id=365,
        app_name="RunnerV5LiveBridge",
        event_name="callback",
        event_result="tabulation",
        event_data={"outcome": "recusa"},
    )

    assert result is not None
    assert result.id == 365
    assert result.resume_required is True
    assert session.parameters[1] == {"class_id": 92021, "object_id": 365}
    assert "WHERE id = :session_id" in session.statements[2]
    assert "ended_at IS NULL" in session.statements[2]
    assert session.parameters[3]["resume_required"] is True


@pytest.mark.asyncio
async def test_tabulation_without_link_is_durable_and_pending() -> None:
    callback_payload = {"outcome": "improdutiva", "source": "runner_v5_live"}
    session = _ScriptedSession(
        [
            _Result(),
            _Result(),
            _Result(),
            _Result(
                row={
                    "id": 91,
                    "event_key": "live-resolved:event-1",
                    "runner_session_id": RUNNER_SESSION_ID,
                    "runner_flow_uuid": RUNNER_FLOW_UUID,
                    "callback_payload": callback_payload,
                }
            ),
            _Result(),
        ]
    )

    result = await register_runner_tabulation(
        session,  # type: ignore[arg-type]
        runner_session_id=RUNNER_SESSION_ID,
        runner_flow_uuid=RUNNER_FLOW_UUID,
        event_key="live-resolved:event-1",
        callback_payload=callback_payload,
    )

    assert result.status == "pending_link"
    assert result.accepted is True
    assert session.parameters[1]["lock_key"] == (
        f"runner_orch_bridge_session|{RUNNER_SESSION_ID}"
    )
    assert "status" in session.statements[3]


@pytest.mark.asyncio
async def test_bind_not_found_is_fail_closed_and_uses_same_session_lock() -> None:
    session = _ScriptedSession(
        [
            _Result(),
            _Result(),
            _Result(rows=[]),
            _Result(rows=[]),
        ]
    )

    result = await bind_runner_session(
        session,  # type: ignore[arg-type]
        runner_session_id=RUNNER_SESSION_ID,
        runner_flow_uuid=RUNNER_FLOW_UUID,
        provider_context_message_id="wamid.unknown",
    )

    assert result.status == "not_found"
    assert result.accepted is False
    assert result.orch_session_id is None
    assert session.parameters[1]["lock_key"] == (
        f"runner_orch_bridge_session|{RUNNER_SESSION_ID}"
    )
    assert "event.event_id = :provider_context_message_id" in session.statements[3]
    assert "session.ended_at IS NULL" in session.statements[3]


@pytest.mark.asyncio
async def test_bind_applies_tabulation_that_arrived_before_the_link() -> None:
    callback_payload = {"outcome": "improdutiva", "source": "runner_v5_live"}
    waiting_runtime = {
        "workflow_v2": {
            "blocking_stop_reason": "blocked_wait_for_event",
            "wait_for_event": {
                "event_source": "callback",
                "event_result": "tabulation",
            },
        }
    }
    session = _ScriptedSession(
        [
            _Result(),
            _Result(),
            _Result(rows=[]),
            _Result(
                rows=[
                    {
                        "orch_session_id": 365,
                        "orch_session_uuid": ORCH_SESSION_UUID,
                        "orch_flow_uuid": ORCH_FLOW_UUID,
                        "created_at": "2026-10-01T10:00:00Z",
                    }
                ]
            ),
            _Result(),
            _Result(
                rows=[
                    {
                        "id": 91,
                        "event_key": "live-resolved:event-before-bind",
                        "runner_session_id": RUNNER_SESSION_ID,
                        "runner_flow_uuid": RUNNER_FLOW_UUID,
                        "callback_payload": callback_payload,
                    }
                ]
            ),
            _Result(),
            _Result(),
            _Result(
                row={
                    "id": 365,
                    "uuid": ORCH_SESSION_UUID,
                    "flow_uuid": ORCH_FLOW_UUID,
                    "state": 1,
                    "entity": "person-1",
                    "entity_type": "person",
                    "entity_address": "5511975620806",
                    "entity_session_id": ORCH_SESSION_UUID,
                    "runtime_variables": waiting_runtime,
                }
            ),
            _Result(
                row={
                    "id": 365,
                    "uuid": ORCH_SESSION_UUID,
                    "flow_uuid": ORCH_FLOW_UUID,
                    "state": 0,
                }
            ),
            _Result(),
        ]
    )

    result = await bind_runner_session(
        session,  # type: ignore[arg-type]
        runner_session_id=RUNNER_SESSION_ID,
        runner_flow_uuid=RUNNER_FLOW_UUID,
        provider_context_message_id="wamid.outbound-orch",
    )

    assert result.status == "bound"
    assert result.accepted is True
    assert result.resume_session_id == 365
    assert "status = 'applied'" in session.statements[-1]


@pytest.mark.asyncio
async def test_replay_of_ignored_event_remains_ignored_and_idempotent() -> None:
    callback_payload = {"outcome": "recusa", "source": "runner_v5_live"}
    session = _ScriptedSession(
        [
            _Result(),
            _Result(),
            _Result(
                row={
                    "id": 92,
                    "event_key": "live-resolved:event-2",
                    "runner_session_id": RUNNER_SESSION_ID,
                    "runner_flow_uuid": RUNNER_FLOW_UUID,
                    "orch_session_id": 365,
                    "callback_payload": callback_payload,
                    "status": "ignored",
                }
            ),
            _Result(
                row={
                    "runner_session_id": RUNNER_SESSION_ID,
                    "runner_flow_uuid": RUNNER_FLOW_UUID,
                    "orch_session_id": 365,
                    "provider_context_message_id": "wamid.outbound",
                    "orch_session_uuid": ORCH_SESSION_UUID,
                    "orch_flow_uuid": ORCH_FLOW_UUID,
                }
            ),
        ]
    )

    result = await register_runner_tabulation(
        session,  # type: ignore[arg-type]
        runner_session_id=RUNNER_SESSION_ID,
        runner_flow_uuid=RUNNER_FLOW_UUID,
        event_key="live-resolved:event-2",
        callback_payload=callback_payload,
    )

    assert result.status == "ignored"
    assert result.accepted is False
    assert result.idempotent is True
    assert result.orch_session_uuid == ORCH_SESSION_UUID
