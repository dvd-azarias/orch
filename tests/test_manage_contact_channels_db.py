from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.services.workflow_m2_service import _run_manage_contact_channels


def _runtime(person_uuid: str, identifier: str) -> dict:
    return {
        "variables": {
            "payload": {"phone": "+55 (11) 97562-0806"},
            "customs": {},
            "contact": {"person_uuid": person_uuid, "identifier": identifier},
        },
        "workflow_v2": {
            "person_adoption": {
                "status": "adopted",
                "person_uuid": person_uuid,
                "identifier": identifier,
            }
        },
    }


def _component(operation: str = "upsert") -> dict:
    return {
        "ref_id": "manage-contact-channels-db",
        "component_id": "manage_contact_channels",
        "parameters": {
            "person_uuid": "{{contact.person_uuid}}",
            "operation": operation,
            "channels": [
                {
                    "type": "voice",
                    "address": "{{payload.phone}}",
                    "label": "principal",
                    "priority": 1,
                    "is_primary": True,
                }
            ],
            "output_var": "contact_channels",
        },
    }


@pytest.mark.asyncio
async def test_manage_channels_is_idempotent_and_allows_same_address_for_two_people() -> None:
    first_person_uuid = str(uuid4())
    second_person_uuid = str(uuid4())
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
                        primary_channel_type text,
                        primary_channel_value text,
                        primary_channel_label text,
                        channels jsonb NOT NULL DEFAULT '[]'::jsonb,
                        last_seen_at timestamptz,
                        updated_at timestamptz DEFAULT NOW(),
                        merged_into_uuid uuid,
                        UNIQUE (primary_channel_type, primary_channel_value)
                    ) ON COMMIT DROP
                    """
                )
            )
            await db_session.execute(
                text(
                    """
                    INSERT INTO persons (id, uuid, identifier)
                    VALUES
                        (1, CAST(:first_uuid AS uuid), 'PERSON-ONE'),
                        (2, CAST(:second_uuid AS uuid), 'PERSON-TWO')
                    """
                ),
                {
                    "first_uuid": first_person_uuid,
                    "second_uuid": second_person_uuid,
                },
            )

            first_runtime = _runtime(first_person_uuid, "PERSON-ONE")
            second_runtime = _runtime(second_person_uuid, "PERSON-TWO")
            first = await _run_manage_contact_channels(
                db_session=db_session,
                flow_uuid=str(uuid4()),
                component=_component(),
                runtime_variables=first_runtime,
                contact_row=None,
            )
            second = await _run_manage_contact_channels(
                db_session=db_session,
                flow_uuid=str(uuid4()),
                component=_component(),
                runtime_variables=second_runtime,
                contact_row=None,
            )
            second_repeat = await _run_manage_contact_channels(
                db_session=db_session,
                flow_uuid=str(uuid4()),
                component=_component(),
                runtime_variables=second_runtime,
                contact_row=None,
            )

            rows = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            identifier,
                            primary_channel_value,
                            channels
                        FROM persons
                        ORDER BY id
                        """
                    )
                )
            ).mappings().all()

            assert first == "changed"
            assert second == "changed"
            assert second_repeat == "unchanged"
            assert [row["identifier"] for row in rows] == ["PERSON-ONE", "PERSON-TWO"]
            assert rows[0]["primary_channel_value"] == "11975620806"
            assert rows[1]["primary_channel_value"] is None
            assert rows[0]["channels"][0]["value"] == "11975620806"
            assert rows[1]["channels"][0]["value"] == "11975620806"
            assert (
                first_runtime["variables"]["customs"]["contact_channels"][
                    "primary_projection_applied"
                ]
                is True
            )
            assert (
                second_runtime["variables"]["customs"]["contact_channels"][
                    "primary_projection_applied"
                ]
                is False
            )

            deactivated = await _run_manage_contact_channels(
                db_session=db_session,
                flow_uuid=str(uuid4()),
                component=_component(operation="deactivate"),
                runtime_variables=first_runtime,
                contact_row=None,
            )
            second_projection_refresh = await _run_manage_contact_channels(
                db_session=db_session,
                flow_uuid=str(uuid4()),
                component=_component(),
                runtime_variables=second_runtime,
                contact_row=None,
            )

            final_rows = (
                await db_session.execute(
                    text(
                        """
                        SELECT identifier, primary_channel_value, channels
                        FROM persons
                        ORDER BY id
                        """
                    )
                )
            ).mappings().all()
            assert deactivated == "changed"
            assert second_projection_refresh == "changed"
            assert final_rows[0]["primary_channel_value"] is None
            assert final_rows[0]["channels"][0]["state"] == "inactive"
            assert final_rows[1]["primary_channel_value"] == "11975620806"
