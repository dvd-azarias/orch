from __future__ import annotations

from typing import Any, Dict
import logging

from app.commands.base import CommandHandler, ExecResult
from app.services import orchestrator_member


class OrchSendWithWhatsappHandler(CommandHandler):
    component_id = "send_with_whatsapp"

    def execute(
        self,
        *,
        flow,
        state: Dict[str, Any],
        cmd,
        session_id: str,
        helpers: Dict[str, Any],
    ) -> ExecResult:
        return _execute_blocking_send(
            actuator_value="whatsapp",
            flow=flow,
            state=state,
            cmd=cmd,
            helpers=helpers,
        )


def _execute_blocking_send(
    *, actuator_value: str, flow, state: Dict[str, Any], cmd, helpers: Dict[str, Any]
) -> ExecResult:
    variables = state.get("variables") or {}
    system_vars = variables.get("system") or {}
    contact = system_vars.get("contact") or {}

    member_id = contact.get("contact_list_member_id") or contact.get("contact_list_member") or contact.get("member_id")
    workspace_uuid = system_vars.get("workspace_uuid")

    if (not member_id) and workspace_uuid:
        selectors = {
            "id": contact.get("id") or contact.get("member_id") or contact.get("contact_list_member_id"),
            "contact_identifier": contact.get("identifier") or contact.get("contact_identifier"),
            "contact_id": contact.get("contact_id"),
            "contact_channel_address": contact.get("channel_address") or contact.get("contact_channel_address"),
        }
        try:
            row, _ = orchestrator_member.fetch_member(str(workspace_uuid), selectors)
            member_id = row.get("id")
            if member_id:
                contact["contact_list_member_id"] = member_id
                system_vars["contact"] = contact
                variables["system"] = system_vars
        except Exception:
            logging.getLogger("target_core.orch_send_with_whatsapp").debug(
                "orch_send_with_whatsapp.fallback_fetch_failed",
                exc_info=True,
                extra={"workspace_uuid": workspace_uuid, "selectors": selectors},
            )

    logger = logging.getLogger("target_core.orch_send_with_whatsapp")
    next_ref = _resolve_next_ref(flow, cmd.ref_id, helpers)

    if workspace_uuid and member_id:
        orchestrator_member.update_actuator_and_cursor(
            str(workspace_uuid),
            int(member_id),
            linked_actuator=actuator_value,
            last_ref_id=str(next_ref) if next_ref else None,
        )
    else:
        logger.debug(
            "orch_send_with_whatsapp.skip_update",
            extra={"workspace_uuid": workspace_uuid, "member_id": member_id, "next_ref": next_ref},
        )
    logger.info(
        "orch_send_with_whatsapp.exit",
        extra={"workspace_uuid": workspace_uuid, "member_id": member_id, "next_ref": next_ref},
    )

    return ExecResult(continue_loop=False, to_ref=next_ref, finished=False)


def _resolve_next_ref(flow, current_ref: str, helpers: Dict[str, Any]):
    branch_to = helpers.get("branch_to")
    if callable(branch_to):
        branch_next = branch_to(current_ref, "proximo")
        if branch_next:
            return branch_next

    outgoing = flow.outgoing(current_ref) if hasattr(flow, "outgoing") else []
    if len(outgoing) == 1:
        to_candidate = outgoing[0].get("to") if isinstance(outgoing[0], dict) else None
        if to_candidate:
            return to_candidate

    next_ref = helpers.get("next_ref")
    if callable(next_ref):
        try:
            return next_ref(current_ref)
        except Exception:
            return None
    return None
