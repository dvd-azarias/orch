from __future__ import annotations

import copy
import json
import logging

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .base import BatchEntry, CommandHandler, ExecResult
from app.config import get_settings

LOGGER = logging.getLogger("target_core.commands.finish_flow")
_SETTINGS = get_settings()


def record_disposition_set(*args: Any, **kwargs: Any) -> Any:
    from app.services.metrics_utils import record_disposition_set as _record_disposition_set

    return _record_disposition_set(*args, **kwargs)


def resolve_channel(system: Dict[str, Any], fallback: Any) -> str:
    from app.services.metrics_utils import resolve_channel as _resolve_channel

    return _resolve_channel(system, fallback)


def normalize_finish_flow_result(raw_result: Any) -> str:
    allowed = {"success", "unsuccess", "neutral", "transfer"}

    if isinstance(raw_result, dict):
        for key in ("id", "value", "name", "label"):
            candidate = raw_result.get(key)
            if isinstance(candidate, str) and candidate.strip():
                raw_result = candidate
                break

    if isinstance(raw_result, str):
        normalized = raw_result.strip().lower()
        if normalized in allowed:
            return normalized
        aliases = {
            "sucesso": "success",
            "insucesso": "unsuccess",
            "neutro": "neutral",
            "transferido": "transfer",
        }
        if normalized in aliases:
            return aliases[normalized]

    return "neutral"


class FinishFlowHandler(CommandHandler):
    component_id = "finish_flow"

    @staticmethod
    def _preview_value(value: Any) -> str:
        if isinstance(value, str):
            return value[:200]
        try:
            serialized = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            serialized = str(value)
        return serialized[:200]

    @classmethod
    def _restore_rendered_types(cls, original: Any, rendered: Any, resolved: Dict[str, Any]) -> Any:
        if isinstance(original, str) and isinstance(rendered, str):
            trimmed = original.strip()
            if trimmed.startswith("{{") and trimmed.endswith("}}"):
                key = trimmed[2:-2].strip()
                if key and key in resolved:
                    return copy.deepcopy(resolved[key])
            return rendered

        if isinstance(original, list) and isinstance(rendered, list):
            result: List[Any] = []
            for idx, rendered_item in enumerate(rendered):
                original_item = original[idx] if idx < len(original) else rendered_item
                result.append(cls._restore_rendered_types(original_item, rendered_item, resolved))
            return result

        if isinstance(original, tuple) and isinstance(rendered, tuple):
            restored_items = []
            for idx, rendered_item in enumerate(rendered):
                original_item = original[idx] if idx < len(original) else rendered_item
                restored_items.append(cls._restore_rendered_types(original_item, rendered_item, resolved))
            return tuple(restored_items)

        if isinstance(original, dict) and isinstance(rendered, dict):
            output: Dict[str, Any] = {}
            for key, rendered_value in rendered.items():
                output[key] = cls._restore_rendered_types(original.get(key), rendered_value, resolved)
            return output

        return rendered

    @classmethod
    def _render_disposition_payload(
        cls,
        *,
        state: Dict[str, Any],
        original_payload: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
        try:
            from app.services.template_resolver import render_obj  # pylint: disable=import-outside-toplevel
        except Exception:  # pragma: no cover - evita ciclagem durante importação parcial
            LOGGER.warning("finish_flow.template_resolver_import_error")
            return original_payload, [], {}

        variables = state.get("variables") if isinstance(state, dict) else {}
        if not isinstance(variables, dict):
            variables = {}

        try:
            rendered, missing, resolved = render_obj(original_payload, variables)
        except Exception:  # pragma: no cover - proteção contra falhas na engine
            LOGGER.warning("finish_flow.template_render_error")
            return original_payload, [], {}

        missing_dedup = list(dict.fromkeys(missing)) if missing else []
        resolved_copy = {key: copy.deepcopy(value) for key, value in resolved.items()}
        restored = cls._restore_rendered_types(original_payload, rendered, resolved_copy)
        return restored, missing_dedup, resolved_copy

    @classmethod
    def _build_trace_payload(
        cls,
        *,
        resolved: Dict[str, Any],
        missing: List[str],
    ) -> Optional[Dict[str, Any]]:
        if not _SETTINGS.trace_in_ref_path:
            return None

        trace: Dict[str, Any] = {}
        if resolved:
            preview: Dict[str, str] = {}
            for key in list(resolved.keys())[:10]:
                preview[key] = cls._preview_value(resolved[key])
            trace["json_terminate_resolved"] = preview
        if missing:
            trace["json_terminate_missing"] = missing[:20]
        return trace or None

    @staticmethod
    def _should_export(raw_value: Any) -> bool:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    item_id = str(item.get("id") or item.get("value") or "").strip().lower()
                    if item_id in {"yes", "export"}:
                        return True
                elif isinstance(item, str) and item.strip().lower() in {"yes", "export", "true", "1"}:
                    return True
                elif isinstance(item, (int, float)) and str(item).strip() == "1":
                    return True
            return False
        if isinstance(raw_value, str):
            return raw_value.strip().lower() in {"yes", "export", "true", "1"}
        if isinstance(raw_value, (int, float)):
            return bool(int(raw_value))
        if isinstance(raw_value, bool):
            return raw_value
        return False

    @staticmethod
    def _normalize_text(raw_value: Any) -> Optional[str]:
        if raw_value is None:
            return None
        text = str(raw_value).strip()
        return text or None

    @classmethod
    def _normalize_json_terminate(
        cls,
        raw_value: Any,
        *,
        fallback_code: str,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if isinstance(raw_value, dict):
            payload = raw_value
        elif isinstance(raw_value, str):
            candidate = raw_value.strip()
            if candidate:
                try:
                    parsed = json.loads(candidate)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = {}
                if isinstance(parsed, dict):
                    payload = parsed

        disposition_code = cls._normalize_text(payload.get("disposition_code")) or fallback_code
        disposition_category = cls._normalize_text(payload.get("disposition_category"))
        disposition_description = cls._normalize_text(payload.get("disposition_description"))
        notes = cls._normalize_text(payload.get("notes"))
        follow_up_required = cls._normalize_boolean(payload.get("follow_up_required"), default=True)
        follow_up_date = cls._normalize_text(payload.get("follow_up_date"))
        tags = cls._normalize_tags(payload.get("tags"))

        additional_data = payload.get("additional_data")
        if not isinstance(additional_data, dict):
            additional_data = {}

        return {
            "disposition_code": disposition_code,
            "disposition_category": disposition_category,
            "disposition_description": disposition_description,
            "notes": notes,
            "follow_up_required": follow_up_required,
            "follow_up_date": follow_up_date,
            "tags": tags,
            "additional_data": additional_data,
        }

    @classmethod
    def _resolve_json_terminate_raw(
        cls,
        *,
        state: Dict[str, Any],
        raw_value: Any,
    ) -> Any:
        if not isinstance(raw_value, dict):
            return raw_value

        nested_json = raw_value.get("json")
        if isinstance(nested_json, (dict, str)):
            return nested_json

        var_name = cls._normalize_text(raw_value.get("output_var_name"))
        if not var_name:
            return raw_value

        variables = state.get("variables") if isinstance(state, dict) else {}
        if not isinstance(variables, dict):
            variables = {}

        customs = variables.get("customs")
        if isinstance(customs, dict) and var_name in customs:
            return customs.get(var_name)

        system = variables.get("system")
        if isinstance(system, dict):
            system_customs = system.get("customs")
            if isinstance(system_customs, dict) and var_name in system_customs:
                return system_customs.get(var_name)

        return raw_value

    @staticmethod
    def _normalize_boolean(raw_value: Any, *, default: bool) -> bool:
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, (int, float)):
            return bool(raw_value)
        if isinstance(raw_value, str):
            normalized = raw_value.strip().lower()
            if normalized in {"1", "true", "yes", "sim"}:
                return True
            if normalized in {"0", "false", "no", "nao", "não"}:
                return False
        return default

    @classmethod
    def _normalize_tags(cls, raw_value: Any) -> List[str]:
        if isinstance(raw_value, str):
            tag = cls._normalize_text(raw_value)
            return [tag] if tag else []

        if not isinstance(raw_value, (list, tuple, set)):
            return []

        normalized_tags: List[str] = []
        for item in raw_value:
            tag = cls._normalize_text(item)
            if tag:
                normalized_tags.append(tag)
        return normalized_tags

    @staticmethod
    def _normalize_webhook(raw_value: Any) -> Optional[str]:
        if not isinstance(raw_value, str):
            return None
        candidate = raw_value.strip()
        if not candidate:
            return None
        parsed = urlparse(candidate)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if not parsed.netloc:
            return None
        return candidate

    @classmethod
    def _normalize_custom_variable_name(cls, raw_value: Any) -> Optional[str]:
        if not isinstance(raw_value, str):
            return None
        candidate = raw_value.strip()
        if not candidate:
            return None
        if candidate.startswith("customs."):
            candidate = candidate[len("customs.") :].strip()
        return candidate or None

    @classmethod
    def _extract_enrich_variable_name(cls, item: Any) -> Optional[str]:
        if isinstance(item, str):
            return cls._normalize_custom_variable_name(item)

        if not isinstance(item, dict):
            return None

        payload = item.get("payload")
        if isinstance(payload, dict):
            normalized = cls._normalize_custom_variable_name(payload.get("path"))
            if normalized:
                return normalized

        for key in ("id", "name", "value", "label", "variable", "sync_uuid"):
            normalized = cls._normalize_custom_variable_name(item.get(key))
            if normalized:
                return normalized
        return None

    @classmethod
    def _extract_selected_enrich_variable_names(cls, raw_value: Any) -> List[str]:
        selected_items: List[Any] = []

        if isinstance(raw_value, list):
            selected_items = raw_value
        elif isinstance(raw_value, dict):
            variables_bucket = raw_value.get("variables")
            if isinstance(variables_bucket, dict):
                in_use_bucket = variables_bucket.get("in_use")
                if isinstance(in_use_bucket, list):
                    selected_items = in_use_bucket
                elif in_use_bucket is not None:
                    selected_items = [in_use_bucket]
            elif isinstance(raw_value.get("in_use"), list):
                selected_items = raw_value.get("in_use")
            elif raw_value.get("in_use") is not None:
                selected_items = [raw_value.get("in_use")]
            else:
                selected_items = [raw_value]
        elif raw_value is not None:
            selected_items = [raw_value]

        selected_names: List[str] = []
        seen_names: set[str] = set()
        for item in selected_items:
            normalized = cls._extract_enrich_variable_name(item)
            if not normalized or normalized in seen_names:
                continue
            seen_names.add(normalized)
            selected_names.append(normalized)
        return selected_names

    @classmethod
    def _enrich_disposition_payload_with_selected_variables(
        cls,
        *,
        state: Dict[str, Any],
        disposition_payload: Dict[str, Any],
        enrich_variables_raw: Any,
    ) -> Dict[str, Any]:
        additional_data = disposition_payload.get("additional_data")
        if not isinstance(additional_data, dict):
            disposition_payload.pop("additional_data", None)
            return disposition_payload

        disposition_payload["additional_data"] = copy.deepcopy(additional_data)
        return disposition_payload

    @classmethod
    def _build_disposition_payload_with_session_snapshot(
        cls,
        *,
        state: Dict[str, Any],
        disposition_code: str,
        base_payload: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        _ = state
        payload = copy.deepcopy(base_payload) if isinstance(base_payload, dict) else {}
        payload["disposition_code"] = disposition_code
        return payload

    @staticmethod
    def _register_export(
        *,
        state: Dict[str, Any],
        ref_id: str,
        result_value: str,
        webhook_url: str,
    ) -> None:
        pending: List[Dict[str, Any]] = state.setdefault("_finish_flow_exports", [])
        pending.append(
            {
                "ref_id": ref_id,
                "result": result_value,
                "webhook": webhook_url,
            }
        )

    def _maybe_emit_disposition_metric(
        self,
        *,
        helpers: Dict[str, Any],
        state: Dict[str, Any],
        session_id: str,
        disposition_code: str,
        disposition_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not _SETTINGS.codex_metrics_v5_enabled:
            return
        engine = helpers.get("engine") if isinstance(helpers, dict) else None
        store = getattr(engine, "store", None) if engine else None
        if store is None:
            return

        variables = state.get("variables") if isinstance(state, dict) else {}
        system = variables.get("system") if isinstance(variables, dict) else {}
        if not isinstance(system, dict):
            system = {}

        workspace_id = (
            system.get("workspace_uuid")
            or system.get("workspace_id")
            or system.get("workspace")
        )
        if not workspace_id:
            return

        flow_id = (
            system.get("flow_id")
            or system.get("flow_uuid")
            or system.get("flow_slug")
            or system.get("flow_display_name")
        )

        contact = system.get("contact") if isinstance(system.get("contact"), dict) else {}
        contact_id = (
            system.get("contact_id")
            or contact.get("id")
            or contact.get("identifier")
            or system.get("external_session_id")
            or system.get("session_external_id")
            or system.get("contact_channel_address")
        )

        channel_for_metrics = resolve_channel(system, system.get("channel"))

        try:
            record_disposition_set(
                store,
                session_id=session_id,
                workspace_id=str(workspace_id),
                flow_id=str(flow_id) if flow_id else None,
                channel=channel_for_metrics,
                disposition_code=disposition_code,
                disposition_payload=disposition_payload,
                contact_id=str(contact_id) if contact_id else None,
                usage_tokens=None,
                use_codex_sync=True,
                batch_with_session_end=True,
                runtime_state=state,
            )
        except Exception as exc:  # pragma: no cover
            LOGGER.warning(
                "finish_flow.metrics_event_failed session_id=%s channel=%s error=%s",
                session_id,
                channel_for_metrics,
                exc,
            )

    def execute(
        self,
        *,
        flow,
        state: Dict[str, Any],
        cmd,
        session_id: str,
        helpers: Dict[str, Any],
    ) -> ExecResult:
        params = cmd.parameters if isinstance(cmd.parameters, dict) else {}
        result_value = normalize_finish_flow_result(params.get("result"))
        params["result"] = result_value

        # Compatibilidade: ignora sinalizador legado, json_terminate é sempre ativo.
        params.pop("emit_disposition", None)

        json_terminate_raw = self._resolve_json_terminate_raw(
            state=state,
            raw_value=params.get("json_terminate"),
        )
        disposition_payload: Optional[Dict[str, Any]] = self._normalize_json_terminate(
            json_terminate_raw,
            fallback_code=result_value,
        )
        if disposition_payload is not None:
            disposition_payload = self._enrich_disposition_payload_with_selected_variables(
                state=state,
                disposition_payload=disposition_payload,
                enrich_variables_raw=params.get("enrich_variables"),
            )
            (
                disposition_payload,
                trace_missing,
                trace_resolved,
            ) = self._render_disposition_payload(
                state=state,
                original_payload=disposition_payload,
            )
            disposition_payload["disposition_category"] = result_value
        else:
            trace_missing = []
            trace_resolved = {}
        params["json_terminate"] = disposition_payload

        export_enabled = self._should_export(params.get("export_session_data"))
        webhook_url = self._normalize_webhook(params.get("webhook"))
        if export_enabled and webhook_url:
            if isinstance(state, dict):
                self._register_export(
                    state=state,
                    ref_id=cmd.ref_id,
                    result_value=result_value,
                    webhook_url=webhook_url,
                )

        cmd.parameters = params

        try:
            disposition_code_for_metrics = (
                str(disposition_payload.get("disposition_code")).strip()
                if disposition_payload and disposition_payload.get("disposition_code")
                else result_value
            )
            disposition_payload_for_metrics = self._build_disposition_payload_with_session_snapshot(
                state=state,
                disposition_code=disposition_code_for_metrics,
                base_payload=disposition_payload,
            )
            self._maybe_emit_disposition_metric(
                helpers=helpers,
                state=state,
                session_id=session_id,
                disposition_code=disposition_code_for_metrics,
                disposition_payload=disposition_payload_for_metrics,
            )
        except Exception as exc:  # pragma: no cover - evitar impacto no fluxo principal
            LOGGER.warning("finish_flow.metrics_dispatch_error session_id=%s error=%s", session_id, exc)

        batch = BatchEntry(selected="parameters.result")
        trace_payload = self._build_trace_payload(resolved=trace_resolved, missing=trace_missing)
        if trace_payload:
            batch.trace = trace_payload
        state["cursor_ref_id"] = None
        return ExecResult(continue_loop=False, batch_entry=batch, finished=True)
