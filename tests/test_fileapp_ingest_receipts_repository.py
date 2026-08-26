from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.repositories.orch_fileapp_ingest_receipts_repository import claim_fileapp_ingest_receipt


class _Mappings:
    def one_or_none(self):
        return {"id": 7, "status": "accepted", "task_id": None, "should_enqueue": True}


class _Result:
    def mappings(self):
        return _Mappings()


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
    assert "task_id = NULL" in query
    assert "enqueued_at = NULL" in query
    assert result["should_enqueue"] is True
