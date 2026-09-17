from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

import app.api.v1.orch_observability as observability_api
from app.services.journey_observability_service import (
    JourneyObservabilityValidationError,
    _mask_address,
    _mask_identifier,
    _project_definition,
    validate_period,
)
from app.repositories.journey_observability_repository import (
    fetch_journey_sessions,
    fetch_observed_revisions,
)


def _settings(**overrides):  # type: ignore[no-untyped-def]
    values = {
        "orch_observability_client_id": "journey-ui",
        "orch_observability_client_secret": "strong-secret",
        "orch_observability_max_window_hours": 168,
        "orch_observability_statement_timeout_ms": 5000,
        "orch_observability_max_trace_steps": 2000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_observability_auth_fails_closed_when_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(
        observability_api,
        "get_settings",
        lambda: _settings(orch_observability_client_id=None, orch_observability_client_secret=None),
    )
    with pytest.raises(HTTPException) as exc_info:
        observability_api.require_observability_reader(
            client_id="journey-ui",
            client_secret="strong-secret",
        )
    assert exc_info.value.status_code == 503


def test_observability_auth_rejects_wrong_secret(monkeypatch) -> None:
    monkeypatch.setattr(observability_api, "get_settings", lambda: _settings())
    with pytest.raises(HTTPException) as exc_info:
        observability_api.require_observability_reader(
            client_id="journey-ui",
            client_secret="wrong",
        )
    assert exc_info.value.status_code == 401


def test_observability_auth_accepts_dedicated_credentials(monkeypatch) -> None:
    monkeypatch.setattr(observability_api, "get_settings", lambda: _settings())
    observability_api.require_observability_reader(
        client_id="journey-ui",
        client_secret="strong-secret",
    )


def test_validate_period_requires_timezone_and_enforces_safe_window() -> None:
    now = datetime.now(timezone.utc)
    assert validate_period(now - timedelta(hours=1), now, settings=_settings()) == (
        now - timedelta(hours=1),
        now,
    )
    with pytest.raises(JourneyObservabilityValidationError, match="timezone"):
        validate_period(now.replace(tzinfo=None), now, settings=_settings())
    with pytest.raises(JourneyObservabilityValidationError, match="168 horas"):
        validate_period(now - timedelta(hours=169), now, settings=_settings())


def test_project_definition_exposes_only_structural_canvas_data() -> None:
    definition = {
        "trigger_start_by_ref_id": "card-a",
        "components": [
            {
                "ref_id": "card-a",
                "component_id": "api_call",
                "description": "Consultar cadastro",
                "parameters": {"token": "must-not-leak"},
            },
            {
                "ref_id": "card-b",
                "component_id": "finish_flow",
                "description": "Encerrar",
            },
        ],
        "branches": [{"from": "card-a", "to": "card-b", "branch": "success"}],
        "canvas_properties": {
            "positions": [
                {"ref_id": "trigger-start-node", "position": {"x": 10, "y": 20}},
                {"ref_id": "card-a", "position": {"x": 300, "y": 200}},
            ]
        },
    }
    nodes, edges = _project_definition(definition)
    assert [node.id for node in nodes] == ["trigger-start-node", "card-a", "card-b"]
    assert nodes[1].label == "Consultar cadastro"
    assert nodes[1].position_x == 300
    assert [edge.branch for edge in edges] == ["start", "success"]
    assert "must-not-leak" not in repr(nodes)


def test_masks_person_identifiers_and_channel_addresses() -> None:
    assert _mask_identifier("12345678901") == "123••••••01"
    assert _mask_address("5511975620806") == "••••0806"
    assert _mask_address("deivid@example.com") == "d•••@example.com"


class _MappingsListResult:
    def mappings(self) -> "_MappingsListResult":
        return self

    def all(self) -> list[dict]:
        return []


class _RecordingSession:
    def __init__(self) -> None:
        self.statement = ""
        self.parameters: dict = {}

    async def execute(self, statement, parameters=None) -> _MappingsListResult:  # noqa: ANN001
        self.statement = str(statement)
        self.parameters = parameters or {}
        return _MappingsListResult()


@pytest.mark.asyncio
async def test_observed_revisions_follow_card_activity_in_the_period() -> None:
    session = _RecordingSession()
    now = datetime.now(timezone.utc)

    await fetch_observed_revisions(
        session,
        flow_uuid="11111111-1111-4111-8111-111111111111",
        period_from=now - timedelta(hours=1),
        period_to=now,
    )

    assert "FROM orch_session_metrics" in session.statement
    assert "COUNT(DISTINCT session_id)" in session.statement
    assert "metric_type = 'card'" in session.statement
    assert "LIMIT 20" in session.statement


@pytest.mark.asyncio
async def test_person_filter_covers_person_and_channel_session_modes() -> None:
    session = _RecordingSession()
    now = datetime.now(timezone.utc)
    person_uuid = "22222222-2222-4222-8222-222222222222"

    await fetch_journey_sessions(
        session,
        flow_uuid="11111111-1111-4111-8111-111111111111",
        person_uuid=person_uuid,
        period_from=now - timedelta(hours=1),
        period_to=now,
        limit=51,
        cursor_created_at=None,
        cursor_id=None,
    )

    assert "FROM contact_list_members clm" in session.statement
    assert "clm.person_uuid = CAST(:person_uuid AS uuid)" in session.statement
    assert "orch_sessions.entity_type = 'person'" in session.statement
    assert "FROM persons p" in session.statement
    assert session.parameters["person_uuid"] == person_uuid


@pytest.mark.asyncio
async def test_workspace_binding_enforces_database_read_only_before_validation(monkeypatch) -> None:
    calls: list[str] = []

    class _Session:
        async def execute(self, statement) -> None:  # noqa: ANN001
            calls.append(str(statement).strip())

    async def _ensure(_session, *, workspace_uuid: str) -> None:  # noqa: ANN001
        calls.append(f"validate:{workspace_uuid}")

    monkeypatch.setattr(observability_api, "ensure_active_workspace", _ensure)
    workspace_uuid = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"

    await observability_api._bind_active_workspace(  # noqa: SLF001
        _Session(),
        workspace_uuid=UUID(workspace_uuid),
    )

    assert calls == ["SET TRANSACTION READ ONLY", f"validate:{workspace_uuid}"]
