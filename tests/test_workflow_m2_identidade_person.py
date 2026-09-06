from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

import app.services.workflow_m2_service as workflow
from app.services.identidade_person_service import IdentidadePersonQueryResult


def _provider_person() -> dict:
    return {
        "document": {"raw": "12345678901", "formatted": "123.456.789-01"},
        "name": {"raw": "MANOEL DO CARMO", "formatted": "Manoel do Carmo"},
        "birthday": "1940-08-12",
        "gender": {"code": None, "description": "Desconhecido"},
        "addresses": [],
        "companies": [],
        "emails": [],
        "phones": [
            {
                "ranking": 1,
                "score": 5,
                "number": {"raw": "21974305218"},
                "has_whatsapp": True,
                "has_rcs": False,
                "do_not_disturb": False,
            }
        ],
        "relatives": [],
    }


def _component(**parameters) -> dict:
    defaults = {
        "document": "{{contact.identifier}}",
        "workspace_id": "27861684-6da2-478c-9996-86eb9ac14d5d",
        "access_token": "secret-token",
        "require_phone": [],
        "require_email": [],
        "person_action": "lookup_only",
        "enrichment_policy": "fill_missing",
        "phone_policy": {"id": "best_eligible", "name": "Melhor telefone elegível"},
        "response_detail": "normalized",
        "output_var": "identidade",
        "mailing_id": None,
        "link_mailing_to_current_flow": [],
    }
    defaults.update(parameters)
    return {"ref_id": "identidade-1", "component_id": "identidade_person", "parameters": defaults}


class FakeSession:
    @asynccontextmanager
    async def begin_nested(self):
        yield


@pytest.mark.asyncio
async def test_identidade_lookup_only_accepts_real_catalog_shapes_without_writes(monkeypatch) -> None:
    query = AsyncMock(
        return_value=IdentidadePersonQueryResult(
            found=True,
            person=_provider_person(),
            attempts=1,
            status_code=200,
        )
    )
    monkeypatch.setattr(workflow, "query_identidade_person", query)
    persist = AsyncMock()
    monkeypatch.setattr(workflow, "insert_person_if_missing", persist)
    runtime = {"variables": {"contact": {"identifier": "123.456.789-01"}, "customs": {}}}

    branch = await workflow._run_identidade_person(
        db_session=FakeSession(),
        flow_uuid="4e7340ee-ac17-488d-953f-46c51d7b2cd3",
        component=_component(
            mailing_id={"mailing_id": "b4474ea9-f12b-4e50-a55a-8165adafa5b7", "name": "Lista"},
            link_mailing_to_current_flow=[{"id": "yes", "name": "Vincular"}],
        ),
        runtime_variables=runtime,
    )

    assert branch == "encontrado"
    assert runtime["variables"]["customs"]["identidade"]["found"] is True
    assert runtime["variables"]["customs"]["identidade"]["local_action"]["status"] == "not_written"
    assert runtime["variables"]["customs"]["identidade"]["mailing_action"]["status"] == "ignored_lookup_only"
    assert "secret-token" not in str(runtime)
    assert "identidade_person_pending_query" not in runtime
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_identidade_routes_empty_data_to_nao_encontrado(monkeypatch) -> None:
    monkeypatch.setattr(
        workflow,
        "query_identidade_person",
        AsyncMock(return_value=IdentidadePersonQueryResult(found=False, person=None, attempts=1, status_code=200)),
    )
    runtime = {"variables": {"contact": {"identifier": "12345678901"}, "customs": {}}}

    branch = await workflow._run_identidade_person(
        db_session=FakeSession(),
        flow_uuid="4e7340ee-ac17-488d-953f-46c51d7b2cd3",
        component=_component(),
        runtime_variables=runtime,
    )

    assert branch == "nao_encontrado"
    assert runtime["variables"]["customs"]["identidade"]["found"] is False


@pytest.mark.asyncio
async def test_identidade_upsert_creates_person_and_adds_to_selected_mailing(monkeypatch) -> None:
    monkeypatch.setattr(
        workflow,
        "query_identidade_person",
        AsyncMock(
            return_value=IdentidadePersonQueryResult(
                found=True,
                person=_provider_person(),
                attempts=1,
                status_code=200,
            )
        ),
    )
    monkeypatch.setattr(workflow, "fetch_person_by_identifier_for_update", AsyncMock(return_value=None))
    monkeypatch.setattr(
        workflow,
        "insert_person_if_missing",
        AsyncMock(
            return_value={
                "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "identifier": "12345678901",
                "full_name": "Manoel do Carmo",
                "channels": [{"type": "voice", "value": "21974305218", "label": "identidade_tel_1"}],
                "extras": {},
            }
        ),
    )
    monkeypatch.setattr(
        workflow,
        "resolve_source_list_by_public_id",
        AsyncMock(
            return_value={
                "id": 1139,
                "public_id": "b4474ea9-f12b-4e50-a55a-8165adafa5b7",
                "status": "PROCESSED",
            }
        ),
    )
    membership = AsyncMock(return_value={"created": True, "contact_draft_id": "draft-1", "channels": 1})
    monkeypatch.setattr(workflow, "ensure_person_in_source_list", membership)
    runtime = {"variables": {"contact": {"identifier": "12345678901"}, "customs": {}}}

    branch = await workflow._run_identidade_person(
        db_session=FakeSession(),
        flow_uuid="4e7340ee-ac17-488d-953f-46c51d7b2cd3",
        component=_component(
            person_action={"id": "upsert", "name": "Criar ou enriquecer"},
            mailing_id={"mailing_id": "b4474ea9-f12b-4e50-a55a-8165adafa5b7"},
        ),
        runtime_variables=runtime,
    )

    assert branch == "encontrado"
    result = runtime["variables"]["customs"]["identidade"]
    assert result["local_action"]["status"] == "created"
    assert result["mailing_action"]["status"] == "added"
    membership.assert_awaited_once()


@pytest.mark.asyncio
async def test_identidade_reuses_pending_success_after_persistence_failure(monkeypatch) -> None:
    query = AsyncMock(
        return_value=IdentidadePersonQueryResult(
            found=True,
            person=_provider_person(),
            attempts=1,
            status_code=200,
        )
    )
    monkeypatch.setattr(workflow, "query_identidade_person", query)
    monkeypatch.setattr(workflow, "fetch_person_by_identifier_for_update", AsyncMock(side_effect=RuntimeError("db")))
    runtime = {"variables": {"contact": {"identifier": "12345678901"}, "customs": {}}}
    component = _component(person_action="upsert")

    with pytest.raises(workflow.WorkflowExecutionError) as first:
        await workflow._run_identidade_person(
            db_session=FakeSession(),
            flow_uuid="4e7340ee-ac17-488d-953f-46c51d7b2cd3",
            component=component,
            runtime_variables=runtime,
        )
    with pytest.raises(workflow.WorkflowExecutionError):
        await workflow._run_identidade_person(
            db_session=FakeSession(),
            flow_uuid="4e7340ee-ac17-488d-953f-46c51d7b2cd3",
            component=component,
            runtime_variables=runtime,
        )

    assert first.value.code == "identidade_person_persistence_failed"
    assert query.await_count == 1
    assert runtime["identidade_person_pending_query"]["document_masked"] == "***8901"
    assert "secret-token" not in str(runtime)


@pytest.mark.asyncio
async def test_identidade_blocks_for_post_commit_flow_link(monkeypatch) -> None:
    monkeypatch.setattr(
        workflow,
        "query_identidade_person",
        AsyncMock(
            return_value=IdentidadePersonQueryResult(
                found=True,
                person=_provider_person(),
                attempts=1,
                status_code=200,
            )
        ),
    )
    monkeypatch.setattr(workflow, "fetch_person_by_identifier_for_update", AsyncMock(return_value=None))
    monkeypatch.setattr(
        workflow,
        "insert_person_if_missing",
        AsyncMock(return_value={"uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "identifier": "12345678901"}),
    )
    monkeypatch.setattr(
        workflow,
        "resolve_source_list_by_public_id",
        AsyncMock(return_value={"id": 1139, "status": "PROCESSED"}),
    )
    monkeypatch.setattr(
        workflow,
        "ensure_person_in_source_list",
        AsyncMock(return_value={"created": True, "contact_draft_id": "draft-1", "channels": 0}),
    )
    monkeypatch.setattr(workflow, "fetch_active_flow_mailing_link", AsyncMock(return_value=None))
    runtime = {"variables": {"contact": {"identifier": "12345678901"}, "customs": {}}}

    branch = await workflow._run_identidade_person(
        db_session=FakeSession(),
        flow_uuid="4e7340ee-ac17-488d-953f-46c51d7b2cd3",
        component=_component(
            person_action="upsert",
            mailing_id={"mailing_id": "b4474ea9-f12b-4e50-a55a-8165adafa5b7"},
            link_mailing_to_current_flow=[{"id": "yes"}],
        ),
        runtime_variables=runtime,
    )

    assert branch is None
    state = runtime["workflow_v2"]["identidade_person_flow_link"]
    assert state["status"] == "pending"
    assert state["mailing_uuid"] == "b4474ea9-f12b-4e50-a55a-8165adafa5b7"
    assert "secret-token" not in str(state)


@pytest.mark.asyncio
async def test_identidade_resumes_completed_flow_link_without_new_provider_query(monkeypatch) -> None:
    query = AsyncMock(side_effect=AssertionError("não deveria consultar novamente"))
    monkeypatch.setattr(workflow, "query_identidade_person", query)
    custom_result = {
        "found": True,
        "mailing_action": {"status": "added", "flow_link": "pending"},
    }
    last_result = {
        "found": True,
        "mailing_action": {"status": "added", "flow_link": "pending"},
    }
    runtime = {
        "variables": {"customs": {"identidade": custom_result}},
        "identidade_person_last_result": {"output_var": "identidade", "result": last_result},
        "workflow_v2": {
            "identidade_person_flow_link": {
                "component_ref_id": "identidade-1",
                "status": "completed",
                "attempts": 1,
                "status_code": 200,
            }
        },
    }

    branch = await workflow._run_identidade_person(
        db_session=FakeSession(),
        flow_uuid="4e7340ee-ac17-488d-953f-46c51d7b2cd3",
        component=_component(),
        runtime_variables=runtime,
    )

    assert branch == "encontrado"
    assert custom_result["mailing_action"]["flow_link"] == "linked"
    assert last_result["mailing_action"]["flow_link"] == "linked"
    assert runtime["workflow_v2"]["identidade_person_flow_link"]["status"] == "consumed"
    query.assert_not_awaited()


@pytest.mark.asyncio
async def test_identidade_resumes_failed_flow_link_through_exception_without_new_query(monkeypatch) -> None:
    query = AsyncMock(side_effect=AssertionError("não deveria consultar novamente"))
    monkeypatch.setattr(workflow, "query_identidade_person", query)
    custom_result = {
        "found": True,
        "mailing_action": {"status": "added", "flow_link": "pending"},
    }
    runtime = {
        "variables": {"customs": {"identidade": custom_result}},
        "identidade_person_last_result": {"output_var": "identidade", "result": dict(custom_result)},
        "workflow_v2": {
            "identidade_person_flow_link": {
                "component_ref_id": "identidade-1",
                "status": "failed",
                "attempts": 3,
                "status_code": 503,
                "last_error": {
                    "code": "identidade_person_flow_link_target_core_http_error",
                    "message": "Target Core respondeu HTTP 503.",
                },
            }
        },
    }

    with pytest.raises(workflow.WorkflowExecutionError) as exc:
        await workflow._run_identidade_person(
            db_session=FakeSession(),
            flow_uuid="4e7340ee-ac17-488d-953f-46c51d7b2cd3",
            component=_component(),
            runtime_variables=runtime,
        )

    assert exc.value.code == "identidade_person_flow_link_target_core_http_error"
    assert custom_result["mailing_action"]["flow_link"] == "failed"
    assert runtime["workflow_v2"]["identidade_person_flow_link"]["status"] == "consumed"
    query.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identidade_branch", "expected_stop", "expected_steps"),
    [
        ("encontrado", "end_of_branch", 2),
        (None, "blocked_identidade_person_flow_link", 1),
    ],
)
async def test_workflow_m2_dispatches_identidade_person_and_follows_branch(
    monkeypatch,
    identidade_branch,
    expected_stop,
    expected_steps,
) -> None:
    flow_uuid = "33333333-3333-3333-3333-333333333333"
    identidade_ref = "11111111-1111-1111-1111-111111111111"
    set_ref = "22222222-2222-2222-2222-222222222222"
    definition = {
        "components": [
            {"ref_id": identidade_ref, "component_id": "identidade_person", "parameters": {}},
            {"ref_id": set_ref, "component_id": "set_variables", "parameters": {"instructions": []}},
        ],
        "branches": [{"from": identidade_ref, "to": set_ref, "branch": "encontrado"}],
    }
    runtime = {
        "input_payload": {
            "session_scope": "person",
            "contact_list_member_id": 77,
            "contact_list_id": "66666666-6666-6666-6666-666666666666",
            "mailing_id": 1139,
        },
        "workflow_v2": {"flow_id": flow_uuid, "next_card_cursor": identidade_ref},
        "variables": {"payload": {}, "customs": {}},
    }
    persisted: list[dict] = []

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class Result:
        def scalar_one(self) -> bool:
            return True

    class Session:
        def in_transaction(self) -> bool:
            return False

        def begin(self) -> Transaction:
            return Transaction()

        async def execute(self, *_args, **_kwargs) -> Result:
            return Result()

    monkeypatch.setattr(workflow, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(workflow, "fetch_flow_row", AsyncMock(return_value={"id": flow_uuid}))
    monkeypatch.setattr(
        workflow,
        "fetch_selected_revision",
        AsyncMock(return_value={"id": "44444444-4444-4444-4444-444444444444", "definition": definition}),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_session_workflow_state",
        AsyncMock(
            return_value={
                "uuid": "55555555-5555-5555-5555-555555555555",
                "state": 0,
                "runtime_variables": runtime,
                "last_card_uuid": None,
                "next_card_uuid": identidade_ref,
                "frozen_until": None,
            }
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_contact_runtime_context_for_session",
        AsyncMock(
            return_value={
                "contact_list_member_id": 77,
                "contact_identifier": "12345678901",
                "contact_channel_type": "voice",
                "contact_channel_address": "21974305218",
            }
        ),
    )

    async def replace(*_args, **kwargs):
        persisted.append(kwargs)

    monkeypatch.setattr(workflow, "replace_session_workflow_state", replace)
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())
    run_identidade = AsyncMock(return_value=identidade_branch)
    monkeypatch.setattr(workflow, "_run_identidade_person", run_identidade)

    result = await workflow.execute_workflow_m2_for_session(
        Session(),
        flow_uuid=flow_uuid,
        session_id=123,
    )

    assert result.stopped_reason == expected_stop
    assert result.executed_steps == expected_steps
    run_identidade.assert_awaited_once()
    if identidade_branch is None:
        assert persisted[-1]["last_card_uuid"] == identidade_ref
        assert persisted[-1]["next_card_uuid"] == identidade_ref
    else:
        assert persisted[-1]["last_card_uuid"] == set_ref
        assert persisted[-1]["next_card_uuid"] is None
