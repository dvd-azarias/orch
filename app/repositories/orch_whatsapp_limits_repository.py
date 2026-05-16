from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def register_whatsapp_limit_event(
    db_session: AsyncSession,
    *,
    phone: str,
    allowed_limit: int,
) -> dict[str, Any]:
    lock_key = f"whatsapp-limit|{phone}"
    await db_session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": lock_key},
    )
    await db_session.execute(
        text(
            """
            UPDATE orch_whatsapp_limits
            SET
                in_use = FALSE,
                updated_at = NOW()
            WHERE phone = :phone
              AND in_use = TRUE
            """
        ),
        {"phone": phone},
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
            "phone": phone,
            "allowed_limit": allowed_limit,
        },
    )
    return dict(result.mappings().one())
