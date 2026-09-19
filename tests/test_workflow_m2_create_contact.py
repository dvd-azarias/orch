from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import ANY, AsyncMock

import pytest

import app.services.workflow_m2_service as workflow
from app.services.workflow_revision_service import WorkflowRevisionResolution


PERSON_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
FLOW_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class _NestedSession:
    @asynccontextmanager
    async def begin_nested(self):
        yield


def _component(**parameters) -> dict:
    defaults = {
        "person_action": "upsert",
        "identifier": "{{contact.identifier}}",
        "enrichment_policy": "fill_missing",
        "mapping": [
            {"key": "full_name", "value": "{{payload.name}}"},
            {"key": "extra.segment", "value": "{{payload.segment}}"},
        ],
        "output_var": "contact_action",
    }
    defaults.update(parameters)
    return {
        "ref_id": "create-contact-1",
        "component_id": "create_contact",
        "parameters": defaults,
    }


def _runtime(**payload) -> dict:
    return {
        "variables": {
            "payload": {
                "name": "Nome Novo",
                "segment": "premium",
                **payload,
            },
            "customs": {},
            "contact": {"identifier": "12345678901", "person_uuid": PERSON_UUID},
        }
    }


def _person(**overrides) -> dict:
    person = {
        "id": 1,
        "uuid": PERSON_UUID,
        "identifier": "12345678901",
        "full_name": "Nome Atual",
        "company": None,
        "gender": None,
        "role": None,
        "country": None,
        "state": None,
        "city": None,
        "birthdate": None,
        "extras": {},
    }
    person.update(overrides)
    return person


def test_person_adoption_rejects_identity_change_after_bootstrap() -> None:
    runtime: dict = {"variables": {"customs": {}}}
    workflow._adopt_person_runtime_scope(
        runtime_variables=runtime,
        person=_person(),
        contact_row=None,
        source_component_kind="create_contact",
        source_component_ref_id="create-contact-1",
    )

    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._adopt_person_runtime_scope(
            runtime_variables=runtime,
            person=_person(
                uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
                identifier="10987654321",
            ),
            contact_row=None,
            source_component_kind="identidade_person",
            source_component_ref_id="identidade-person-1",
        )

    assert exc_info.value.code == "contact_person_identity_conflict"
    assert runtime["workflow_v2"]["person_adoption"]["person_uuid"] == PERSON_UUID
    assert runtime["variables"]["contact"]["identifier"] == "12345678901"


def test_build_payload_renderiza_campos_e_extras_aninhados() -> None:
    payload, configured = workflow._build_create_contact_payload(
        mapping=[
            {"key": "full_name", "value": "{{payload.name}}"},
            {"key": "birthdate", "value": "1940-08-12"},
            {"key": "extra.profile.segment", "value": "{{payload.segment}}"},
        ],
        resolution_scope={"payload": {"name": "  Pessoa Teste  ", "segment": "premium"}},
    )

    assert payload == {
        "full_name": "Pessoa Teste",
        "birthdate": date(1940, 8, 12),
        "extras": {"profile": {"segment": "premium"}},
    }
    assert configured == ["full_name", "birthdate", "extra.profile.segment"]


def test_build_payload_preserva_false_e_zero_em_extras() -> None:
    payload, configured = workflow._build_create_contact_payload(
        mapping={"extra.enabled": False, "extra.score": 0},
        resolution_scope={},
    )

    assert payload == {"extras": {"enabled": False, "score": 0}}
    assert configured == ["extra.enabled", "extra.score"]


def test_build_payload_vazio_so_e_aceito_quando_explicitamente_permitido() -> None:
    mapping = [
        {"key": "full_name", "value": "{{payload.name}}"},
        {"key": "city", "value": "{{payload.city}}"},
    ]
    resolution_scope = {"payload": {"name": "", "city": None}}

    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._build_create_contact_payload(
            mapping=mapping,
            resolution_scope=resolution_scope,
        )

    assert exc_info.value.code == "create_contact_empty_mapping"
    payload, configured = workflow._build_create_contact_payload(
        mapping=mapping,
        resolution_scope=resolution_scope,
        allow_empty=True,
    )
    assert payload == {"extras": {}}
    assert configured == []


@pytest.mark.parametrize("field", ["identifier", "address", "channels", "name"])
def test_build_payload_rejeita_campos_fora_do_novo_contrato(field: str) -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._build_create_contact_payload(
            mapping=[{"key": field, "value": "valor"}],
            resolution_scope={},
        )

    assert exc_info.value.code == "create_contact_invalid_mapping_field"


def test_build_payload_nao_faz_fanout_de_listas_do_contrato_antigo() -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._build_create_contact_payload(
            mapping=[{"key": "full_name", "value": "{{payload.names}}"}],
            resolution_scope={"payload": {"names": ["Pessoa Um", "Pessoa Dois"]}},
        )

    assert exc_info.value.code == "create_contact_invalid_mapping_value"


def test_build_payload_rejeita_caminhos_extra_conflitantes() -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        workflow._build_create_contact_payload(
            mapping=[
                {"key": "extra.profile", "value": "premium"},
                {"key": "extra.profile.segment", "value": "gold"},
            ],
            resolution_scope={},
        )

    assert exc_info.value.code == "create_contact_conflicting_extra_mapping"


def test_merge_fill_missing_preserva_dados_e_completa_extras() -> None:
    merged, changed = workflow._merge_create_contact_payload(
        _person(
            full_name="Nome Atual",
            company="",
            extras={"profile": {"tier": "gold", "origin": None}},
        ),
        {
            "full_name": "Nome Novo",
            "company": "GOHP",
            "extras": {"profile": {"tier": "silver", "origin": "campaign"}},
        },
        enrichment_policy="fill_missing",
    )

    assert merged["full_name"] == "Nome Atual"
    assert merged["company"] == "GOHP"
    assert merged["extras"] == {"profile": {"tier": "gold", "origin": "campaign"}}
    assert changed == ["company", "extra.profile.origin"]


def test_merge_overwrite_non_null_substitui_sem_apagar() -> None:
    merged, changed = workflow._merge_create_contact_payload(
        _person(full_name="Nome Atual", extras={"segment": "basic", "preserved": "yes"}),
        {"full_name": "Nome Novo", "extras": {"segment": "premium"}},
        enrichment_policy="overwrite_non_null",
    )

    assert merged["full_name"] == "Nome Novo"
    assert merged["extras"] == {"segment": "premium", "preserved": "yes"}
    assert changed == ["full_name", "extra.segment"]


@pytest.mark.asyncio
async def test_update_current_usa_person_uuid_contextual_e_ignora_identifier_do_card(monkeypatch) -> None:
    existing = _person(company=None)
    fetch_current = AsyncMock(return_value=existing)
    update = AsyncMock(return_value=_person(company="GOHP"))
    fetch_identifier = AsyncMock()
    monkeypatch.setattr(workflow, "fetch_create_contact_person_by_uuid_for_update", fetch_current)
    monkeypatch.setattr(workflow, "fetch_create_contact_person_by_identifier_for_update", fetch_identifier)
    monkeypatch.setattr(workflow, "update_create_contact_person_profile", update)
    runtime = _runtime(name="Nome Atual", segment=None)

    branch = await workflow._run_create_contact(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(
            person_action="update_current",
            identifier="identificador-que-deve-ser-ignorado",
            mapping=[{"key": "company", "value": "GOHP"}],
        ),
        runtime_variables=runtime,
        contact_row={"person_uuid": PERSON_UUID},
    )

    assert branch == "updated"
    fetch_current.assert_awaited_once_with(ANY, person_uuid=PERSON_UUID)
    fetch_identifier.assert_not_awaited()
    update.assert_awaited_once()
    output = runtime["variables"]["customs"]["contact_action"]
    assert output == {
        "action": "updated",
        "person_uuid": PERSON_UUID,
        "identifier": "12345678901",
        "changed_fields": ["company"],
    }


@pytest.mark.asyncio
async def test_update_current_sem_person_contextual_segue_not_found(monkeypatch) -> None:
    fetch_current = AsyncMock()
    monkeypatch.setattr(workflow, "fetch_create_contact_person_by_uuid_for_update", fetch_current)
    runtime = _runtime()

    branch = await workflow._run_create_contact(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(person_action="update_current"),
        runtime_variables=runtime,
        contact_row={"person_uuid": None},
    )

    assert branch == "not_found"
    fetch_current.assert_not_awaited()
    assert runtime["variables"]["customs"]["contact_action"]["person_uuid"] is None


@pytest.mark.asyncio
async def test_create_if_missing_preserva_pessoa_existente_sem_update(monkeypatch) -> None:
    existing = _person()
    monkeypatch.setattr(
        workflow,
        "fetch_create_contact_person_by_identifier_for_update",
        AsyncMock(return_value=existing),
    )
    insert = AsyncMock()
    update = AsyncMock()
    monkeypatch.setattr(workflow, "insert_create_contact_person_if_missing", insert)
    monkeypatch.setattr(workflow, "update_create_contact_person_profile", update)
    runtime = _runtime()

    branch = await workflow._run_create_contact(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(person_action="create_if_missing"),
        runtime_variables=runtime,
        contact_row=None,
    )

    assert branch == "unchanged"
    insert.assert_not_awaited()
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_cria_pessoa_sem_canal_lista_ou_sessao_filha(monkeypatch) -> None:
    fetch = AsyncMock(return_value=None)
    created = _person(full_name="Nome Novo", extras={"segment": "premium"})
    insert = AsyncMock(return_value=created)
    update = AsyncMock()
    monkeypatch.setattr(workflow, "fetch_create_contact_person_by_identifier_for_update", fetch)
    monkeypatch.setattr(workflow, "insert_create_contact_person_if_missing", insert)
    monkeypatch.setattr(workflow, "update_create_contact_person_profile", update)
    runtime = _runtime()

    branch = await workflow._run_create_contact(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(),
        runtime_variables=runtime,
        contact_row=None,
    )

    assert branch == "created"
    insert.assert_awaited_once()
    update.assert_not_awaited()
    assert runtime["variables"]["customs"]["contact_action"]["changed_fields"] == [
        "full_name",
        "extra.segment",
    ]
    assert runtime["variables"]["contact"]["person_uuid"] == PERSON_UUID
    assert runtime["variables"]["contact"]["channel_address"] is None
    assert runtime["workflow_v2"]["person_adoption"]["source_component_kind"] == (
        "create_contact"
    )


@pytest.mark.asyncio
async def test_upsert_aceita_dropdown_serializado_e_parameters_em_lista(monkeypatch) -> None:
    created = _person(full_name="Nome Novo", extras={"segment": "premium"})
    monkeypatch.setattr(
        workflow,
        "fetch_create_contact_person_by_identifier_for_update",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        workflow,
        "insert_create_contact_person_if_missing",
        AsyncMock(return_value=created),
    )
    component = _component()
    component["parameters"] = [
        {"id": "person_action", "value": {"id": "upsert", "name": "Criar ou atualizar"}},
        {"id": "identifier", "value": "{{contact.identifier}}"},
        {"id": "enrichment_policy", "value": [{"id": "fill_missing", "name": "Preencher vazios"}]},
        {
            "id": "mapping",
            "value": {"full_name": "{{payload.name}}", "extra.segment": "{{payload.segment}}"},
        },
        {"id": "output_var", "value": "contact_action"},
    ]
    runtime = _runtime()

    branch = await workflow._run_create_contact(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=component,
        runtime_variables=runtime,
        contact_row=None,
    )

    assert branch == "created"
    assert runtime["variables"]["customs"]["contact_action"]["action"] == "created"


@pytest.mark.asyncio
async def test_upsert_atualiza_pessoa_existente_com_politica(monkeypatch) -> None:
    existing = _person(full_name="Nome Atual", extras={"segment": "basic"})
    updated = _person(full_name="Nome Novo", extras={"segment": "premium"})
    monkeypatch.setattr(
        workflow,
        "fetch_create_contact_person_by_identifier_for_update",
        AsyncMock(return_value=existing),
    )
    update = AsyncMock(return_value=updated)
    monkeypatch.setattr(workflow, "update_create_contact_person_profile", update)
    runtime = _runtime()

    branch = await workflow._run_create_contact(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(enrichment_policy="overwrite_non_null"),
        runtime_variables=runtime,
        contact_row=None,
    )

    assert branch == "updated"
    payload = update.await_args.kwargs["payload"]
    assert payload["full_name"] == "Nome Novo"
    assert payload["extras"]["segment"] == "premium"


@pytest.mark.asyncio
async def test_upsert_existente_com_mapping_vazio_preserva_e_segue_unchanged(monkeypatch) -> None:
    existing = _person(
        full_name="Nome Preservado",
        country="BR",
        state="SP",
        city="Campinas",
        extras={"origin": "preserved"},
    )
    monkeypatch.setattr(
        workflow,
        "fetch_create_contact_person_by_identifier_for_update",
        AsyncMock(return_value=existing),
    )
    update = AsyncMock()
    monkeypatch.setattr(workflow, "update_create_contact_person_profile", update)
    runtime = _runtime(name="", segment=None, city="", country=None, state="   ")

    branch = await workflow._run_create_contact(
        db_session=_NestedSession(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        component=_component(
            enrichment_policy="overwrite_non_null",
            mapping=[
                {"key": "full_name", "value": "{{payload.name}}"},
                {"key": "country", "value": "{{payload.country}}"},
                {"key": "state", "value": "{{payload.state}}"},
                {"key": "city", "value": "{{payload.city}}"},
            ],
        ),
        runtime_variables=runtime,
        contact_row=None,
    )

    assert branch == "unchanged"
    update.assert_not_awaited()
    assert runtime["variables"]["customs"]["contact_action"] == {
        "action": "unchanged",
        "person_uuid": PERSON_UUID,
        "identifier": "12345678901",
        "changed_fields": [],
    }


@pytest.mark.asyncio
async def test_upsert_novo_com_mapping_vazio_permanece_rejeitado(monkeypatch) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_create_contact_person_by_identifier_for_update",
        AsyncMock(return_value=None),
    )
    insert = AsyncMock()
    monkeypatch.setattr(workflow, "insert_create_contact_person_if_missing", insert)
    runtime = _runtime(name="", segment=None)

    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        await workflow._run_create_contact(
            db_session=_NestedSession(),  # type: ignore[arg-type]
            flow_uuid=FLOW_UUID,
            component=_component(
                enrichment_policy="overwrite_non_null",
                mapping=[{"key": "full_name", "value": "{{payload.name}}"}],
            ),
            runtime_variables=runtime,
            contact_row=None,
        )

    assert exc_info.value.code == "create_contact_empty_mapping"
    insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_envelope_is_rejected_in_runtime() -> None:
    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        await workflow._run_create_contact(
            db_session=_NestedSession(),  # type: ignore[arg-type]
            flow_uuid=FLOW_UUID,
            component={
                "ref_id": "legacy",
                "component_id": "create_contact",
                "parameters": {
                    "mapping": [
                        {"key": "identifier", "value": "12345678901"},
                        {"key": "address", "value": "5511999999999"},
                    ]
                },
            },
            runtime_variables=_runtime(),
            contact_row=None,
        )

    assert exc_info.value.code == "create_contact_invalid_person_action"


@pytest.mark.asyncio
async def test_persistence_failure_is_converted_to_component_error(monkeypatch) -> None:
    monkeypatch.setattr(
        workflow,
        "fetch_create_contact_person_by_identifier_for_update",
        AsyncMock(side_effect=RuntimeError("db unavailable")),
    )

    with pytest.raises(workflow.WorkflowExecutionError) as exc_info:
        await workflow._run_create_contact(
            db_session=_NestedSession(),  # type: ignore[arg-type]
            flow_uuid=FLOW_UUID,
            component=_component(),
            runtime_variables=_runtime(),
            contact_row=None,
        )

    assert exc_info.value.code == "create_contact_persistence_failed"


@pytest.mark.asyncio
async def test_execute_workflow_routes_create_contact_by_new_branch(monkeypatch) -> None:
    create_ref = "11111111-1111-1111-1111-111111111111"
    finish_ref = "22222222-2222-2222-2222-222222222222"
    definition = {
        "components": [
            {
                **_component(
                    person_action="update_current",
                    mapping=[{"key": "company", "value": "GOHP"}],
                ),
                "ref_id": create_ref,
            },
            {"ref_id": finish_ref, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [{"from": create_ref, "to": finish_ref, "branch": "updated"}],
    }
    runtime = _runtime()
    persisted: list[dict] = []

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class _Result:
        def scalar_one(self) -> bool:
            return True

    class _Session:
        def in_transaction(self) -> bool:
            return False

        def begin(self) -> _Transaction:
            return _Transaction()

        def begin_nested(self) -> _Transaction:
            return _Transaction()

        async def execute(self, *_args, **_kwargs) -> _Result:
            return _Result()

    monkeypatch.setattr(workflow, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(workflow, "fetch_flow_row", AsyncMock(return_value={"id": FLOW_UUID}))
    monkeypatch.setattr(
        workflow,
        "resolve_workflow_revision_for_session",
        AsyncMock(
            return_value=WorkflowRevisionResolution(
                revision={"id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "definition": definition},
                source="pinned",
                requested_revision_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
                failure_reason=None,
            )
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_session_workflow_state",
        AsyncMock(
            return_value={
                "uuid": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "state": 0,
                "runtime_variables": runtime,
                "last_card_uuid": None,
                "next_card_uuid": create_ref,
                "frozen_until": None,
            }
        ),
    )
    contact_row = {"contact_list_member_id": 10, "person_uuid": PERSON_UUID}
    monkeypatch.setattr(
        workflow,
        "fetch_contact_runtime_context_for_session",
        AsyncMock(return_value=contact_row),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_create_contact_person_by_uuid_for_update",
        AsyncMock(return_value=_person(company=None)),
    )
    monkeypatch.setattr(
        workflow,
        "update_create_contact_person_profile",
        AsyncMock(return_value=_person(company="GOHP")),
    )

    async def _replace(*_args, **kwargs) -> None:
        persisted.append(kwargs)

    monkeypatch.setattr(workflow, "replace_session_workflow_state", _replace)
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=123,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.last_card_uuid == finish_ref
    assert runtime["variables"]["customs"]["contact_action"]["action"] == "updated"
    assert any(item.get("next_card_uuid") == finish_ref for item in persisted)


@pytest.mark.asyncio
async def test_unbound_person_bootstrap_create_adopts_person_and_finishes(monkeypatch) -> None:
    create_ref = "31111111-1111-1111-1111-111111111111"
    finish_ref = "32222222-2222-2222-2222-222222222222"
    definition = {
        "components": [
            {
                **_component(identifier="{{payload.identifier}}"),
                "ref_id": create_ref,
            },
            {"ref_id": finish_ref, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [{"from": create_ref, "to": finish_ref, "branch": "created"}],
    }
    runtime = {
        "input_payload": {"session_scope": "person"},
        "workflow_v2": {"flow_id": FLOW_UUID, "next_card_cursor": create_ref},
        "variables": {
            "payload": {
                "identifier": "12345678901",
                "name": "Nome Novo",
                "segment": "premium",
            },
            "customs": {},
        },
    }
    persisted: list[dict] = []

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class _Result:
        def scalar_one(self) -> bool:
            return True

    class _Session:
        def in_transaction(self) -> bool:
            return False

        def begin(self) -> _Transaction:
            return _Transaction()

        def begin_nested(self) -> _Transaction:
            return _Transaction()

        async def execute(self, *_args, **_kwargs) -> _Result:
            return _Result()

    monkeypatch.setattr(workflow, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(workflow, "fetch_flow_row", AsyncMock(return_value={"id": FLOW_UUID}))
    monkeypatch.setattr(
        workflow,
        "resolve_workflow_revision_for_session",
        AsyncMock(
            return_value=WorkflowRevisionResolution(
                revision={
                    "id": "3ccccccc-cccc-cccc-cccc-cccccccccccc",
                    "definition": definition,
                },
                source="pinned",
                requested_revision_id="3ccccccc-cccc-cccc-cccc-cccccccccccc",
                failure_reason=None,
            )
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_session_workflow_state",
        AsyncMock(
            return_value={
                "uuid": "3ddddddd-dddd-dddd-dddd-dddddddddddd",
                "state": 0,
                "runtime_variables": runtime,
                "last_card_uuid": None,
                "next_card_uuid": create_ref,
                "frozen_until": None,
            }
        ),
    )
    fetch_context = AsyncMock(return_value=None)
    monkeypatch.setattr(
        workflow,
        "fetch_contact_runtime_context_for_session",
        fetch_context,
    )
    monkeypatch.setattr(
        workflow,
        "fetch_create_contact_person_by_identifier_for_update",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        workflow,
        "insert_create_contact_person_if_missing",
        AsyncMock(return_value=_person(full_name="Nome Novo", extras={"segment": "premium"})),
    )

    async def _replace(*_args, **kwargs) -> None:
        persisted.append(kwargs)

    monkeypatch.setattr(workflow, "replace_session_workflow_state", _replace)
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=301,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.executed_steps == 2
    assert fetch_context.await_count == 0
    assert runtime["workflow_v2"]["person_adoption"]["person_uuid"] == PERSON_UUID
    assert runtime["variables"]["contact"]["contact_list_member_id"] is None
    assert runtime["variables"]["contact"]["channel_address"] is None
    assert runtime["variables"]["system"]["contact"]["identifier"] == "12345678901"
    assert persisted[-1]["state"] == 3


@pytest.mark.asyncio
async def test_unbound_person_allows_membership_only_after_adoption(monkeypatch) -> None:
    create_ref = "41111111-1111-1111-1111-111111111111"
    membership_ref = "42222222-2222-2222-2222-222222222222"
    finish_ref = "43333333-3333-3333-3333-333333333333"
    definition = {
        "components": [
            {
                **_component(identifier="{{payload.identifier}}"),
                "ref_id": create_ref,
            },
            {
                "ref_id": membership_ref,
                "component_id": "source_list_membership",
                "parameters": {},
            },
            {"ref_id": finish_ref, "component_id": "finish_flow", "parameters": {}},
        ],
        "branches": [
            {"from": create_ref, "to": membership_ref, "branch": "created"},
            {"from": membership_ref, "to": finish_ref, "branch": "changed"},
        ],
    }
    runtime = {
        "input_payload": {"session_scope": "person"},
        "workflow_v2": {"flow_id": FLOW_UUID, "next_card_cursor": create_ref},
        "variables": {
            "payload": {
                "identifier": "12345678901",
                "name": "Nome Novo",
                "segment": "premium",
            },
            "customs": {},
        },
    }

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class _Result:
        def scalar_one(self) -> bool:
            return True

    class _Session:
        def in_transaction(self) -> bool:
            return False

        def begin(self) -> _Transaction:
            return _Transaction()

        def begin_nested(self) -> _Transaction:
            return _Transaction()

        async def execute(self, *_args, **_kwargs) -> _Result:
            return _Result()

    monkeypatch.setattr(workflow, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(workflow, "fetch_flow_row", AsyncMock(return_value={"id": FLOW_UUID}))
    monkeypatch.setattr(
        workflow,
        "resolve_workflow_revision_for_session",
        AsyncMock(
            return_value=WorkflowRevisionResolution(
                revision={
                    "id": "4ccccccc-cccc-cccc-cccc-cccccccccccc",
                    "definition": definition,
                },
                source="pinned",
                requested_revision_id="4ccccccc-cccc-cccc-cccc-cccccccccccc",
                failure_reason=None,
            )
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_session_workflow_state",
        AsyncMock(
            return_value={
                "uuid": "4ddddddd-dddd-dddd-dddd-dddddddddddd",
                "state": 0,
                "runtime_variables": runtime,
                "last_card_uuid": None,
                "next_card_uuid": create_ref,
                "frozen_until": None,
            }
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_create_contact_person_by_identifier_for_update",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        workflow,
        "insert_create_contact_person_if_missing",
        AsyncMock(return_value=_person(full_name="Nome Novo")),
    )
    run_membership = AsyncMock(return_value="changed")
    monkeypatch.setattr(workflow, "_run_source_list_membership", run_membership)
    monkeypatch.setattr(workflow, "replace_session_workflow_state", AsyncMock())
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=401,
    )

    assert result.stopped_reason == "finished_by_component"
    assert result.executed_steps == 3
    run_membership.assert_awaited_once()


@pytest.mark.asyncio
async def test_unbound_person_blocks_selector_without_materialized_member(monkeypatch) -> None:
    create_ref = "51111111-1111-1111-1111-111111111111"
    selector_ref = "52222222-2222-2222-2222-222222222222"
    definition = {
        "components": [
            {
                **_component(identifier="{{payload.identifier}}"),
                "ref_id": create_ref,
            },
            {
                "ref_id": selector_ref,
                "component_id": "select_contact_channel",
                "parameters": {},
            },
        ],
        "branches": [
            {"from": create_ref, "to": selector_ref, "branch": "created"},
        ],
    }
    runtime = {
        "input_payload": {"session_scope": "person"},
        "workflow_v2": {"flow_id": FLOW_UUID, "next_card_cursor": create_ref},
        "variables": {
            "payload": {
                "identifier": "12345678901",
                "name": "Nome Novo",
                "segment": "premium",
            },
            "customs": {},
        },
    }

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class _Result:
        def scalar_one(self) -> bool:
            return True

    class _Session:
        def in_transaction(self) -> bool:
            return False

        def begin(self) -> _Transaction:
            return _Transaction()

        def begin_nested(self) -> _Transaction:
            return _Transaction()

        async def execute(self, *_args, **_kwargs) -> _Result:
            return _Result()

    monkeypatch.setattr(workflow, "_read_enabled", lambda _settings: True)
    monkeypatch.setattr(workflow, "fetch_flow_row", AsyncMock(return_value={"id": FLOW_UUID}))
    monkeypatch.setattr(
        workflow,
        "resolve_workflow_revision_for_session",
        AsyncMock(
            return_value=WorkflowRevisionResolution(
                revision={
                    "id": "5ccccccc-cccc-cccc-cccc-cccccccccccc",
                    "definition": definition,
                },
                source="pinned",
                requested_revision_id="5ccccccc-cccc-cccc-cccc-cccccccccccc",
                failure_reason=None,
            )
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_session_workflow_state",
        AsyncMock(
            return_value={
                "uuid": "5ddddddd-dddd-dddd-dddd-dddddddddddd",
                "state": 0,
                "runtime_variables": runtime,
                "last_card_uuid": None,
                "next_card_uuid": create_ref,
                "frozen_until": None,
            }
        ),
    )
    monkeypatch.setattr(
        workflow,
        "fetch_create_contact_person_by_identifier_for_update",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        workflow,
        "insert_create_contact_person_if_missing",
        AsyncMock(return_value=_person(full_name="Nome Novo")),
    )
    run_selector = AsyncMock()
    monkeypatch.setattr(workflow, "_run_select_contact_channel", run_selector)
    replace = AsyncMock()
    monkeypatch.setattr(workflow, "replace_session_workflow_state", replace)
    monkeypatch.setattr(workflow, "persist_session_metrics", AsyncMock())

    result = await workflow.execute_workflow_m2_for_session(
        _Session(),  # type: ignore[arg-type]
        flow_uuid=FLOW_UUID,
        session_id=501,
    )

    assert result.stopped_reason == "contact_member_scope_not_found"
    assert result.executed_steps == 2
    assert result.next_card_uuid is None
    run_selector.assert_not_awaited()
    assert runtime["workflow_v2"]["terminal_failure"]["component_kind"] == (
        "select_contact_channel"
    )
    assert replace.await_args.kwargs["state"] == 3
