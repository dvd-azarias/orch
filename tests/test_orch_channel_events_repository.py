from __future__ import annotations

import pytest

from app.repositories.orch_channel_events_repository import mark_pending_channel_events_processed


class _Result:
    rowcount = 2


class _Session:
    def __init__(self) -> None:
        self.statement = ""
        self.parameters: dict = {}

    async def execute(self, statement, parameters):  # noqa: ANN001
        self.statement = str(statement)
        self.parameters = parameters
        return _Result()


@pytest.mark.asyncio
async def test_mark_pending_channel_events_processed_is_scoped_to_session_and_channel() -> None:
    session = _Session()

    updated = await mark_pending_channel_events_processed(
        session,  # type: ignore[arg-type]
        session_id=6945,
        channel="dialer",
    )

    assert updated == 2
    assert "processed_at = NOW()" in session.statement
    assert "session_id = :session_id" in session.statement
    assert "channel = :channel" in session.statement
    assert "processed_at IS NULL" in session.statement
    assert session.parameters == {"session_id": 6945, "channel": "dialer"}
