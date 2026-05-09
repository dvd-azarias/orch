from __future__ import annotations

import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from app.commands.base import BatchEntry, CommandHandler, ExecResult
from app.common.variables import normalize_variables_structure
from app.services import orchestrator_member
from app.services.orchestrator_member import (
    AmbiguousMemberError,
    MemberNotFoundError,
)

import logging

log = logging.getLogger("target_core.orch_condition")

Operator = str
PROTECTED_SYSTEM_KEYS = {"contact", "inputs"}


class ConditionHandler(CommandHandler):
    component_id = "condition"

    def execute(
        self,
        *,
        flow,
        state: Dict[str, Any],
        cmd,
        session_id: str,
        helpers: Dict[str, Any],
    ) -> ExecResult:
        if _is_orchestration(state):
            return _execute_orchestration_condition(flow=flow, state=state, cmd=cmd, helpers=helpers)
        params = cmd.parameters or {}
        conditions: List[Dict[str, Any]] = params.get("conditions") or []
        variables = state.get("variables") or {}
        view = _build_view(variables)

        selected_condition: Optional[Dict[str, Any]] = None
        branch_candidates: List[str] = []
        condition_traces: List[Dict[str, Any]] = []
        evaluation_errors: List[str] = []
        error_in_matched_condition = False

        for condition in (cond for cond in conditions if isinstance(cond, dict)):
            rules = condition.get("rules") or []
            match_mode = str(condition.get("match") or "all").strip().lower()
            rule_results: List[Tuple[Dict[str, Any], bool]] = []
            condition_errors: List[str] = []

            for rule in (r for r in rules if isinstance(r, dict)):
                try:
                    result = _evaluate_rule(rule, view)
                except Exception as exc:  # noqa: BLE001 - queremos capturar qualquer falha de regra
                    result = False
                    rule_label = rule.get("id") or rule.get("field") or "rule"
                    error_message = f"rule:{rule_label} error:{exc}"
                    condition_errors.append(error_message)
                    evaluation_errors.append(error_message)
                rule_results.append((rule, result))

            matched = _aggregate_results(rule_results, match_mode)
            trace_entry: Dict[str, Any] = {
                "id": condition.get("id"),
                "label": condition.get("label"),
                "match": match_mode,
                "matched": matched,
            }
            if condition_errors:
                trace_entry["errors"] = condition_errors[:3]
            condition_traces.append(trace_entry)

            if matched:
                selected_condition = condition
                branch_candidates = _branch_candidates_for(condition)
                if condition_errors:
                    error_in_matched_condition = True
                break

        if selected_condition is None:
            if evaluation_errors:
                branch_candidates = ["exception", "no_results", "no_result"]
            else:
                branch_candidates = ["no_results", "no_result"]
        elif error_in_matched_condition:
            branch_candidates = ["exception"] + [candidate for candidate in branch_candidates if candidate != "exception"]

        batch = BatchEntry(selected="parameters.conditions")
        if branch_candidates:
            seen_candidates: set[str] = set()
            branch_candidates = [
                candidate
                for candidate in branch_candidates
                if candidate and (candidate not in seen_candidates and not seen_candidates.add(candidate))
            ]

        if condition_traces:
            batch.trace = {
                "evaluated_conditions": condition_traces[:10],
                "branch_attempts": branch_candidates[:5],
            }
            if evaluation_errors:
                batch.trace["errors"] = evaluation_errors[:5]

        to_ref: Optional[str] = None
        chosen_branch: Optional[str] = None
        branch_to = helpers.get("branch_to")
        if callable(branch_to):
            for candidate in (c for c in branch_candidates if c):
                try:
                    to_ref = branch_to(cmd.ref_id, candidate)
                except Exception:
                    to_ref = None
                if to_ref:
                    chosen_branch = candidate
                    break

        if not to_ref:
            next_ref = helpers.get("next_ref")
            if callable(next_ref):
                try:
                    to_ref = next_ref(cmd.ref_id)
                except Exception:
                    to_ref = None

        cut_idx = None
        if to_ref:
            compute_cut = helpers.get("compute_cut")
            if callable(compute_cut):
                cut_idx = compute_cut(cmd.ref_id, to_ref)

        if batch.trace is not None and chosen_branch:
            batch.trace["branch"] = chosen_branch

        return ExecResult(
            continue_loop=True,
            to_ref=to_ref,
            cut_index=cut_idx,
            batch_entry=batch,
        )


def _build_view(variables: Dict[str, Any]) -> Dict[str, Any]:
    variables = normalize_variables_structure(variables or {})

    customs_candidate = variables.get("customs")
    customs = customs_candidate if isinstance(customs_candidate, dict) else {}

    system_candidate = variables.get("system")
    system = system_candidate if isinstance(system_candidate, dict) else {}

    utils_candidate = variables.get("utils")
    utils = utils_candidate if isinstance(utils_candidate, dict) else {}

    view: Dict[str, Any] = {}
    for source in (system, utils):
        if isinstance(source, dict):
            for key, value in source.items():
                view.setdefault(key, value)
    if isinstance(customs, dict):
        for key, value in customs.items():
            if key in PROTECTED_SYSTEM_KEYS and key in view:
                continue
            view[key] = value

    view["customs"] = customs
    view["system"] = system
    view["utils"] = utils
    view["variables"] = variables
    return view


def _is_orchestration(state: Dict[str, Any]) -> bool:
    variables_raw = state.get("variables") if isinstance(state, dict) else {}
    variables = normalize_variables_structure(variables_raw or {})
    system = variables.get("system") if isinstance(variables, dict) else {}
    runner_block = system.get("runner_v5") if isinstance(system, dict) else {}
    mode_candidates = []
    for src in (system, runner_block):
        if isinstance(src, dict):
            mode_candidates.append(src.get("mode"))
    for candidate in mode_candidates:
        if isinstance(candidate, str) and candidate.strip().lower() == "orchestration":
            return True
    # fallback: provider == orchestrator também indica modo orchestration
    provider = None
    if isinstance(system, dict):
        provider = system.get("provider")
    if isinstance(provider, str) and provider.strip().lower() == "orchestrator":
        return True
    return False


def _execute_orchestration_condition(*, flow, state: Dict[str, Any], cmd, helpers: Dict[str, Any]) -> ExecResult:
    params = cmd.parameters or {}
    conditions: List[Dict[str, Any]] = params.get("conditions") or []

    variables = state.get("variables") or {}
    system_vars = variables.get("system") or {}
    contact = system_vars.get("contact") or {}

    member_id = contact.get("contact_list_member_id") or contact.get("contact_list_member") or contact.get("member_id")
    workspace_uuid = system_vars.get("workspace_uuid")

    if (not member_id) and workspace_uuid:
        # Fallback: resolve novamente o membro
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
                state["variables"] = variables
        except Exception:
            log.debug("orch_condition.fallback_fetch_failed", exc_info=True, extra={"workspace_uuid": workspace_uuid, "selectors": selectors})

    view = _build_contact_view(contact)

    selected_condition: Optional[Dict[str, Any]] = None
    branch_candidates: List[str] = []
    condition_traces: List[Dict[str, Any]] = []
    evaluation_errors: List[str] = []
    error_in_matched_condition = False

    for condition in (cond for cond in conditions if isinstance(cond, dict)):
        rules = condition.get("rules") or []
        match_mode = str(condition.get("match") or "all").strip().lower()
        rule_results: List[Tuple[Dict[str, Any], bool]] = []
        condition_errors: List[str] = []
        for rule in (r for r in rules if isinstance(r, dict)):
            try:
                result = _evaluate_orch_rule(rule, view)
            except Exception as exc:  # noqa: BLE001
                result = False
                rule_label = rule.get("id") or rule.get("field") or "rule"
                error_detail = f"rule:{rule_label} error:{exc}"
                condition_errors.append(error_detail)
                evaluation_errors.append(error_detail)
            rule_results.append((rule, result))

        matched = _aggregate_results(rule_results, match_mode)
        trace_entry: Dict[str, Any] = {
            "id": condition.get("id"),
            "label": condition.get("label"),
            "match": match_mode,
            "matched": matched,
        }
        if condition_errors:
            trace_entry["errors"] = condition_errors[:3]
        condition_traces.append(trace_entry)

        if matched:
            selected_condition = condition
            branch_candidates = _branch_candidates_for(condition)
            if condition_errors:
                error_in_matched_condition = True
            break

    if selected_condition is None:
        branch_candidates = ["exception", "no_match"] if evaluation_errors else ["no_match"]
        segment_value = "NO_MATCH"
    else:
        segment_value = str(selected_condition.get("label") or "").strip() or "NO_MATCH"
        if error_in_matched_condition:
            branch_candidates = ["exception"] + [candidate for candidate in branch_candidates if candidate != "exception"]

    if isinstance(system_vars, dict):
        contact_state = system_vars.get("contact") if isinstance(system_vars.get("contact"), dict) else {}
        contact_state = dict(contact_state)
        contact_state["segment"] = segment_value
        system_vars["contact"] = contact_state
        variables["system"] = system_vars
        state["variables"] = variables

    segment_update_error = False
    if workspace_uuid and member_id:
        try:
            orchestrator_member.update_segment(str(workspace_uuid), int(member_id), segment_value)
        except (ValueError, MemberNotFoundError, AmbiguousMemberError):
            log.warning(
                "orch_condition.segment_update_failed",
                extra={"workspace_uuid": workspace_uuid, "member_id": member_id, "segment": segment_value},
            )
        except Exception:
            log.exception(
                "orch_condition.segment_update_error",
                extra={"workspace_uuid": workspace_uuid, "member_id": member_id, "segment": segment_value},
            )
            segment_update_error = True
    else:
        log.debug(
            "orch_condition.segment_update_skipped",
            extra={"workspace_uuid": workspace_uuid, "member_id": member_id, "segment": segment_value},
        )
    log.info(
        "orch_condition.exit",
        extra={"workspace_uuid": workspace_uuid, "member_id": member_id, "segment": segment_value},
    )

    to_ref: Optional[str] = None
    chosen_branch: Optional[str] = None
    branch_to = helpers.get("branch_to")
    if callable(branch_to):
        for candidate in (c for c in branch_candidates if c):
            try:
                to_ref = branch_to(cmd.ref_id, candidate)
            except Exception:
                to_ref = None
            if to_ref:
                chosen_branch = candidate
                break

    if not to_ref:
        next_ref = helpers.get("next_ref")
        if callable(next_ref):
            try:
                to_ref = next_ref(cmd.ref_id)
            except Exception:
                to_ref = None

    cut_idx = None
    if to_ref:
        compute_cut = helpers.get("compute_cut")
        if callable(compute_cut):
            cut_idx = compute_cut(cmd.ref_id, to_ref)

    trace: Optional[Dict[str, Any]] = None
    if segment_update_error and "exception" not in branch_candidates:
        branch_candidates = ["exception"] + branch_candidates

    if branch_candidates:
        seen_candidates: set[str] = set()
        branch_candidates = [
            candidate
            for candidate in branch_candidates
            if candidate and (candidate not in seen_candidates and not seen_candidates.add(candidate))
        ]

    if condition_traces:
        trace = {
            "evaluated_conditions": condition_traces[:10],
            "branch_attempts": branch_candidates[:5],
        }
        if chosen_branch:
            trace["branch"] = chosen_branch
        if evaluation_errors:
            trace["errors"] = evaluation_errors[:5]
        if segment_update_error:
            trace["segment_update_error"] = True

    batch = BatchEntry(selected="parameters.conditions", trace=trace)

    return ExecResult(continue_loop=True, to_ref=to_ref, cut_index=cut_idx, batch_entry=batch)


def _build_contact_view(contact: Dict[str, Any]) -> Dict[str, Any]:
    base: Dict[str, Any] = {}
    if not isinstance(contact, dict):
        return base
    for key, value in contact.items():
        base[key] = value
    attempts = contact.get("attempts") if isinstance(contact.get("attempts"), dict) else {}
    base["attempts"] = attempts
    extra = contact.get("extra") if isinstance(contact.get("extra"), dict) else {}
    base["extra"] = extra
    return base


_FIELD_MAP = {
    "contact.identifier": "identifier",
    "contact.name": "name",
    "contact.full_name": "full_name",
    "contact.gender": "gender",
    "contact.country": "country",
    "contact.province": "province",
    "contact.city": "city",
    "contact.birth_date": "birth_date",
    "contact.age": "age",
    "contact.status": "status",
    "contact.draft_id": "draft_id",
    "contact.id": "id",
    "contact.channel.type": "channel.type",
    "contact.channel.label": "channel.label",
    "contact.channel.address": "channel.address",
    "contact.attempts.busy": "attempts.busy",
    "contact.attempts.noanswer": "attempts.noanswer",
    "contact.attempts.machine": "attempts.machine",
    "contact.attempts.rejected": "attempts.rejected",
    "contact.attempts.invalidnumber": "attempts.invalidnumber",
    "contact.attempts.failure": "attempts.failure",
}


def _evaluate_orch_rule(rule: Dict[str, Any], contact_view: Dict[str, Any]) -> bool:
    operator: Operator = str(rule.get("operator") or "is").strip().lower()
    field_raw = rule.get("field")
    value = rule.get("value")

    path = None
    if isinstance(field_raw, str):
        field_str = field_raw.strip()
        if field_str.startswith("{{") and field_str.endswith("}}"):
            field_str = field_str[2:-2].strip()
        mapped = _FIELD_MAP.get(field_str) or _FIELD_MAP.get(f"contact.{field_str}")
        if mapped:
            path = mapped
        elif field_str.startswith("contact.extra."):
            path = field_str.replace("contact.extra.", "extra.", 1)
        elif field_str.startswith("extra."):
            path = field_str
        elif field_str.startswith("contact."):
            path = field_str.replace("contact.", "", 1)
        else:
            path = field_str

    left = _lookup_contact(contact_view, path) if path else None

    if operator == "is":
        return _orch_compare_eq(left, value)
    return False


def _lookup_contact(view: Dict[str, Any], path: Optional[str]) -> Any:
    if not path:
        return None
    segments = [segment for segment in path.split(".") if segment]
    current: Any = view
    for seg in segments:
        if isinstance(current, dict) and seg in current:
            current = current[seg]
            continue
        return None
    return current


def _orch_compare_eq(left: Any, right: Any) -> bool:
    # Coerção numérica se ambos forem números válidos
    left_num = _coerce_number(left)
    right_num = _coerce_number(right)
    if left_num is not None and right_num is not None:
        return left_num == right_num

    if isinstance(left, str):
        left_norm = left.strip()
    else:
        left_norm = str(left).strip() if left is not None else ""

    if isinstance(right, str):
        right_norm = right.strip()
    else:
        right_norm = str(right).strip() if right is not None else ""

    return left_norm == right_norm


def _evaluate_rule(rule: Dict[str, Any], view: Dict[str, Any]) -> bool:
    matched, _, _ = _evaluate_rule_with_details(rule, view)
    return matched


def _evaluate_rule_with_details(rule: Dict[str, Any], view: Dict[str, Any]) -> Tuple[bool, Any, Any]:
    operator: Operator = str(rule.get("operator") or "is").strip().lower()
    left = _resolve_operand(rule.get("field"), view)
    right = _resolve_operand(rule.get("value"), view)
    return _compare(left, right, operator), left, right


def _resolve_operand(raw: Any, view: Dict[str, Any]) -> Any:
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("{{") and stripped.endswith("}}"):
            key = stripped[2:-2].strip()
            return _lookup(view, key)
        return raw
    return raw


def _lookup(view: Dict[str, Any], path: str) -> Any:
    if not path:
        return None
    current: Any = view
    for segment in _split_path(path):
        if isinstance(segment, int):
            if isinstance(current, (list, tuple)) and -len(current) <= segment < len(current):
                current = current[segment]
                continue
            return None
        if isinstance(current, dict) and segment in current:
            current = current[segment]
            continue
        return None
    return current


def _split_path(path: str) -> Iterable[Union[str, int]]:
    for segment in (part.strip() for part in path.split(".") if part.strip()):
        if "[" in segment and segment.endswith("]"):
            key, indexes = _split_index_segment(segment)
            if key:
                yield key
            for idx in indexes:
                yield idx
        else:
            yield segment


def _split_index_segment(segment: str) -> Tuple[Optional[str], List[int]]:
    root: List[str] = []
    indexes: List[int] = []
    buf = ""
    in_index = False
    for char in segment:
        if char == "[":
            if not in_index:
                if buf:
                    root.append(buf)
                buf = ""
                in_index = True
            else:
                return segment, []  # formato inválido, devolve literal
        elif char == "]":
            if in_index:
                try:
                    indexes.append(int(buf))
                except ValueError:
                    return segment, []
                buf = ""
                in_index = False
            else:
                return segment, []
        else:
            buf += char
    if buf:
        if in_index:
            try:
                indexes.append(int(buf))
            except ValueError:
                return segment, []
        else:
            root.append(buf)
    key = root[0] if root else None
    return key, indexes


def _aggregate_results(results: List[Tuple[Dict[str, Any], bool]], mode: str) -> bool:
    if not results:
        return False
    if mode == "any":
        return any(result for _, result in results)
    return all(result for _, result in results)


def _branch_candidates_for(condition: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []
    label = str(condition.get("label") or "").strip()
    cond_id = str(condition.get("id") or "").strip()
    for candidate in (label, cond_id, "success"):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        candidates.append("success")
    return candidates


def _compare(left: Any, right: Any, operator: Operator) -> bool:
    if operator == "is":
        return _string_or_typed_equals(left, right)
    if operator == "is_not":
        return not _string_or_typed_equals(left, right)
    if operator == "greater":
        return _numeric_cmp(left, right, lambda a, b: a > b)
    if operator == "greater_equal":
        return _numeric_cmp(left, right, lambda a, b: a >= b)
    if operator == "less":
        return _numeric_cmp(left, right, lambda a, b: a < b)
    if operator == "less_equal":
        return _numeric_cmp(left, right, lambda a, b: a <= b)
    if operator == "contains":
        return _string_contains(left, right)
    if operator == "not_contains":
        return not _string_contains(left, right)
    if operator == "starts_with":
        return _string_predicate(left, right, str.startswith)
    if operator == "ends_with":
        return _string_predicate(left, right, str.endswith)
    return False


def _normalize(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        lowered = stripped.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        if lowered in {"null", "none"}:
            return None
        numeric = _coerce_number(value)
        if numeric is not None:
            return numeric
        return stripped
    return value


def _numeric_cmp(left: Any, right: Any, comparator) -> bool:
    left_num = _coerce_number(left)
    right_num = _coerce_number(right)
    if left_num is None or right_num is None:
        return False
    return comparator(left_num, right_num)


def _coerce_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _string_contains(left: Any, right: Any) -> bool:
    left_str = _stringify(left)
    right_str = _stringify(right)
    if not right_str:
        return False
    if right_str in left_str:
        return True
    left_digits = _digits_only(left_str)
    right_digits = _digits_only(right_str)
    if left_digits and right_digits:
        return right_digits in left_digits
    return False


def _string_predicate(left: Any, right: Any, predicate) -> bool:
    left_str = _stringify(left)
    right_str = _stringify(right)
    if not right_str:
        return False
    return predicate(left_str, right_str)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return str(value)
    return _normalize_text(str(value))


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.replace("\u00A0", " ")
    cleaned = "".join(ch for ch in normalized if unicodedata.category(ch) not in {"Cc", "Cf"})
    return cleaned.strip()


def _digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _string_or_typed_equals(left: Any, right: Any) -> bool:
    left_norm = _normalize(left)
    right_norm = _normalize(right)
    if left_norm == right_norm:
        return True
    left_str = _stringify(left)
    right_str = _stringify(right)
    if left_str == right_str:
        return True
    left_digits = _digits_only(left_str)
    right_digits = _digits_only(right_str)
    if left_digits and right_digits:
        return left_digits == right_digits
    return False
