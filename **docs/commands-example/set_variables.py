# app/commands/set_variables.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from app.commands.base import CommandHandler, ExecResult, BatchEntry
from app.common.variables import normalize_variables_structure
from app.services.template_resolver import render_string, render_obj
from app.config import get_settings

_settings = get_settings()


def _dbg_on() -> bool:
    return _settings.set_variables_debug


def _trace_enabled() -> bool:
    return _settings.trace_in_ref_path


def _coerce_json_strings() -> bool:
    return _settings.set_variables_coerce_json


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
    return text if len(text) <= limit else text[:limit] + "…"


def _safe_json_coerce(val: Any) -> Any:
    """
    Se val for string com cara de JSON, tenta json.loads.
    Caso contrário retorna o valor original.
    """
    if not isinstance(val, str):
        return val
    s = val.strip()
    if not s:
        return val
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            return json.loads(s)
        except Exception:
            return val
    return val


def _get_root_for_path(variables: Dict[str, Any], path: str) -> Tuple[Dict[str, Any], str]:
    """
    Decide a RAIZ de escrita a partir do prefixo do path:
      - variables.<...>  → raiz = variables
      - customs.<...>    → raiz = variables['customs']
      - system.<...>     → raiz = variables['system']
      - utils.<...>      → raiz = variables['utils']
      - (sem prefixo)    → raiz = variables['customs'] e path permanece como veio
    Retorna (root_dict, remaining_path) e garante a existência do dicionário raiz.
    """
    variables.setdefault("customs", {})

    system_ref = variables.get("system")
    if not isinstance(system_ref, dict):
        system_ref = {}
        variables["system"] = system_ref

    utils_ref = variables.get("utils")
    if not isinstance(utils_ref, dict):
        utils_ref = {}
        variables["utils"] = utils_ref

    p = str(path or "").strip()
    if p.startswith("variables."):
        return variables, p[len("variables.") :]

    for prefix, key in (
        ("customs.", "customs"),
        ("system.", "system"),
    ):
        if p.startswith(prefix):
            return variables[key], p[len(prefix) :]

    if p.startswith("utils."):
        utils_ref = variables.get("utils")
        if not isinstance(utils_ref, dict):
            utils_ref = {}
            variables["utils"] = utils_ref
        return utils_ref, p[len("utils.") :]

    # sem prefixo → assume customs
    return variables["customs"], p


def _set_by_dot_path(root: Dict[str, Any], path: str, value: Any) -> None:
    """
    Cria/atualiza dicionários intermediários para escrever em a.b.c = value.
    Se path vazio, não faz nada.
    """
    keypath = [seg for seg in (path or "").split(".") if seg]
    if not keypath:
        return

    cur = root
    for seg in keypath[:-1]:
        if not isinstance(cur.get(seg), dict):
            cur[seg] = {}
        cur = cur[seg]
    cur[keypath[-1]] = value


class SetVariablesHandler(CommandHandler):
    """
    Componente set_variables

    Contrato esperado (UI):
      parameters: { "instructions": [...] }

    Regras:
      - Se 'variable' começar com 'variables.' / 'customs.' / 'system.' / 'utils.',
        é interpretado como caminho ABSOLUTO.
      - Caso contrário, o caminho é relativo a 'variables.customs'.
      - Os valores passam pelo template_resolver (placeholders e fuzzy lookup).
      - Se SET_VARIABLES_COERCE_JSON=1, tenta carregar strings com cara de JSON.
      - Sempre persiste em state["variables"] (mutável).
      - Produz trace de resolved/missing quando habilitado.
    """
    component_id = "set_variables"

    def execute(
        self,
        *,
        flow,
        state: Dict[str, Any],
        cmd,
        session_id: str,
        helpers: Dict[str, Any],
    ) -> ExecResult:
        params = cmd.parameters or {}
        variables = normalize_variables_structure(state.get("variables") or {})
        state["variables"] = variables
        instructions = params.get("instructions") or []

        if not isinstance(instructions, list):
            instructions = []

        all_missing: List[str] = []
        resolved_union: Dict[str, Any] = {}
        applied_values: Dict[str, str] = {}
        missing_variables: List[str] = []
        empty_variables: List[str] = []

        # Processa instrução por instrução
        for it in instructions:
            if not isinstance(it, dict):
                continue

            var_name = (it.get("variable") or it.get("key") or "").strip()
            raw_value = it.get("value")

            if not var_name:
                continue

            # Renderiza o valor (pode ser string/dict/list)
            rendered_value, missing, resolved = render_obj(raw_value, variables)
            if missing:
                all_missing.extend(missing)
            if resolved:
                resolved_union.update(resolved)

            # Coerção opcional de JSON em strings
            if _coerce_json_strings():
                rendered_value = _safe_json_coerce(rendered_value)

            # Normaliza casos onde o placeholder permanece no valor (não encontrado)
            value_for_storage = rendered_value
            if missing and isinstance(rendered_value, str):
                stripped = rendered_value.strip()
                if stripped.startswith("{{") and stripped.endswith("}}") and stripped.count("{{") == 1:
                    value_for_storage = None
                    missing_variables.append(var_name)
            if value_for_storage in (None, "", [], {}, ()) and var_name not in missing_variables:
                empty_variables.append(var_name)

            # Decide raiz e caminho relativo
            root, rel_path = _get_root_for_path(variables, var_name)

            # Garante que a raiz seja dict
            if not isinstance(root, dict):
                # Se o usuário apontou para algo que não é dict, substituímos por dict
                # para permitir a criação do caminho (decisão pragmática).
                target_prefix = var_name.split(".", 1)[0]
                if target_prefix == "variables":
                    # 'variables' deve ser dict sempre
                    state["variables"] = variables = {}
                    root = variables
                    rel_path = var_name[len("variables.") :]
                else:
                    # mapeia o prefixo em variables
                    if var_name.startswith("customs."):
                        variables["customs"] = {}
                        root = variables["customs"]
                        rel_path = var_name[len("customs.") :]
                    elif var_name.startswith("system."):
                        variables["system"] = {}
                        root = variables["system"]
                        rel_path = var_name[len("system.") :]
                    elif var_name.startswith("utils."):
                        utils_root = variables.get("utils")
                        if not isinstance(utils_root, dict):
                            utils_root = {}
                        variables["utils"] = utils_root
                        system_root = variables.get("system")
                        if not isinstance(system_root, dict):
                            system_root = {}
                            variables["system"] = system_root
                        system_root["utils"] = utils_root
                        root = utils_root
                        rel_path = var_name[len("utils.") :]
                    else:
                        variables["customs"] = {}
                        root = variables["customs"]
                        rel_path = var_name

            # Escreve no caminho (cria dicionários intermediários conforme necessário)
            _set_by_dot_path(root, rel_path, value_for_storage)

            applied_values[var_name] = _preview_value(value_for_storage)

            if _dbg_on():
                print(f"[set_variables] var={var_name!r} -> value={value_for_storage!r}")

        # Persiste de volta no estado
        state["variables"] = normalize_variables_structure(variables)

        # Trace (opcional)
        batch = BatchEntry(selected="parameters.instructions")
        if _trace_enabled():
            trace: Dict[str, Any] = {}
            if all_missing:
                trace["missing_placeholders"] = list(dict.fromkeys(all_missing))[:30]
            if resolved_union:
                # Por segurança, registra apenas as CHAVES resolvidas
                trace["resolved_keys"] = list(resolved_union.keys())[:30]
            if missing_variables:
                trace["missing_variables"] = list(dict.fromkeys(missing_variables))[:30]
            if empty_variables:
                trace["empty_variables"] = list(dict.fromkeys(empty_variables))[:30]
            if applied_values:
                ordered = list(applied_values.items())[:20]
                trace["applied"] = [{"variable": key, "value": value} for key, value in ordered]
            if trace:
                batch.trace = trace

        return ExecResult(continue_loop=True, to_ref=None, batch_entry=batch)
