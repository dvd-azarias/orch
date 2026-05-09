# app/commands/code_editor.py
from __future__ import annotations
from typing import Any, Dict, List, Tuple
import json
import logging
import re

from app.commands.base import CommandHandler, BatchEntry, ExecResult
from app.commands.utils import (
    run_js_code,
    extract_branches_from_code,
    transform_export_default,
)
from app.services.template_resolver import render_string  # <<< resolver unificado
from app.common.variables import normalize_variables_structure
from app.config import get_settings

_settings = get_settings()
LOGGER = logging.getLogger("target_core.code_editor")

LOGGER.debug("code_editor.handler_version=2026-03-02T-customs-alias+inference-sync")

DEFAULT_TIMEOUT_MS = 400
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 5000

def dbg_on() -> bool:
    return _settings.code_editor_debug

def trace_enabled() -> bool:
    return _settings.trace_in_ref_path

# Se quiser desligar a expansão de {{}} só para este componente:
def template_on() -> bool:
    return _settings.code_editor_template_enabled


def _preview_value(value: Any, limit: int = 200) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except Exception:
            text = str(value)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _collect_updates(before: Dict[str, Any], after: Dict[str, Any], limit: int = 10) -> Dict[str, str]:
    updates: Dict[str, str] = {}
    count = 0
    for key, value in after.items():
        if count >= limit:
            break
        if key not in before or before.get(key) != value:
            updates[key] = _preview_value(value)
            count += 1
    return updates


def _coerce_timeout_ms(raw_value: Any) -> Tuple[int, str | None]:
    if raw_value is None:
        return DEFAULT_TIMEOUT_MS, None
    try:
        parsed = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MS, f"invalid_timeout:{raw_value!r}"
    clamped = max(MIN_TIMEOUT_MS, min(MAX_TIMEOUT_MS, parsed))
    if clamped != parsed:
        return clamped, f"clamped_timeout:{parsed}->{clamped}"
    return clamped, None


# =========================
# Helpers de instrumentação
# =========================

_VAR_DECL_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)", re.MULTILINE
)
_FUNC_MAIN_RE = re.compile(
    r"function\s+main\s*\(\s*ctx\s*\)\s*\{", re.MULTILINE
)
_JS_RESERVED = {
    "await", "break", "case", "catch", "class", "const", "continue",
    "debugger", "default", "delete", "do", "else", "enum", "export",
    "extends", "false", "finally", "for", "function", "if", "import",
    "in", "instanceof", "new", "null", "return", "super", "switch",
    "this", "throw", "true", "try", "typeof", "var", "void", "while",
    "with", "yield", "let",
}


def _find_matching_brace(code: str, open_brace_index: int) -> int:
    depth = 0
    mode = "normal"
    i = open_brace_index
    n = len(code)

    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""

        if mode == "normal":
            if ch == "/" and nxt == "/":
                mode = "line_comment"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                mode = "block_comment"
                i += 2
                continue
            if ch == "'":
                mode = "single"
                i += 1
                continue
            if ch == '"':
                mode = "double"
                i += 1
                continue
            if ch == "`":
                mode = "template"
                i += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
            continue

        if mode == "line_comment":
            if ch in ("\n", "\r"):
                mode = "normal"
            i += 1
            continue

        if mode == "block_comment":
            if ch == "*" and nxt == "/":
                mode = "normal"
                i += 2
                continue
            i += 1
            continue

        if mode in ("single", "double", "template"):
            if ch == "\\":
                i += 2
                continue
            if mode == "single" and ch == "'":
                mode = "normal"
                i += 1
                continue
            if mode == "double" and ch == '"':
                mode = "normal"
                i += 1
                continue
            if mode == "template" and ch == "`":
                mode = "normal"
                i += 1
                continue
            i += 1
            continue

    raise ValueError("Chaves desequilibradas em main(ctx)")


def _find_main_body_span(code: str) -> Tuple[int, int, int]:
    m = _FUNC_MAIN_RE.search(code)
    if not m:
        raise ValueError("function main(ctx){...} não encontrada após transform_export_default()")
    brace_open = code.find("{", m.end() - 1)
    if brace_open == -1:
        raise ValueError("Delimitador '{' de main não encontrado")
    brace_close = _find_matching_brace(code, brace_open)
    return brace_open, brace_open + 1, brace_close - 1

def _extract_var_names_from(src: str) -> List[str]:
    names: List[str] = []
    for m in _VAR_DECL_RE.finditer(src):
        nm = m.group(1)
        if not nm or nm in _JS_RESERVED:
            continue
        if nm not in names:
            names.append(nm)
    return names

def _inject_auto_export_with_returns(code_exec: str, code_raw_for_names: str) -> str:
    try:
        _, body_start, body_end = _find_main_body_span(code_exec)
    except Exception as e:
        if dbg_on():
            LOGGER.debug("[code_editor][auto-export] aviso: %s", str(e))
        return code_exec

    body = code_exec[body_start:body_end + 1]
    names = _extract_var_names_from(body)
    if not names:
        try:
            tmp = transform_export_default(str(code_raw_for_names))
            _, bs, be = _find_main_body_span(tmp)
            names = _extract_var_names_from(tmp[bs:be + 1])
        except Exception:
            names = []

    prelude_lines = [
        "/* ensure ctx.customs compatibility with ctx.variables.customs */",
        "try {",
        "  ctx = ctx || {};",
        "  ctx.variables = ctx.variables || {};",
        "  ctx.variables.customs = ctx.variables.customs || {};",
        "  if (!ctx.customs || typeof ctx.customs !== 'object') {",
        "    ctx.customs = ctx.variables.customs;",
        "  }",
        "} catch (_) {}",
        "/* end ctx.customs compatibility */",
        "",
    ]
    body = "\n".join(prelude_lines) + body

    if names:
        lines = [
            "/* auto-export locals to ctx.variables.customs */",
            "function __auto_export(){",
            "  try {",
            "    ctx = ctx || {};",
            "    ctx.variables = ctx.variables || {};",
            "    ctx.variables.customs = ctx.variables.customs || {};",
            "    if (ctx.customs && typeof ctx.customs === 'object') {",
            "      for (var __k in ctx.customs) {",
            "        ctx.variables.customs[__k] = ctx.customs[__k];",
            "      }",
            "    }",
            "    ctx.customs = ctx.variables.customs;",
        ]
        for nm in names:
            lines.append(f'    if (typeof {nm} !== "undefined") ctx.variables.customs["{nm}"] = {nm};')
        lines += [
            "  } catch (_) {}",
            "}",
            "/* end auto-export */",
            "",
        ]
        header = "\n".join(lines)
        body = header + body

        def repl_return(m: re.Match) -> str:
            expr = m.group(1)
            return f"return (__auto_export(), {expr});"

        body = re.sub(r"\breturn\b\s*([^;]*);", repl_return, body)

    code_exec = code_exec[:body_start] + body + code_exec[body_end + 1:]

    return code_exec


# =========================
# Handler
# =========================

class CodeEditorHandler(CommandHandler):
    component_id = "code_editor"

    def execute(
        self,
        *,
        flow,
        state: Dict[str, Any],
        cmd,
        session_id: str,
        helpers: Dict[str, Any],
    ) -> ExecResult:
        batch = BatchEntry(selected="parameters.code")

        # 1) Parâmetros
        params = cmd.parameters or {}
        code_raw = params.get("code") or params.get("script") or ""
        timeout_ms, timeout_note = _coerce_timeout_ms(params.get("timeout_ms"))

        variables = state.get("variables") or {}

        # 2) (NOVIDADE) Resolver {{placeholders}} no código, caso existam
        if template_on() and isinstance(code_raw, str) and ("{{" in code_raw and "}}" in code_raw):
            code_resolved, missing_keys, resolved_map = render_string(code_raw, variables)
            if dbg_on():
                LOGGER.debug(
                    "[code_editor][template] placeholders found? -> %s",
                    "yes" if resolved_map or missing_keys else "no",
                )
                if resolved_map:
                    LOGGER.debug("[code_editor][template] resolved keys: %s", list(resolved_map.keys())[:20])
                if missing_keys:
                    LOGGER.debug("[code_editor][template] missing keys: %s", missing_keys[:20])
        else:
            code_resolved, missing_keys, resolved_map = str(code_raw), [], {}

        # 3) Branches esperados para ctx.branches → extrair do código JÁ resolvido
        branch_labels = extract_branches_from_code(code_resolved)
        if "success" not in branch_labels:
            branch_labels.append("success")
        if "error" not in branch_labels:
            branch_labels.append("error")
        if "exception" not in branch_labels:
            branch_labels.append("exception")

        # 4) Transform & instrumentação usando o código resolvido
        code_exec = transform_export_default(str(code_resolved))
        code_exec = _inject_auto_export_with_returns(code_exec, code_resolved)

        # 5) Montar ctx inicial + NORMALIZAÇÃO de api_status/api_body para customs
        vars_obj_raw = state.get("variables") or {}
        vars_obj = normalize_variables_structure(dict(vars_obj_raw))
        system_candidate = vars_obj.get("system")
        system = dict(system_candidate) if isinstance(system_candidate, dict) else {}
        customs = dict(vars_obj.get("customs") or {})
        utils = dict(vars_obj.get("utils") or {})
        vars_obj["system"] = dict(system)
        vars_obj["customs"] = customs
        vars_obj["utils"] = utils

        if "api_status" in vars_obj and "api_status" not in customs:
            try:
                customs["api_status"] = int(vars_obj["api_status"])
            except Exception:
                customs["api_status"] = vars_obj["api_status"]
        if "api_body" in vars_obj and "api_body" not in customs:
            customs["api_body"] = vars_obj["api_body"]

        vars_obj["customs"] = customs
        state["variables"] = vars_obj

        customs_before = dict(customs) if isinstance(customs, dict) else {}
        system_before = dict(system) if isinstance(system, dict) else {}
        utils_before = dict(utils) if isinstance(utils, dict) else {}

        system_ctx = dict(system)
        system_ctx_with_utils = dict(system_ctx)
        system_ctx_with_utils["utils"] = dict(utils)

        js_ctx = {
            "variables": {
                "system": system_ctx_with_utils,
                "customs": customs,
                "utils": utils
            },
            "customs": customs,
            "branches": {lbl: lbl for lbl in branch_labels},
        }

        if dbg_on():
            LOGGER.debug("---------> has export (raw)? %s", "export default" in code_raw)
            LOGGER.debug("---------> has export (exec)? %s", "export default" in code_exec)
            LOGGER.debug(
                "---------> has async (exec)? %s",
                ("async function" in code_exec) or ("async(" in code_exec),
            )

        # 6) Executar JS
        res = run_js_code(code_exec, js_ctx, timeout_ms=timeout_ms)
        logs = res.get("logs") or []
        js_err = res.get("error")
        js_error_type = res.get("error_type")
        alias_linked = res.get("customs_alias_linked")

        # 7) Merge granular ctx.variables => state.variables (HÍBRIDO)
        out_ctx = res.get("ctx")
        if not isinstance(out_ctx, dict):
            out_ctx = {}
        out_vars = out_ctx.get("variables")
        if not isinstance(out_vars, dict):
            fallback_vars = js_ctx.get("variables")
            out_vars = dict(fallback_vars) if isinstance(fallback_vars, dict) else {}
            out_ctx["variables"] = out_vars

        merged = (state.get("variables") or {}).copy()

        merged_customs = dict(merged.get("customs") or {})
        out_customs: Dict[str, Any] = {}
        if isinstance(out_vars.get("customs"), dict):
            out_customs.update(out_vars.get("customs") or {})
        if isinstance(out_ctx.get("customs"), dict):
            out_customs.update(out_ctx.get("customs") or {})
        for k, v in out_customs.items():
            merged_customs[k] = v
        merged["customs"] = merged_customs
        customs_after = merged_customs

        merged_system = dict(merged.get("system") or {})
        for k, v in (out_vars.get("system") or {}).items():
            merged_system[k] = v

        outgoing_utils_sources: List[Dict[str, Any]] = []
        system_outgoing = out_vars.get("system")
        if isinstance(system_outgoing, dict):
            utils_candidate = system_outgoing.get("utils")
            if isinstance(utils_candidate, dict):
                outgoing_utils_sources.append(utils_candidate)
        utils_root = out_vars.get("utils")
        if isinstance(utils_root, dict):
            outgoing_utils_sources.append(utils_root)

        merged_utils = dict(merged.get("utils") or {})
        for source in outgoing_utils_sources:
            for key, value in source.items():
                merged_utils[key] = value

        merged["system"] = merged_system
        merged["utils"] = merged_utils
        merged = normalize_variables_structure(merged)
        state["variables"] = merged
        vars_obj = merged
        system_after = dict(merged.get("system") or {})
        utils_after = dict(merged.get("utils") or {})

        customs_updates = _collect_updates(customs_before, customs_after)
        system_updates = _collect_updates(system_before, system_after)
        utils_updates = _collect_updates(utils_before, utils_after)

        # 8) Outcome (branch)
        outcome_raw = res.get("branch")
        outcome = str(outcome_raw).strip() if isinstance(outcome_raw, str) else ""
        if not outcome:
            outcome = "error"

        # 9) Debug/Logs
        if dbg_on():
            LOGGER.debug(
                "[code_editor] session_id=%s ref_id=%s branch=%s",
                session_id,
                cmd.ref_id,
                outcome,
            )
            if js_err:
                LOGGER.debug("error: %s", js_err)
            if logs:
                LOGGER.debug("logs:")
                for ln in logs[:50]:
                    LOGGER.debug(" - %s", ln)

        # 10) Trace: inclui erros e (se houver) infos de template
        preferred_outcomes = []
        if js_err:
            preferred_outcomes.append("exception")
        preferred_outcomes.append(outcome)
        if "error" not in preferred_outcomes:
            preferred_outcomes.append("error")
        seen_outcomes: set[str] = set()
        preferred_outcomes = [
            candidate
            for candidate in preferred_outcomes
            if candidate and (candidate not in seen_outcomes and not seen_outcomes.add(candidate))
        ]

        if trace_enabled():
            trace: Dict[str, Any] = {
                "branch": outcome,
                "result": "exception" if js_err else ("success" if outcome == "success" else outcome),
                "timeout_ms": timeout_ms,
                "branch_attempts": preferred_outcomes[:5],
            }
            if timeout_note:
                trace["timeout_note"] = timeout_note
            if js_err:
                trace["error"] = str(js_err)
                trace["error_type"] = str(js_error_type or "runtime_error")
                trace["error_message"] = str(js_err)
            if logs:
                trace["logs"] = logs[:5]
            if isinstance(alias_linked, bool):
                trace["customs_alias_linked"] = alias_linked
            if resolved_map:
                trace["template_resolved_keys"] = list(resolved_map.keys())[:20]
            if missing_keys:
                trace["template_missing_keys"] = missing_keys[:20]
            if customs_updates:
                trace["customs_updates"] = customs_updates
            if system_updates:
                trace["system_updates"] = system_updates
            if utils_updates:
                trace["utils_updates"] = utils_updates
            batch.trace = trace

        branch_to = helpers.get("branch_to")
        compute_cut = helpers.get("compute_cut")
        for candidate in preferred_outcomes:
            try:
                to_ref = branch_to(cmd.ref_id, candidate) if callable(branch_to) else None
            except Exception:
                to_ref = None
            if not to_ref:
                continue
            if batch.trace is not None:
                batch.trace["branch"] = candidate
                cut_idx = compute_cut(cmd.ref_id, to_ref) if callable(compute_cut) else None
                return ExecResult(
                    continue_loop=True,
                    to_ref=to_ref,
                    cut_index=cut_idx,
                    batch_entry=batch,
                )

        if js_err:
            if batch.trace is not None:
                batch.trace["unrouted_js_error"] = True
            return ExecResult(
                continue_loop=False,
                to_ref=None,
                batch_entry=batch,
                error=str(js_err),
            )

        return ExecResult(continue_loop=True, to_ref=None, batch_entry=batch)
