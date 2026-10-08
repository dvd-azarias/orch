from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.api.v1.orch as orch_api
import app.services.live_orch_tabulation_service as live_service
from app.schemas.orch import OrchLiveTabulationRequest
from app.services.live_orch_tabulation_service import (
    LiveTabulationResult,
    build_live_tabulation_payloads,
    register_live_tabulation,
)
from app.services.migration_service import MIGRATIONS


WORKSPACE_UUID = "11497cd6-0332-49bb-a9f9-de0addca0114"
FLOW_UUID = "96cd32a7-8736-4312-a871-3e3eb3a3c5c7"
SESSION_UUID = "3d942250-f02e-498f-a416-0958808cf930"
CONVERSATION_ID = "01a11b37-9892-7ac1-a5e2-b785bc86b6d1"
IDEMPOTENCY_KEY = (
    "agent-close-voice:7667ad7d-9b7a-41fd-9aa6-788798522176:"
    "01a11b37-9892-7ac1-a5e2-b785bc86b6d1:state-changed"
)


def _sample_request(**overrides: object) -> OrchLiveTabulationRequest:
    payload: dict[str, object] = {
        "type": "live.conversation.resolved",
        "workspace_id": WORKSPACE_UUID,
        "interaction_id": SESSION_UUID,
        "conversation_id": CONVERSATION_ID,
        "reason": "agent_closed",
        "idempotency_key": IDEMPOTENCY_KEY,
        "tabulation_event_id": "cmuopcbf701vtqg015yal145w",
        "disposition_code": "recusa",
        "disposition_category": "negative",
        "polarity": "negative",
        "is_cpc": False,
        "value": None,
        "notes": None,
        "ends_session": True,
        "additional_data": {"isCpc": False, "valueBrl": None},
    }
    payload.update(overrides)
    return OrchLiveTabulationRequest(**payload)


class _Result:
    def __init__(self, *, row: dict | None = None):
        self.row = row

    def mappings(self) -> "_Result":
        return self

    def first(self):  # noqa: ANN201
        return self.row


class _ScriptedSession:
    def __init__(self, results: list[_Result]):
        self.results = list(results)
        self.statements: list[str] = []
        self.parameters: list[dict] = []

    async def execute(self, statement, parameters=None):  # noqa: ANN001, ANN201
        self.statements.append(str(statement))
        self.parameters.append(parameters or {})
        return self.results.pop(0)


class _TransactionContext:
    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, exc_type, exc, traceback):  # noqa: ANN001, ANN204
        return False


class _ApiSession:
    def __init__(self):
        self.statements: list[str] = []

    def in_transaction(self) -> bool:
        return False

    def begin(self) -> _TransactionContext:
        return _TransactionContext()

    async def execute(self, statement, parameters=None):  # noqa: ANN001, ANN201
        self.statements.append(str(statement))
        return _Result()

    async def commit(self) -> None:
        raise AssertionError("commit não deve ser necessário após db_session.begin()")


def test_live_tabulation_migration_is_additive_and_session_idempotent() -> None:
    migration = (
        "0029_create_orch_live_tabulation_events",
        "sql/029_create_orch_live_tabulation_events.sql",
    )
    assert migration in MIGRATIONS
    assert MIGRATIONS.index(migration) > MIGRATIONS.index(
        (
            "0028_create_orch_flow_builder_tables",
            "sql/028_create_orch_flow_builder_tables.sql",
        )
    )
    sql = Path(migration[1]).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS orch_live_tabulation_events" in sql
    assert "UNIQUE (orch_session_uuid, idempotency_key)" in sql
    assert "ends_session" not in sql
    assert "DROP TABLE" not in sql
    assert "DROP COLUMN" not in sql


def test_contract_accepts_future_fields_and_optional_operational_metadata() -> None:
    request = _sample_request(
        occurred_at="2026-10-08T12:00:00Z",
        call_id="138-1791457990.44",
        future_live_field={"value": "kept"},
    )

    payload = request.model_dump(mode="json")

    assert payload["call_id"] == "138-1791457990.44"
    assert payload["future_live_field"] == {"value": "kept"}


def test_contract_does_not_require_disposition_code_or_outcome() -> None:
    request = _sample_request(disposition_code=None)
    request_payload, callback_payload = build_live_tabulation_payloads(
        request.model_dump(mode="json")
    )

    assert request_payload["disposition_code"] is None
    assert "outcome" not in callback_payload


def test_contract_rejects_blank_idempotency_and_tabulation_event_ids() -> None:
    with pytest.raises(ValidationError):
        _sample_request(idempotency_key="   ")
    with pytest.raises(ValidationError):
        _sample_request(tabulation_event_id="   ")


def test_normalization_discards_ends_session_and_forces_server_source() -> None:
    request = _sample_request(source="untrusted", future_live_field="kept")

    request_payload, callback_payload = build_live_tabulation_payloads(
        request.model_dump(mode="json")
    )

    assert "ends_session" not in request_payload
    assert "ends_session" not in callback_payload
    assert "source" not in request_payload
    assert callback_payload["source"] == "atendimento_live_direct"
    assert callback_payload["outcome"] == "recusa"
    assert callback_payload["future_live_field"] == "kept"


def test_envelope_identity_must_match_route() -> None:
    request = _sample_request(interaction_id="11111111-1111-4111-8111-111111111111")

    with pytest.raises(HTTPException) as exc_info:
        orch_api._validate_live_tabulation_identity(
            workspace_uuid=WORKSPACE_UUID,
            session_uuid=SESSION_UUID,
            request=request,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error_code"] == "live_tabulation_identity_mismatch"
    assert exc_info.value.detail["fields"] == ["interaction_id"]


@pytest.mark.asyncio
async def test_api_normalizes_payload_and_enqueues_only_new_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session = _ApiSession()
    register = AsyncMock(
        return_value=LiveTabulationResult(
            status="applied",
            accepted=True,
            idempotent=False,
            idempotency_key=IDEMPOTENCY_KEY,
            orch_session_id=458,
            orch_session_uuid=SESSION_UUID,
            orch_flow_uuid=FLOW_UUID,
            resume_required=True,
        )
    )
    enqueue = AsyncMock()
    monkeypatch.setattr(
        orch_api,
        "bind_workspace_context",
        lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"),
    )
    monkeypatch.setattr(orch_api, "ensure_active_workspace", AsyncMock())
    monkeypatch.setattr(orch_api, "register_live_tabulation", register)
    monkeypatch.setattr(orch_api, "_enqueue_live_tabulation_resume", enqueue)

    response = await orch_api.callback_live_tabulation_by_session(
        workspace_uuid=UUID(WORKSPACE_UUID),
        flow_uuid=UUID(FLOW_UUID),
        orch_session_uuid=UUID(SESSION_UUID),
        request=_sample_request(),
        db_session=db_session,  # type: ignore[arg-type]
    )

    assert response.status == "applied"
    assert response.resume_required is True
    kwargs = register.await_args.kwargs
    assert "ends_session" not in kwargs["request_payload"]
    assert "ends_session" not in kwargs["callback_payload"]
    assert kwargs["callback_payload"]["outcome"] == "recusa"
    enqueue.assert_awaited_once_with(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        session_id=458,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_status", "expected_http_status"),
    [("not_found", 404), ("conflict", 409)],
)
async def test_api_maps_permanent_delivery_failures_to_4xx(
    monkeypatch: pytest.MonkeyPatch,
    result_status: str,
    expected_http_status: int,
) -> None:
    monkeypatch.setattr(
        orch_api,
        "bind_workspace_context",
        lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"),
    )
    monkeypatch.setattr(orch_api, "ensure_active_workspace", AsyncMock())
    monkeypatch.setattr(
        orch_api,
        "register_live_tabulation",
        AsyncMock(
            return_value=LiveTabulationResult(
                status=result_status,
                accepted=False,
                idempotent=False,
                idempotency_key=IDEMPOTENCY_KEY,
                orch_session_uuid=SESSION_UUID,
                orch_flow_uuid=FLOW_UUID,
            )
        ),
    )
    enqueue = AsyncMock()
    monkeypatch.setattr(orch_api, "_enqueue_live_tabulation_resume", enqueue)

    with pytest.raises(HTTPException) as exc_info:
        await orch_api.callback_live_tabulation_by_session(
            workspace_uuid=UUID(WORKSPACE_UUID),
            flow_uuid=UUID(FLOW_UUID),
            orch_session_uuid=UUID(SESSION_UUID),
            request=_sample_request(),
            db_session=_ApiSession(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == expected_http_status
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_tabulation_applies_to_exact_session_and_records_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_payload, callback_payload = build_live_tabulation_payloads(
        _sample_request().model_dump(mode="json")
    )
    session = _ScriptedSession(
        [
            _Result(),
            _Result(),
            _Result(row={"id": 458, "uuid": SESSION_UUID, "flow_uuid": FLOW_UUID}),
            _Result(),
            _Result(),
        ]
    )
    persisted = SimpleNamespace(
        id=458,
        uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        state=0,
        resume_required=True,
    )
    monkeypatch.setattr(
        live_service,
        "persist_callback_event_for_exact_session",
        AsyncMock(return_value=persisted),
    )

    result = await register_live_tabulation(
        session,  # type: ignore[arg-type]
        session_uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        idempotency_key=IDEMPOTENCY_KEY,
        request_payload=request_payload,
        callback_payload=callback_payload,
    )

    assert result.status == "applied"
    assert result.accepted is True
    assert result.resume_required is True
    assert session.parameters[1]["lock_key"] == (
        f"live_orch_tabulation_session|{SESSION_UUID}"
    )
    assert "uuid = CAST(:session_uuid AS uuid)" in session.statements[2]
    assert "flow_uuid = CAST(:flow_uuid AS uuid)" in session.statements[2]
    inserted_request = json.loads(session.parameters[-1]["request_payload"])
    inserted_callback = json.loads(session.parameters[-1]["callback_payload"])
    assert "ends_session" not in inserted_request
    assert "ends_session" not in inserted_callback
    assert inserted_callback["source"] == "atendimento_live_direct"


@pytest.mark.asyncio
async def test_identical_replay_is_idempotent_without_reapplying_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_payload, callback_payload = build_live_tabulation_payloads(
        _sample_request().model_dump(mode="json")
    )
    session = _ScriptedSession(
        [
            _Result(),
            _Result(),
            _Result(row={"id": 458, "uuid": SESSION_UUID, "flow_uuid": FLOW_UUID}),
            _Result(
                row={
                    "id": 91,
                    "idempotency_key": IDEMPOTENCY_KEY,
                    "orch_session_id": 458,
                    "orch_session_uuid": SESSION_UUID,
                    "flow_uuid": FLOW_UUID,
                    "request_payload": request_payload,
                    "status": "applied",
                    "resume_required": True,
                }
            ),
        ]
    )
    persist = AsyncMock()
    monkeypatch.setattr(live_service, "persist_callback_event_for_exact_session", persist)

    result = await register_live_tabulation(
        session,  # type: ignore[arg-type]
        session_uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        idempotency_key=IDEMPOTENCY_KEY,
        request_payload=request_payload,
        callback_payload=callback_payload,
    )

    assert result.status == "applied"
    assert result.idempotent is True
    assert result.resume_required is True
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_key_with_different_payload_is_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_payload, callback_payload = build_live_tabulation_payloads(
        _sample_request().model_dump(mode="json")
    )
    session = _ScriptedSession(
        [
            _Result(),
            _Result(),
            _Result(row={"id": 458, "uuid": SESSION_UUID, "flow_uuid": FLOW_UUID}),
            _Result(
                row={
                    "id": 91,
                    "idempotency_key": IDEMPOTENCY_KEY,
                    "orch_session_id": 458,
                    "orch_session_uuid": SESSION_UUID,
                    "flow_uuid": FLOW_UUID,
                    "request_payload": {**request_payload, "notes": "different"},
                    "status": "applied",
                    "resume_required": False,
                }
            ),
        ]
    )
    persist = AsyncMock()
    monkeypatch.setattr(live_service, "persist_callback_event_for_exact_session", persist)

    result = await register_live_tabulation(
        session,  # type: ignore[arg-type]
        session_uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        idempotency_key=IDEMPOTENCY_KEY,
        request_payload=request_payload,
        callback_payload=callback_payload,
    )

    assert result.status == "conflict"
    assert result.accepted is False
    assert result.idempotent is False
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_inactive_session_is_durably_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_payload, callback_payload = build_live_tabulation_payloads(
        _sample_request().model_dump(mode="json")
    )
    session = _ScriptedSession(
        [
            _Result(),
            _Result(),
            _Result(row={"id": 458, "uuid": SESSION_UUID, "flow_uuid": FLOW_UUID}),
            _Result(),
            _Result(),
        ]
    )
    monkeypatch.setattr(
        live_service,
        "persist_callback_event_for_exact_session",
        AsyncMock(return_value=None),
    )

    result = await register_live_tabulation(
        session,  # type: ignore[arg-type]
        session_uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        idempotency_key=IDEMPOTENCY_KEY,
        request_payload=request_payload,
        callback_payload=callback_payload,
    )

    assert result.status == "ignored"
    assert result.accepted is False
    assert result.resume_required is False
    assert session.parameters[-1]["status"] == "ignored"


@pytest.mark.asyncio
async def test_unknown_session_is_not_recorded() -> None:
    request_payload, callback_payload = build_live_tabulation_payloads(
        _sample_request().model_dump(mode="json")
    )
    session = _ScriptedSession([_Result(), _Result(), _Result()])

    result = await register_live_tabulation(
        session,  # type: ignore[arg-type]
        session_uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        idempotency_key=IDEMPOTENCY_KEY,
        request_payload=request_payload,
        callback_payload=callback_payload,
    )

    assert result.status == "not_found"
    assert result.accepted is False
    assert len(session.statements) == 3
