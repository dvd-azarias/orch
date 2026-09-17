from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.repositories.identidade_person_repository import (
    resolve_source_list_from_session_origin,
)
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


@pytest.mark.asyncio
async def test_session_origin_resolves_only_the_active_materialized_source_list() -> None:
    flow_uuid = str(uuid4())
    person_uuid = str(uuid4())
    mailing_uuid = str(uuid4())
    contact_list_id = str(uuid4())
    session_factory = get_session_factory()

    async with session_factory() as db_session:
        async with db_session.begin():
            ddl_statements = (
                """
                    CREATE TEMP TABLE source_lists (
                        id bigint PRIMARY KEY,
                        public_id uuid NOT NULL UNIQUE,
                        name text,
                        status text,
                        origin text
                    ) ON COMMIT DROP
                """,
                """
                    CREATE TEMP TABLE orch_sessions (
                        id bigint PRIMARY KEY,
                        flow_uuid uuid NOT NULL,
                        runtime_variables jsonb NOT NULL
                    ) ON COMMIT DROP
                """,
                """
                    CREATE TEMP TABLE contact_list_members (
                        id bigint PRIMARY KEY,
                        mailing_id bigint NOT NULL,
                        contact_list_id uuid NOT NULL,
                        person_uuid uuid NOT NULL,
                        deleted_at timestamptz
                    ) ON COMMIT DROP
                """,
                """
                    CREATE TEMP TABLE flow_mailing_links (
                        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                        flow_id uuid NOT NULL,
                        mailing_id bigint NOT NULL,
                        contact_list_id uuid NOT NULL,
                        unlinked_at timestamptz
                    ) ON COMMIT DROP
                """,
            )
            for ddl_statement in ddl_statements:
                await db_session.execute(text(ddl_statement))

            insert_parameters = {
                "flow_uuid": flow_uuid,
                "person_uuid": person_uuid,
                "mailing_uuid": mailing_uuid,
                "contact_list_id": contact_list_id,
            }
            await db_session.execute(
                text(
                    """
                    INSERT INTO source_lists (id, public_id, name, status, origin)
                    VALUES (1139, CAST(:mailing_uuid AS uuid), 'Lista de origem', 'PROCESSED', 'api')
                    """
                ),
                insert_parameters,
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO contact_list_members (
                        id, mailing_id, contact_list_id, person_uuid
                    ) VALUES (
                        77, 1139, CAST(:contact_list_id AS uuid), CAST(:person_uuid AS uuid)
                    )
                    """
                ),
                insert_parameters,
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO flow_mailing_links (
                        flow_id, mailing_id, contact_list_id
                    ) VALUES (
                        CAST(:flow_uuid AS uuid), 1139, CAST(:contact_list_id AS uuid)
                    )
                    """
                ),
                insert_parameters,
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO orch_sessions (id, flow_uuid, runtime_variables)
                    VALUES (
                        123,
                        CAST(:flow_uuid AS uuid),
                        jsonb_build_object(
                            'input_payload',
                            jsonb_build_object(
                                'contact_list_member_id', 77,
                                'contact_list_id', CAST(:contact_list_id AS text),
                                'mailing_id', 1139,
                                'session_scope', 'person'
                            )
                        )
                    )
                    """
                ),
                insert_parameters,
            )

            resolved = await resolve_source_list_from_session_origin(
                db_session,
                flow_uuid=flow_uuid,
                session_id=123,
                source_list_id=1139,
                person_uuid=person_uuid,
            )

            assert resolved == {
                "id": 1139,
                "public_id": mailing_uuid,
                "name": "Lista de origem",
                "status": "PROCESSED",
                "origin": "api",
            }

            await db_session.execute(
                text("UPDATE flow_mailing_links SET unlinked_at = NOW()")
            )
            blocked = await resolve_source_list_from_session_origin(
                db_session,
                flow_uuid=flow_uuid,
                session_id=123,
                source_list_id=1139,
                person_uuid=person_uuid,
            )
            assert blocked is None
