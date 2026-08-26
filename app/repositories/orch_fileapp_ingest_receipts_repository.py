from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def claim_fileapp_ingest_receipt(
    db_session: AsyncSession,
    *,
    flow_uuid: str,
    file_id: str,
    folder_path: str,
    file_name: str,
    ingest_origin: str,
) -> dict[str, Any]:
    result = await db_session.execute(
        text(
            """
            INSERT INTO orch_fileapp_ingest_receipts (
                flow_uuid, file_id, folder_path, file_name, ingest_origin, status, accepted_at
            ) VALUES (
                CAST(:flow_uuid AS uuid), CAST(:file_id AS uuid), :folder_path, :file_name,
                :ingest_origin, 'accepted', NOW()
            )
            ON CONFLICT (flow_uuid, file_id) DO UPDATE
            SET status = 'accepted',
                ingest_origin = EXCLUDED.ingest_origin,
                accepted_at = NOW(),
                last_error = NULL,
                updated_at = NOW()
            WHERE orch_fileapp_ingest_receipts.status IN ('failed', 'enqueue_failed')
            RETURNING id, status, task_id::text AS task_id, (xmax = 0) AS created
            """
        ),
        {
            "flow_uuid": flow_uuid,
            "file_id": file_id,
            "folder_path": folder_path,
            "file_name": file_name,
            "ingest_origin": ingest_origin,
        },
    )
    row = result.mappings().one_or_none()
    if row is not None:
        return dict(row)

    existing = await db_session.execute(
        text(
            """
            SELECT id, status, task_id::text AS task_id, false AS created
            FROM orch_fileapp_ingest_receipts
            WHERE flow_uuid = CAST(:flow_uuid AS uuid)
              AND file_id = CAST(:file_id AS uuid)
            """
        ),
        {"flow_uuid": flow_uuid, "file_id": file_id},
    )
    return dict(existing.mappings().one())


async def mark_fileapp_ingest_receipt_enqueued(
    db_session: AsyncSession,
    *,
    receipt_id: int,
    task_id: str,
) -> None:
    await db_session.execute(
        text(
            """
            UPDATE orch_fileapp_ingest_receipts
            SET status = 'enqueued', task_id = CAST(:task_id AS uuid), enqueued_at = NOW(), updated_at = NOW()
            WHERE id = :receipt_id
            """
        ),
        {"receipt_id": receipt_id, "task_id": task_id},
    )


async def mark_fileapp_ingest_receipt_status(
    db_session: AsyncSession,
    *,
    receipt_id: int,
    status: str,
    error: str | None = None,
) -> None:
    await db_session.execute(
        text(
            """
            UPDATE orch_fileapp_ingest_receipts
            SET status = :status,
                completed_at = CASE WHEN :status = 'completed' THEN NOW() ELSE completed_at END,
                last_error = :error,
                updated_at = NOW()
            WHERE id = :receipt_id
            """
        ),
        {"receipt_id": receipt_id, "status": status, "error": error},
    )
