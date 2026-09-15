from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.dialer_supplier_v2_service import (
    DialerCycleRegistrationResult,
    DialerSupplierV2RegistrationError,
    build_dialer_cycle_intent,
)
from app.tasks import dialer_supplier_v2_tasks as tasks


WORKSPACE_UUID = "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
FLOW_UUID = "4e163399-e9a0-4335-895f-316c6a161299"
SESSION_UUID = "11111111-1111-4111-8111-111111111111"
REVISION_UUID = "22222222-2222-4222-8222-222222222222"
COMPONENT_REF_ID = "33333333-3333-4333-8333-333333333333"
CONTACT_LIST_ID = "44444444-4444-4444-8444-444444444444"
PROFILE_ID = "55555555-5555-4555-8555-555555555555"
CYCLE_ID = "66666666-6666-4666-8666-666666666666"


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "dialer_supplier_v2_enabled": True,
        "dialer_supplier_v2_workspace_allowlist": (WORKSPACE_UUID,),
        "dialer_supplier_v2_flow_allowlist": (FLOW_UUID,),
        "dialer_supplier_v2_max_attempts": 3,
        "dialer_supplier_v2_retry_backoff_seconds": 0.0,
        "dialer_supplier_v2_registration_lease_seconds": 120,
        "dialer_supplier_v2_reconcile_batch_size": 100,
        "celery_dialer_supplier_v2_queue": "orch_dialer_supplier_v2_test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _intent() -> dict:
    return build_dialer_cycle_intent(
        session_uuid=SESSION_UUID,
        flow_uuid=FLOW_UUID,
        flow_revision_id=REVISION_UUID,
        component_ref_id=COMPONENT_REF_ID,
        contact_list_id=CONTACT_LIST_ID,
        contact_list_member_id=71,
        dial_profile_id=PROFILE_ID,
    )


def _result() -> DialerCycleRegistrationResult:
    return DialerCycleRegistrationResult(
        cycle_id=CYCLE_ID,
        state="ready",
        replayed=False,
        dial_profile_revision_id="77777777-7777-4777-8777-777777777777",
        attempt_policy_id="88888888-8888-4888-8888-888888888888",
        attempt_limit_id="99999999-9999-4999-8999-999999999999",
        profile_snapshot_checksum="c" * 64,
        ready_at="2026-09-14T19:00:00-03:00",
    )


@pytest.mark.asyncio
async def test_registration_task_stores_only_safe_cycle_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    stored: dict[str, object] = {}
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings())

    async def _claim(**_kwargs):  # type: ignore[no-untyped-def]
        return {"status": "claimed", "intent": intent, "attempt": 1}

    async def _store_success(**kwargs):  # type: ignore[no-untyped-def]
        stored.update(kwargs)
        return True

    monkeypatch.setattr(tasks, "_claim_registration_attempt", _claim)
    monkeypatch.setattr(tasks, "register_dialer_cycle", lambda **_kwargs: _result())
    monkeypatch.setattr(tasks, "_store_registration_success", _store_success)

    result = await tasks._register_dialer_supplier_v2_cycle_task(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        session_id=71,
        attempt=1,
    )

    assert result == {"status": "ready", "cycle_id": CYCLE_ID}
    safe_result = stored["result"]
    assert isinstance(safe_result, dict)
    assert safe_result["cycle_id"] == CYCLE_ID
    assert "callback_token" not in safe_result


@pytest.mark.asyncio
async def test_registration_task_marks_transient_error_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    stored: dict[str, object] = {}
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings())

    async def _claim(**_kwargs):  # type: ignore[no-untyped-def]
        return {"status": "claimed", "intent": intent, "attempt": 1}

    async def _store_error(**kwargs):  # type: ignore[no-untyped-def]
        stored.update(kwargs)
        return True

    def _register(**_kwargs):  # type: ignore[no-untyped-def]
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_unavailable",
            "Indisponível.",
            retryable=True,
        )

    monkeypatch.setattr(tasks, "_claim_registration_attempt", _claim)
    monkeypatch.setattr(tasks, "register_dialer_cycle", _register)
    monkeypatch.setattr(tasks, "_store_registration_error", _store_error)

    result = await tasks._register_dialer_supplier_v2_cycle_task(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        session_id=71,
        attempt=1,
    )

    assert result["status"] == "retry"
    assert result["attempt"] == 1
    assert stored["will_retry"] is True


@pytest.mark.asyncio
async def test_registration_task_stops_retrying_permanent_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    stored: dict[str, object] = {}
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings())

    async def _claim(**_kwargs):  # type: ignore[no-untyped-def]
        return {"status": "claimed", "intent": intent, "attempt": 1}

    async def _store_error(**kwargs):  # type: ignore[no-untyped-def]
        stored.update(kwargs)
        return True

    def _register(**_kwargs):  # type: ignore[no-untyped-def]
        raise DialerSupplierV2RegistrationError(
            "contact_supplier_v2_profile_invalid",
            "Perfil inválido.",
            retryable=False,
            status_code=422,
        )

    monkeypatch.setattr(tasks, "_claim_registration_attempt", _claim)
    monkeypatch.setattr(tasks, "register_dialer_cycle", _register)
    monkeypatch.setattr(tasks, "_store_registration_error", _store_error)

    result = await tasks._register_dialer_supplier_v2_cycle_task(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        session_id=71,
        attempt=1,
    )

    assert result["status"] == "failed"
    assert stored["will_retry"] is False


@pytest.mark.asyncio
async def test_registration_task_makes_last_transient_attempt_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    stored: dict[str, object] = {}
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings())

    async def _claim(**_kwargs):  # type: ignore[no-untyped-def]
        return {"status": "claimed", "intent": intent, "attempt": 3}

    async def _store_error(**kwargs):  # type: ignore[no-untyped-def]
        stored.update(kwargs)
        return True

    def _register(**_kwargs):  # type: ignore[no-untyped-def]
        raise DialerSupplierV2RegistrationError(
            "dialer_supplier_v2_unavailable",
            "Indisponível.",
            retryable=True,
        )

    monkeypatch.setattr(tasks, "_claim_registration_attempt", _claim)
    monkeypatch.setattr(tasks, "register_dialer_cycle", _register)
    monkeypatch.setattr(tasks, "_store_registration_error", _store_error)

    result = await tasks._register_dialer_supplier_v2_cycle_task(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        session_id=71,
        attempt=3,
    )

    assert result["status"] == "failed"
    assert result["attempt"] == 3
    assert stored["will_retry"] is False


@pytest.mark.asyncio
async def test_registration_task_does_not_spend_retry_on_concurrent_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings())

    async def _claim(**_kwargs):  # type: ignore[no-untyped-def]
        return {"status": "locked"}

    monkeypatch.setattr(tasks, "_claim_registration_attempt", _claim)

    result = await tasks._register_dialer_supplier_v2_cycle_task(
        workspace_uuid=WORKSPACE_UUID,
        flow_uuid=FLOW_UUID,
        session_id=71,
        attempt=1,
    )

    assert result == {"status": "in_progress"}


class _Transaction:
    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_args):  # noqa: ANN204, ANN002
        return None


class _Mappings:
    def all(self) -> list[dict]:
        return [{"id": 71, "flow_uuid": FLOW_UUID, "attempts": 2}]


class _Rows:
    def mappings(self) -> _Mappings:
        return _Mappings()


class _Session:
    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_args):  # noqa: ANN204, ANN002
        return None

    def begin(self) -> _Transaction:
        return _Transaction()

    def in_transaction(self) -> bool:
        return False

    async def commit(self) -> None:
        return None

    async def execute(self, statement, _parameters=None):  # noqa: ANN001, ANN201
        if 'FROM "orch_sessions"' in str(statement):
            return _Rows()
        return object()


@pytest.mark.asyncio
async def test_reconciler_recovers_pending_intent_on_dedicated_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    enqueued: list[dict] = []
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "get_session_factory", lambda: (lambda: _Session()))
    monkeypatch.setattr(
        tasks,
        "list_completed_workspaces",
        lambda _session: _async_value(
            [
                {"workspace_uuid": WORKSPACE_UUID},
                {
                    "workspace_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                },
            ]
        ),
    )
    monkeypatch.setattr(
        tasks,
        "bind_workspace_context",
        lambda workspace_uuid: (workspace_uuid, f"ws_{workspace_uuid}"),
    )
    monkeypatch.setattr(
        tasks.register_dialer_supplier_v2_cycle_task,
        "apply_async",
        lambda **kwargs: enqueued.append(kwargs),
    )

    result = await tasks._reconcile_pending_dialer_supplier_v2_cycles_task()

    assert result == {"scanned": 1, "enqueued": 1}
    assert enqueued[0]["queue"] == "orch_dialer_supplier_v2_test"
    assert enqueued[0]["kwargs"]["session_id"] == 71
    assert enqueued[0]["kwargs"]["recovery_attempt"] == 3


async def _async_value(value):  # type: ignore[no-untyped-def]
    return value
