from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.services.workflow_m2_service import _run_source_list_membership


@pytest.mark.asyncio
async def test_membership_is_idempotent_in_real_postgres_without_shared_residue() -> None:
    person_uuid = str(uuid4())
    mailing_uuid = str(uuid4())
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        async with db_session.begin():
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE persons (
                        id bigint PRIMARY KEY,
                        uuid uuid NOT NULL UNIQUE,
                        identifier text NOT NULL UNIQUE,
                        full_name text,
                        company text,
                        gender text,
                        role text,
                        country text,
                        state text,
                        city text,
                        birthdate date,
                        primary_channel_type text,
                        primary_channel_value text,
                        primary_channel_label text,
                        channels jsonb,
                        extras jsonb,
                        last_contact_draft_id uuid,
                        last_source_list_id bigint,
                        last_mailing_id bigint,
                        last_seen_at timestamptz,
                        created_at timestamptz DEFAULT NOW(),
                        updated_at timestamptz DEFAULT NOW(),
                        merged_into_uuid uuid
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE source_lists (
                        id bigint PRIMARY KEY,
                        public_id uuid NOT NULL UNIQUE,
                        name text,
                        status text,
                        origin text,
                        rows_total bigint DEFAULT 0,
                        rows_processed bigint DEFAULT 0,
                        rows_without_channel bigint DEFAULT 0,
                        updated_at timestamptz DEFAULT NOW()
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE contact_drafts (
                        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                        mailing_id bigint,
                        mailing_row_id bigint,
                        full_name text,
                        identifier text,
                        company text,
                        gender text,
                        role text,
                        country text,
                        state text,
                        city text,
                        birthdate date,
                        validation_status text,
                        is_blacklisted boolean DEFAULT FALSE,
                        is_duplicate boolean DEFAULT FALSE,
                        extras jsonb,
                        created_at timestamptz DEFAULT NOW(),
                        updated_at timestamptz DEFAULT NOW()
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE source_list_contact_drafts (
                        source_list_id bigint NOT NULL,
                        contact_draft_id uuid NOT NULL UNIQUE,
                        created_at timestamptz DEFAULT NOW(),
                        updated_at timestamptz DEFAULT NOW()
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE contact_draft_channels (
                        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                        contact_draft_id uuid NOT NULL,
                        type text NOT NULL,
                        value text NOT NULL,
                        dialer_label text,
                        is_primary boolean DEFAULT FALSE,
                        is_valid boolean DEFAULT TRUE,
                        is_reachable boolean DEFAULT TRUE,
                        used_for_promotion boolean DEFAULT FALSE,
                        created_at timestamptz DEFAULT NOW(),
                        updated_at timestamptz DEFAULT NOW(),
                        UNIQUE (contact_draft_id, type, value)
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    CREATE TEMP TABLE flow_mailing_links (
                        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                        flow_id uuid NOT NULL,
                        mailing_id bigint NOT NULL,
                        contact_list_id uuid,
                        linked_at timestamptz DEFAULT NOW(),
                        unlinked_at timestamptz
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO persons (
                        id,
                        uuid,
                        identifier,
                        full_name,
                        channels,
                        extras
                    ) VALUES (
                        1,
                        CAST(:person_uuid AS uuid),
                        '12345678901',
                        'Pessoa Teste',
                        '[{"type":"phone","value":"21999999999","label":"principal"}]'::jsonb,
                        '{}'::jsonb
                    )
                    """
                ),
                {"person_uuid": person_uuid},
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO source_lists (id, public_id, name, status, origin)
                    VALUES (1139, CAST(:mailing_uuid AS uuid), 'Lista Teste', 'PROCESSED', 'api')
                    """
                ),
                {"mailing_uuid": mailing_uuid},
            )

            component = {
                "ref_id": "source-list-membership-db",
                "component_id": "source_list_membership",
                "parameters": {
                    "person_uuid": "{{contact.person_uuid}}",
                    "mailing_id": mailing_uuid,
                    "membership_state": "active",
                    "output_var": "membership_result",
                },
            }
            runtime = {
                "variables": {
                    "payload": {},
                    "customs": {},
                    "contact": {"person_uuid": person_uuid},
                }
            }

            first_branch = await _run_source_list_membership(
                db_session=db_session,
                flow_uuid=str(uuid4()),
                session_id=123,
                component=component,
                runtime_variables=runtime,
            )
            second_branch = await _run_source_list_membership(
                db_session=db_session,
                flow_uuid=str(uuid4()),
                session_id=123,
                component=component,
                runtime_variables=runtime,
            )

            membership_count = (
                await db_session.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM source_list_contact_drafts slcd
                        JOIN contact_drafts d ON d.id = slcd.contact_draft_id
                        WHERE slcd.source_list_id = 1139
                          AND d.identifier = '12345678901'
                        """
                    )
                )
            ).scalar_one()
            draft_count = (
                await db_session.execute(
                    text("SELECT COUNT(*) FROM contact_drafts WHERE identifier = '12345678901'")
                )
            ).scalar_one()
            channel_count = (
                await db_session.execute(text("SELECT COUNT(*) FROM contact_draft_channels"))
            ).scalar_one()
            source_stats = (
                await db_session.execute(
                    text(
                        """
                        SELECT rows_total, rows_processed, rows_without_channel
                        FROM source_lists
                        WHERE id = 1139
                        """
                    )
                )
            ).one()

            assert first_branch == "changed"
            assert second_branch == "unchanged"
            assert membership_count == 1
            assert draft_count == 1
            assert channel_count == 1
            assert tuple(source_stats) == (1, 1, 0)
            assert runtime["variables"]["customs"]["membership_result"]["action"] == (
                "unchanged"
            )
