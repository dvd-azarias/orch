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
                folder_path = EXCLUDED.folder_path,
                file_name = EXCLUDED.file_name,
                accepted_at = NOW(),
                task_id = NULL,
                enqueued_at = NULL,
                completed_at = NULL,
                last_error = NULL,
                updated_at = NOW()
            WHERE orch_fileapp_ingest_receipts.status IN ('failed', 'enqueue_failed')
               OR (
                    orch_fileapp_ingest_receipts.status = 'accepted'
                    AND orch_fileapp_ingest_receipts.updated_at < NOW() - INTERVAL '60 seconds'
                  )
               OR (
                    orch_fileapp_ingest_receipts.status = 'enqueued'
                    AND orch_fileapp_ingest_receipts.updated_at < NOW() - INTERVAL '5 minutes'
                  )
            RETURNING id, status, task_id::text AS task_id,
                      mailing_uuid::text AS mailing_uuid,
                      true AS should_enqueue
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
            SELECT id, status, task_id::text AS task_id,
                   mailing_uuid::text AS mailing_uuid,
                   false AS should_enqueue
            FROM orch_fileapp_ingest_receipts
            WHERE flow_uuid = CAST(:flow_uuid AS uuid)
              AND file_id = CAST(:file_id AS uuid)
            """
        ),
        {"flow_uuid": flow_uuid, "file_id": file_id},
    )
    return dict(existing.mappings().one())


async def get_fileapp_ingest_receipt_mailing_uuid(
    db_session: AsyncSession,
    *,
    receipt_id: int,
) -> str | None:
    result = await db_session.execute(
        text(
            """
            SELECT mailing_uuid::text
            FROM orch_fileapp_ingest_receipts
            WHERE id = :receipt_id
            """
        ),
        {"receipt_id": receipt_id},
    )
    value = result.scalar_one_or_none()
    return str(value) if value is not None else None


async def bind_fileapp_ingest_receipt_mailing_uuid(
    db_session: AsyncSession,
    *,
    receipt_id: int,
    mailing_uuid: str,
) -> str:
    result = await db_session.execute(
        text(
            """
            UPDATE orch_fileapp_ingest_receipts
            SET mailing_uuid = COALESCE(mailing_uuid, CAST(:mailing_uuid AS uuid)),
                updated_at = NOW()
            WHERE id = :receipt_id
              AND (
                    mailing_uuid IS NULL
                    OR mailing_uuid = CAST(:mailing_uuid AS uuid)
                  )
            RETURNING mailing_uuid::text
            """
        ),
        {"receipt_id": receipt_id, "mailing_uuid": mailing_uuid},
    )
    bound = result.scalar_one_or_none()
    if bound is not None:
        return str(bound)

    existing = await get_fileapp_ingest_receipt_mailing_uuid(
        db_session,
        receipt_id=receipt_id,
    )
    if existing is None:
        raise LookupError(f"FileApp ingest receipt {receipt_id} não encontrado.")
    raise ValueError(
        f"FileApp ingest receipt {receipt_id} já está vinculado ao mailing {existing}."
    )


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
              AND status IN ('accepted', 'enqueued')
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


async def recover_fileapp_ingest_receipt_terminal_status(
    db_session: AsyncSession,
    *,
    receipt_id: int,
    status: str,
    error: str | None = None,
) -> bool:
    """Persist a delayed terminal state without overwriting a newer terminal decision."""
    if status not in {"completed", "failed"}:
        raise ValueError("Receipt recovery accepts only completed or failed status.")

    result = await db_session.execute(
        text(
            """
            UPDATE orch_fileapp_ingest_receipts
            SET status = :status,
                completed_at = CASE
                    WHEN :status = 'completed' THEN COALESCE(completed_at, NOW())
                    ELSE completed_at
                END,
                last_error = :error,
                updated_at = NOW()
            WHERE id = :receipt_id
              AND (
                    status IN ('accepted', 'enqueued', 'processing')
                    OR status = :status
                  )
            RETURNING id
            """
        ),
        {"receipt_id": receipt_id, "status": status, "error": error},
    )
    return result.scalar_one_or_none() is not None
