from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.commands.base import BatchEntry, CommandHandler, ExecResult

import logging
import time

LOGGER = logging.getLogger("target_core.commands.wait")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class WaitHandler(CommandHandler):
    component_id = "wait"

    def execute(
        self,
        *,
        flow,
        state: Dict[str, Any],
        cmd,
        session_id: str,
        helpers: Dict[str, Any],
    ) -> ExecResult:
        engine = helpers.get("engine")
        async_wait_enabled = getattr(engine, "async_wait_enabled", True) if engine is not None else True

        def _resolve_next_ref(default: Optional[str] = None) -> Optional[str]:
            if default:
                return default
            branch_to = helpers.get("branch_to")
            if callable(branch_to):
                try:
                    candidate = branch_to(cmd.ref_id, "proximo")
                    if candidate:
                        return candidate
                except Exception:
                    pass
            next_ref_fn = helpers.get("next_ref")
            if callable(next_ref_fn):
                try:
                    return next_ref_fn(cmd.ref_id)
                except Exception:
                    return None
            return None
        wait_state = state.get("wait_state")
        now = _utcnow()

        if isinstance(wait_state, dict) and wait_state.get("ref_id") == cmd.ref_id:
            resume_at = _parse_iso_datetime(wait_state.get("resume_at"))
            entered_at = _parse_iso_datetime(wait_state.get("entered_at"))
            duration_ms = int(wait_state.get("duration_ms") or 0)
            previous_next_ref = wait_state.get("next_ref_id")

            if not async_wait_enabled:
                next_ref = _resolve_next_ref(previous_next_ref)
                state.pop("wait_state", None)
                state["awaiting_blocking"] = False
                state["awaiting_ref_id"] = None
                state["cursor_ref_id"] = next_ref
                exit_ts = now
                if entered_at:
                    exit_ts = max(now, entered_at)
                trace = {
                    "momento_entrada": entered_at.isoformat() if entered_at else None,
                    "momento_saida": exit_ts.isoformat(),
                    "espera_ms": duration_ms,
                    "espera_real_ms": int((exit_ts - entered_at).total_seconds() * 1000) if entered_at else duration_ms,
                }
                batch = BatchEntry(selected="parameters.tempo", trace=trace)
                if engine is not None:
                    engine.store.set(session_id, state)
                LOGGER.info(
                    "wait.completed_sync",
                    extra={
                        "session_id": session_id,
                        "ref_id": cmd.ref_id,
                        "espera_ms": duration_ms,
                        "next_ref": next_ref,
                    },
                )
                return ExecResult(continue_loop=True, to_ref=next_ref, batch_entry=batch)

            if resume_at is None or entered_at is None:
                state.pop("wait_state", None)
                state["awaiting_blocking"] = False
                state["awaiting_ref_id"] = None
                next_ref = _resolve_next_ref(previous_next_ref)
                state["cursor_ref_id"] = next_ref
                if engine is not None:
                    engine.store.set(session_id, state)
                trace = {
                    "momento_entrada": entered_at.isoformat() if entered_at else None,
                    "momento_saida": now.isoformat(),
                    "espera_ms": duration_ms,
                    "espera_real_ms": duration_ms,
                }
                batch = BatchEntry(selected="parameters.tempo", trace=trace)
                return ExecResult(continue_loop=True, to_ref=next_ref, batch_entry=batch)

            if now < resume_at:
                # Ainda aguardando; não prossegue e mantém o estado.
                wait_state["scheduled"] = False
                wait_state.pop("scheduled_at", None)
                state["wait_state"] = wait_state
                pending_trace = {
                    "momento_entrada": entered_at.isoformat(),
                    "momento_saida_prevista": resume_at.isoformat(),
                    "espera_ms": duration_ms,
                    "aguardando_ms": int((resume_at - now).total_seconds() * 1000),
                }
                batch = BatchEntry(selected="parameters.tempo", trace=pending_trace)
                if engine is not None:
                    engine.store.set(session_id, state)
                LOGGER.debug(
                    "wait.pending",
                    extra={
                        "session_id": session_id,
                        "ref_id": cmd.ref_id,
                        "resume_at": resume_at.isoformat(),
                        "remaining_ms": int((resume_at - now).total_seconds() * 1000),
                    },
                )
                return ExecResult(continue_loop=False, batch_entry=batch)

            actual_ms = int((now - entered_at).total_seconds() * 1000)
            next_ref = _resolve_next_ref(previous_next_ref)

            state.pop("wait_state", None)
            state["awaiting_blocking"] = False
            state["awaiting_ref_id"] = None
            state["cursor_ref_id"] = next_ref

            trace = {
                "momento_entrada": entered_at.isoformat(),
                "momento_saida": now.isoformat(),
                "espera_ms": duration_ms,
                "espera_real_ms": actual_ms,
            }

            batch = BatchEntry(selected="parameters.tempo", trace=trace)
            if engine is not None:
                engine.store.set(session_id, state)
            LOGGER.info(
                "wait.completed",
                extra={
                    "session_id": session_id,
                    "ref_id": cmd.ref_id,
                    "espera_ms": duration_ms,
                    "espera_real_ms": actual_ms,
                    "next_ref": next_ref,
                },
            )
            return ExecResult(continue_loop=True, to_ref=next_ref, batch_entry=batch)

        params = cmd.parameters or {}
        try:
            wait_ms = int(params.get("tempo") or params.get("tempo_ms") or 0)
        except (TypeError, ValueError):
            wait_ms = 0
        wait_ms = max(wait_ms, 0)

        entered_at = now
        resume_at = now + timedelta(milliseconds=wait_ms)

        next_ref = _resolve_next_ref()

        if not async_wait_enabled:
            if wait_ms > 0:
                time.sleep(wait_ms / 1000.0)
            exit_ts = entered_at + timedelta(milliseconds=wait_ms)
            state["cursor_ref_id"] = next_ref
            state["awaiting_blocking"] = False
            state["awaiting_ref_id"] = None
            state.pop("wait_state", None)
            trace = {
                "momento_entrada": entered_at.isoformat(),
                "momento_saida": exit_ts.isoformat(),
                "espera_ms": wait_ms,
                "espera_real_ms": wait_ms,
            }
            batch = BatchEntry(selected="parameters.tempo", trace=trace)
            if engine is not None:
                engine.store.set(session_id, state)
            LOGGER.info(
                "wait.completed_sync",
                extra={
                    "session_id": session_id,
                    "ref_id": cmd.ref_id,
                    "espera_ms": wait_ms,
                    "next_ref": next_ref,
                },
            )
            return ExecResult(continue_loop=True, to_ref=next_ref, batch_entry=batch)

        if wait_ms == 0:
            state.pop("wait_state", None)
            state["awaiting_blocking"] = False
            state["awaiting_ref_id"] = None
            state["cursor_ref_id"] = next_ref
            trace = {
                "momento_entrada": entered_at.isoformat(),
                "momento_saida": entered_at.isoformat(),
                "espera_ms": 0,
                "espera_real_ms": 0,
            }
            batch = BatchEntry(selected="parameters.tempo", trace=trace)
            if engine is not None:
                engine.store.set(session_id, state)
            LOGGER.debug(
                "wait.skip",
                extra={"session_id": session_id, "ref_id": cmd.ref_id},
            )
            return ExecResult(continue_loop=True, to_ref=next_ref, batch_entry=batch)

        wait_state_payload = {
            "ref_id": cmd.ref_id,
            "status": "waiting",
            "entered_at": entered_at.isoformat(),
            "resume_at": resume_at.isoformat(),
            "duration_ms": wait_ms,
            "next_ref_id": next_ref,
            "scheduled": False,
        }

        state["wait_state"] = wait_state_payload
        state["awaiting_blocking"] = True
        state["awaiting_ref_id"] = cmd.ref_id
        state["cursor_ref_id"] = cmd.ref_id

        trace = {
            "momento_entrada": entered_at.isoformat(),
            "momento_saida_prevista": resume_at.isoformat(),
            "espera_ms": wait_ms,
        }

        batch_entry = BatchEntry(selected="parameters.tempo", trace=trace)
        if engine is not None:
            engine.store.set(session_id, state)
        LOGGER.info(
            "wait.scheduled",
            extra={
                "session_id": session_id,
                "ref_id": cmd.ref_id,
                "espera_ms": wait_ms,
                "resume_at": resume_at.isoformat(),
                "next_ref": next_ref,
            },
        )
        return ExecResult(continue_loop=False, batch_entry=batch_entry)
