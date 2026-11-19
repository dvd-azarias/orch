from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.repositories.orch_fileapp_ingest_receipts_repository import (
    bind_fileapp_ingest_receipt_mailing_uuid,
    claim_fileapp_ingest_receipt,
    mark_fileapp_ingest_receipt_enqueued,
    recover_fileapp_ingest_receipt_terminal_status,
)


class _Mappings:
    def one_or_none(self):
        return {"id": 7, "status": "accepted", "task_id": None, "should_enqueue": True}


class _Result:
    def mappings(self):
        return _Mappings()


class _ScalarResult:
    def __init__(self, value):  # type: ignore[no-untyped-def]
        self.value = value

    def scalar_one_or_none(self):  # type: ignore[no-untyped-def]
        return self.value


class _ScalarSequenceSession:
    def __init__(self, values):  # type: ignore[no-untyped-def]
        self.values = iter(values)

    async def execute(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return _ScalarResult(next(self.values))


@pytest.mark.asyncio
async def test_claim_reopens_stale_accepted_and_clears_old_task_correlation() -> None:
    session = AsyncMock()
    session.execute.return_value = _Result()

    result = await claim_fileapp_ingest_receipt(
        session,
        flow_uuid="706c6fef-85f2-4276-bcfd-eb28f75acde2",
        file_id="8ea2b45d-85e6-442c-b365-9355ae2cc2b8",
        folder_path="monitoramento/upload",
        file_name="critical.csv",
        ingest_origin="rescue",
    )

    query = str(session.execute.await_args.args[0])
    assert "status = 'accepted'" in query
    assert "INTERVAL '60 seconds'" in query
    assert "status = 'enqueued'" in query
    assert "INTERVAL '5 minutes'" in query
    assert "status = 'processing'" not in query
    assert "INTERVAL '30 minutes'" not in query
    assert "task_id = NULL" in query
    assert "enqueued_at = NULL" in query
    assert "mailing_uuid = NULL" not in query
    assert "mailing_uuid::text AS mailing_uuid" in query
    assert result["should_enqueue"] is True


@pytest.mark.asyncio
async def test_terminal_recovery_only_updates_open_or_same_terminal_state() -> None:
    session = AsyncMock()
    session.execute.return_value = _ScalarResult(77)

    updated = await recover_fileapp_ingest_receipt_terminal_status(
        session,
        receipt_id=77,
        status="completed",
    )

    query = str(session.execute.await_args.args[0])
    assert "status IN ('accepted', 'enqueued', 'processing')" in query
    assert "OR status = :status" in query
    assert updated is True


@pytest.mark.asyncio
async def test_mark_enqueued_never_regresses_processing_or_terminal_receipt() -> None:
    session = AsyncMock()

    await mark_fileapp_ingest_receipt_enqueued(
        session,
        receipt_id=77,
        task_id="11111111-1111-1111-1111-111111111111",
    )

    query = str(session.execute.await_args.args[0])
    assert "status IN ('accepted', 'enqueued')" in query
    assert "status = 'processing'" not in query


@pytest.mark.asyncio
async def test_terminal_recovery_rejects_non_terminal_state() -> None:
    session = AsyncMock()

    with pytest.raises(ValueError, match="only completed or failed"):
        await recover_fileapp_ingest_receipt_terminal_status(
            session,
            receipt_id=77,
            status="processing",
        )

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_bind_receipt_mailing_is_idempotent_for_same_uuid() -> None:
    session = _ScalarSequenceSession(["11111111-1111-1111-1111-111111111111"])

    bound = await bind_fileapp_ingest_receipt_mailing_uuid(
        session,  # type: ignore[arg-type]
        receipt_id=77,
        mailing_uuid="11111111-1111-1111-1111-111111111111",
    )

    assert bound == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_bind_receipt_mailing_rejects_conflicting_uuid() -> None:
    session = _ScalarSequenceSession(
        [None, "11111111-1111-1111-1111-111111111111"]
    )

    with pytest.raises(ValueError, match="já está vinculado"):
        await bind_fileapp_ingest_receipt_mailing_uuid(
            session,  # type: ignore[arg-type]
            receipt_id=77,
            mailing_uuid="22222222-2222-2222-2222-222222222222",
        )
