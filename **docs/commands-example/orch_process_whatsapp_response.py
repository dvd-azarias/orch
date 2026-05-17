from __future__ import annotations

from typing import Any, Dict, Optional

from app.commands.base import CommandHandler, ExecResult


def _extract_status(state: Dict[str, Any]) -> Optional[str]:
    variables = state.get("variables") or {}
    system = variables.get("system") or {}
    inp = system.get("input") or {}
    message = inp.get("message") if isinstance(inp, dict) else {}
    if isinstance(message, dict):
        status = message.get("status")
        if isinstance(status, str) and status.strip():
            return status.strip().lower()
    if isinstance(inp, dict):
        status = inp.get("status")
        if isinstance(status, str) and status.strip():
            return status.strip().lower()
    return None


class OrchProcessWhatsappResponseHandler(CommandHandler):
    component_id = "process_whatsapp_response"

    def execute(
        self,
        *,
        flow,
        state: Dict[str, Any],
        cmd,
        session_id: str,
        helpers: Dict[str, Any],
    ) -> ExecResult:
        status = _extract_status(state) or ""
        status = status.lower()
        # mapeamento padrão meta: sent, delivered, read, failed
        branch_map = {
            "sent": "sent",
            "delivered": "delivered",
            "read": "read",
            "readed": "read",
            "failed": "failed",
            "limit_reached": "limite_atingido",
            "limite_atingido": "limite_atingido",
            "break_violated": "intervalo_violado",
            "intervalo_violado": "intervalo_violado",
        }
        chosen_branch = branch_map.get(status)

        to_ref: Optional[str] = None
        branch_to = helpers.get("branch_to")
        if callable(branch_to) and chosen_branch:
            to_ref = branch_to(cmd.ref_id, chosen_branch)

        if not to_ref:
            next_ref = helpers.get("next_ref")
            if callable(next_ref):
                try:
                    to_ref = next_ref(cmd.ref_id)
                except Exception:
                    to_ref = None

        return ExecResult(continue_loop=True, to_ref=to_ref, finished=False)
