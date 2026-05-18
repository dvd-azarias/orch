from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.phone_normalizer import normalize_phone_to_canonical_ani


async def register_whatsapp_limit_event(
    db_session: AsyncSession,
    *,
    phone: str,
    allowed_limit: int,
) -> dict[str, Any]:
    canonical_phone = str(normalize_phone_to_canonical_ani(phone) or "").strip()
    if not canonical_phone:
        raise ValueError("phone inválido para limite de WhatsApp.")

    lock_key = f"whatsapp-limit|{canonical_phone}"
    await db_session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": lock_key},
    )
    active_rows = await db_session.execute(
        text(
            """
            SELECT id, phone
            FROM orch_whatsapp_limits
            WHERE in_use = TRUE
            """
        )
    )
    deactivate_ids: list[int] = []
    for row in active_rows.mappings().all():
        current_phone = str(row["phone"] or "").strip()
        if str(normalize_phone_to_canonical_ani(current_phone) or "").strip() == canonical_phone:
            deactivate_ids.append(int(row["id"]))

    if deactivate_ids:
        await db_session.execute(
            text(
                """
                UPDATE orch_whatsapp_limits
                SET
                    in_use = FALSE,
                    updated_at = NOW()
                WHERE id = ANY(CAST(:deactivate_ids AS BIGINT[]))
                """
            ),
            {"deactivate_ids": deactivate_ids},
        )

    result = await db_session.execute(
        text(
            """
            INSERT INTO orch_whatsapp_limits (
                phone,
                allowed_limit,
                received_from_meta_at,
                in_use,
                created_at,
                updated_at
            )
            VALUES (
                :phone,
                :allowed_limit,
                NOW(),
                TRUE,
                NOW(),
                NOW()
            )
            RETURNING
                id,
                phone,
                allowed_limit,
                received_from_meta_at,
                in_use,
                created_at,
                updated_at
            """
        ),
        {
            "phone": canonical_phone,
            "allowed_limit": allowed_limit,
        },
    )
    return dict(result.mappings().one())
