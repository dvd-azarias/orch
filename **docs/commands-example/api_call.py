# app/commands/api_call.py
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import requests

from app.commands.base import CommandHandler, ExecResult, BatchEntry
from app.services.template_resolver import render_string, render_obj  # <-- novo
from app.config import get_settings
from app.services import usage_logger, metrics_utils

_settings = get_settings()

LOGGER = logging.getLogger("target_core.commands.api_call")


def _dbg_on() -> bool:
    return _settings.api_call_debug

def _trace_enabled() -> bool:
    return _settings.trace_in_ref_path

def _as_dict_list(pairs: Any) -> Dict[str, Any]:
    """
    Converte uma lista de pares (ou dict) em dict, ignorando chaves vazias.
    Aceita formatos:
      - [{"key":"a","value":"1"}, {"key":"b","value":"2"}]
      - [["a","1"], ["b","2"]]
      - {"a":"1", "b":"2"}
    """
    if not pairs:
        return {}
    if isinstance(pairs, dict):
        return {str(k): v for k, v in pairs.items() if str(k)}
    out: Dict[str, Any] = {}
    if isinstance(pairs, list):
        for item in pairs:
            if isinstance(item, dict):
                k = str(item.get("key") or "")
                if not k:
                    continue
                out[k] = item.get("value")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                k = str(item[0] or "")
                if not k:
                    continue
                out[k] = item[1]
    return out

def _build_url_with_query(base_url: str, query_items: Any) -> str:
    """
    Anexa query params preservando os existentes.
    """
    base_url = (base_url or "").strip()
    if not base_url:
        return ""
    try:
        parsed = urlparse(base_url)
    except Exception:
        return base_url
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    incoming = _as_dict_list(query_items)
    merged = {**existing, **{k: "" if v is None else str(v) for k, v in incoming.items()}}
    new_query = urlencode(merged, doseq=False)
    rebuilt = parsed._replace(query=new_query)
    return urlunparse(rebuilt)

def _coerce_json_from_maybe_string(val: Any) -> Tuple[Optional[dict], Optional[str]]:
    """
    Se val é dict/list -> retorna (val, None)
    Se val é str -> tenta json.loads; se falhar -> (None, val)
    Outros tipos -> tenta json.dumps+loads; senão -> (None, str(val))
    """
    if val is None:
        return None, None
    if isinstance(val, (dict, list)):
        return val, None
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return json.loads(s), None
            except Exception:
                return None, val
        return None, val
    try:
        return json.loads(json.dumps(val)), None
    except Exception:
        return None, str(val)

def _headers_list_to_dict(headers_any: Any) -> Dict[str, str]:
    """
    Converte headers no formato de lista/dict para dict simples {name: value}, ignorando nomes vazios.
    """
    if not headers_any:
        return {}
    if isinstance(headers_any, dict):
        return {str(k): str(v) for k, v in headers_any.items() if str(k)}
    out: Dict[str, str] = {}
    if isinstance(headers_any, list):
        for h in headers_any:
            if isinstance(h, dict):
                name = str(h.get("name") or h.get("key") or "").strip()
                if not name:
                    continue
                val = h.get("value")
                out[name] = "" if val is None else str(val)
            elif isinstance(h, (list, tuple)) and len(h) >= 2:
                name = str(h[0] or "").strip()
                if not name:
                    continue
                val = h[1]
                out[name] = "" if val is None else str(val)
    return out

def _ensure_default_accept_and_content_type(headers: Dict[str, str], body_mode: str) -> Dict[str, str]:
    """
    Seta defaults úteis sem sobrescrever o que o cliente já passou.
    """
    has_accept = any(k.lower() == "accept" for k in headers.keys())
    has_ct     = any(k.lower() == "content-type" for k in headers.keys())
    if not has_accept:
        headers["Accept"] = "application/json"
    if not has_ct:
        if body_mode == "json":
            headers["Content-Type"] = "application/json"
        elif body_mode == "form":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body_mode == "text":
            headers["Content-Type"] = "text/plain; charset=utf-8"
    return headers

def _apply_auth(headers: Dict[str, str], auth_cfg: dict) -> Tuple[Dict[str, str], Optional[Tuple[str, str]]]:
    """
    Aplica auth:
      - none  -> nada
      - bearer-> Authorization: Bearer <token> (não sobrescreve se já existir)
      - basic -> retorna auth=(username, password) p/ requests
    """
    headers = dict(headers or {})
    basic_auth: Optional[Tuple[str, str]] = None
    t = (auth_cfg or {}).get("type") or "none"
    t = str(t).lower().strip()
    if t == "bearer":
        token = (auth_cfg or {}).get("token") or ""
        has_auth = any(k.lower() == "authorization" for k in headers.keys())
        if (token or "").strip() and not has_auth:
            headers["Authorization"] = f"Bearer {token}"
    elif t == "basic":
        username = (auth_cfg or {}).get("username") or ""
        password = (auth_cfg or {}).get("password") or ""
        basic_auth = (username, password)
    return headers, basic_auth


def _truncate(value: Union[str, bytes], limit: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except Exception:
            value = value.decode("latin-1", errors="ignore")
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _preview_payload(payload: Any, limit: int = 500) -> Optional[str]:
    if payload is None:
        return None
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except Exception:
        text = str(payload)
    return _truncate(text, limit)


def _preview_error_detail(detail: Any, limit: int = 500) -> Optional[str]:
    if detail is None:
        return None
    if isinstance(detail, str):
        return _truncate(detail, limit)
    if isinstance(detail, dict):
        keys = set(detail.keys())
        if keys == {"_text"}:
            text_val = detail.get("_text")
            if isinstance(text_val, str):
                return _truncate(text_val, limit)
    return _preview_payload(detail, limit)


class ApiCallHandler(CommandHandler):
    """
    Componente api_call (contrato parameters.request.*)
    - request: {
        url, method, query: [pairs], headers: [pairs], timeout,
        auth: { type: none|basic|bearer, username, password, token },
        body: { mode: json|form|text|none, json, form: [pairs], text },
        response: { status, body, headers, error }  # nomes das chaves em variables.customs
      }
    - Salva SEMPRE em variables.customs as chaves mapeadas por 'response'
    """
    component_id = "api_call"

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
        req    = params.get("request") or {}

        # --- Ler request.* conforme contrato ---
        url_raw   = (req.get("url") or "").strip()
        method    = str(req.get("method") or "GET").upper().strip()
        timeout_ms = req.get("timeout")
        timeout_s  = max(0.001, (timeout_ms / 1000.0)) if isinstance(timeout_ms, (int, float)) else 3.0

        query_items = req.get("query") or []
        headers_dict = _headers_list_to_dict(req.get("headers"))
        auth_cfg  = req.get("auth") or {}
        body_cfg  = req.get("body") or {}
        resp_map  = req.get("response") or {}

        # nomes dos campos em variables.customs
        key_status = str(resp_map.get("status")  or "api_status")
        key_body   = str(resp_map.get("body")    or "api_body")
        key_hdrs   = str(resp_map.get("headers") or "") or None
        key_error  = str(resp_map.get("error")   or "") or None
        key_request_url = str(resp_map.get("request_url") or "api_request_url")
        key_request_payload = str(resp_map.get("request_payload") or "api_request_payload")
        key_request_url = key_request_url if key_request_url.strip() else None
        key_request_payload = key_request_payload if key_request_payload.strip() else None

        # Branch labels
        branch_success = str(params.get("branch_success") or "success")
        branch_error   = str(params.get("branch_error")   or "error")

        # -------------------------------
        # 1) RENDERIZAÇÃO DE PLACEHOLDERS
        # -------------------------------
        variables = state.get("variables") or {}
        system_vars = variables.get("system") or {}
        workspace_uuid = (
            system_vars.get("workspace_uuid")
            or system_vars.get("workspace_id")
            or system_vars.get("workspace")
        )
        flow_id = system_vars.get("flow_id")
        flow_name = system_vars.get("flow_name")
        flow_mode = system_vars.get("mode")
        session_channel = system_vars.get("channel")
        contact_block = system_vars.get("contact")
        contact_id = contact_block.get("id") if isinstance(contact_block, dict) else None
        call_type_param = str(params.get("call_type") or "api").strip() or "api"

        # URL + query
        url_rendered, miss_url, res_url = render_string(url_raw, variables)
        # query (pares) → render recursivo em valores
        query_rendered, miss_q, res_q = render_obj(query_items, variables)

        # Headers
        headers_rendered_pairs, miss_h, res_h = render_obj(req.get("headers") or [], variables)
        headers_dict = _headers_list_to_dict(headers_rendered_pairs)

        # Auth
        auth_rendered, miss_a, res_a = render_obj(auth_cfg, variables)

        # Body
        body_rendered, miss_b, res_b = render_obj(body_cfg, variables)

        # Agrega (para trace)
        missing_all = (miss_url or []) + (miss_q or []) + (miss_h or []) + (miss_a or []) + (miss_b or [])
        resolved_all: Dict[str, Any] = {}
        for d in (res_url, res_q, res_h, res_a, res_b):
            if d:
                resolved_all.update(d)

        # --- URL + Query ---
        url = _build_url_with_query(url_rendered, query_rendered)

        # Body mode
        body_mode = str((body_rendered or {}).get("mode") or "").lower().strip()
        if body_mode not in ("json", "form", "text", "none", ""):
            body_mode = "json"

        # Defaults de headers
        headers_dict = _ensure_default_accept_and_content_type(headers_dict, body_mode)

        # Auth
        headers_dict, basic_auth = _apply_auth(headers_dict, auth_rendered or {})

        # Serialização de body conforme modo
        json_payload: Optional[dict] = None
        data_payload: Optional[Any]  = None
        if body_mode == "json":
            json_payload, data_payload = _coerce_json_from_maybe_string((body_rendered or {}).get("json"))
        elif body_mode == "form":
            data_payload = _as_dict_list((body_rendered or {}).get("form"))
        elif body_mode == "text":
            data_payload = (body_rendered or {}).get("text")
        else:
            json_payload, data_payload = None, None

        # === Validação de URL ===
        request_log_obj = {
            "method": method,
            "url": url,
            "headers": headers_dict,
            "body": json_payload if json_payload is not None else data_payload,
        }

        if not (url.startswith("http://") or url.startswith("https://")):
            # grava erro direto e desvia
            self._store_result(
                state=state,
                key_status=key_status,
                key_body=key_body,
                key_hdrs=key_hdrs,
                key_error=key_error,
                status=0,
                body=None,
                headers=None,
                error="invalid_url",
                request_url=url_raw or None,
                request_payload=None,
                key_request_url=key_request_url,
                key_request_payload=key_request_payload,
            )
            usage_logger.log_usage(
                endpoint="command/api_call",
                workspace_id=workspace_uuid,
                flow_id=flow_id,
                session_id=session_id,
                flow_name=flow_name,
                mode=flow_mode,
                request_obj=request_log_obj,
                response_obj={"error": "invalid_url"},
                status_code=400,
            )
            batch = BatchEntry(selected=None)
            if _trace_enabled():
                batch.trace = {
                    "status": 0,
                    "exception": "invalid_url",
                    "detail": f"URL invalida ou vazia: {url_raw!r}",
                    "missing_vars": missing_all[:20] if missing_all else None,
                }
            return self._dispatch_outcomes(helpers, cmd, batch, ["exception", branch_error])

        # --- LOG REQUEST ---
        if _dbg_on():
            print("[api_call] === REQUEST =======================================")
            print(f"session_id: {session_id}")
            print(f"ref_id    : {cmd.ref_id}")
            print(f"method    : {method}")
            print(f"url       : {url}")
            try:
                printable_payload = json.dumps(json_payload if json_payload is not None else data_payload, ensure_ascii=False)
            except Exception:
                printable_payload = str(json_payload if json_payload is not None else data_payload)
            print(f"payload   : {printable_payload}")
            print(f"headers   : {headers_dict}")
            if missing_all:
                print(f"[api_call] unresolved_placeholders={missing_all}")
            if resolved_all:
                # exibe apenas as chaves resolvidas (não os valores sensíveis)
                print(f"[api_call] resolved_keys={list(resolved_all.keys())[:20]}")
            print("=================================================================")

        # --- FAZ REQUEST ---
        status: Optional[int] = None
        resp_json: Optional[dict] = None
        resp_text: Optional[str] = None
        exc: Optional[str] = None
        response: Optional[requests.Response] = None
        t0 = time.time()
        try:
            kwargs: Dict[str, Any] = {
                "headers": headers_dict,
                "timeout": max(0.001, float(timeout_s)),
                "auth": basic_auth,
            }
            if method != "GET":
                if json_payload is not None:
                    kwargs["json"] = json_payload
                elif data_payload is not None:
                    kwargs["data"] = data_payload
            response = requests.request(method, url, **kwargs)
            status = response.status_code

            # tenta json
            try:
                resp_json = response.json()
            except Exception:
                resp_text = response.text

            # LOG RESPONSE
            if _dbg_on():
                print("[api_call] --- RESPONSE -------------------------------------")
                print(f"status    : {status}")
                try:
                    hdrs_preview = dict(response.headers)
                except Exception:
                    hdrs_preview = {}
                print(f"headers   : {hdrs_preview}")
                if resp_json is not None:
                    body_preview = json.dumps(resp_json, ensure_ascii=False)[:800]
                else:
                    body_preview = (resp_text or "")[:800]
                print(f"body      : {body_preview}")
                print("[api_call] ===================================================")

        except Exception as e:
            exc = str(e)
            if _dbg_on():
                print("[api_call] !!! EXCEPTION !!! -------------------------------")
                print(exc)
                print("[api_call] ===================================================")

        duration_ms: Optional[float] = None
        try:
            duration_ms = round((time.time() - t0) * 1000.0, 2)
        except Exception:
            duration_ms = None

        # --- PERSISTE RESULTADO EM variables.customs (sempre) ---
        if resp_json is not None:
            body_to_store: Any = resp_json
        elif resp_text is not None:
            body_to_store = {"_text": resp_text}
        else:
            body_to_store = None
        request_payload_to_store: Any = None
        if json_payload is not None:
            request_payload_to_store = json_payload
        elif data_payload is not None:
            request_payload_to_store = data_payload

        hdrs_to_store: Optional[dict] = None
        try:
            hdrs_to_store = dict(response.headers) if response is not None else None
        except Exception:
            hdrs_to_store = None

        final_status = int(status or 0)

        error_detail: Optional[Any] = exc
        if error_detail is None and final_status >= 400:
            if resp_json is not None:
                error_detail = resp_json
            elif resp_text is not None:
                error_detail = resp_text
            elif response is not None:
                try:
                    error_detail = response.reason or f"HTTP {final_status}"
                except Exception:
                    error_detail = f"HTTP {final_status}"

        self._store_result(
            state=state,
            key_status=key_status,
            key_body=key_body,
            key_hdrs=key_hdrs,
            key_error=key_error,
            status=final_status,
            body=body_to_store,
            headers=hdrs_to_store,
            error=error_detail,
            request_url=url,
            request_payload=request_payload_to_store,
            key_request_url=key_request_url,
            key_request_payload=key_request_payload,
        )

        response_log_obj = {
            "status": final_status,
            "body": body_to_store,
            "headers": hdrs_to_store,
            "duration_ms": duration_ms,
            "error": error_detail,
        }
        if exc and exc is not error_detail:
            response_log_obj["exception"] = exc
        usage_logger.log_usage(
            endpoint="command/api_call",
            workspace_id=workspace_uuid,
            flow_id=flow_id,
            session_id=session_id,
            flow_name=flow_name,
            mode=flow_mode,
            request_obj=request_log_obj,
            response_obj=response_log_obj,
            status_code=final_status or 500,
        )

        engine = helpers.get("engine")
        if engine is not None:
            try:
                metrics_utils.record_flow_external_called(
                    engine.store,
                    session_id=session_id,
                    workspace_id=str(workspace_uuid) if workspace_uuid else "",
                    flow_id=str(flow_id) if flow_id else None,
                    channel=session_channel,
                    call_type=call_type_param,
                    endpoint=url,
                    response_time_ms=int(duration_ms) if isinstance(duration_ms, (int, float)) else None,
                    method=method,
                    request_body=request_payload_to_store,
                    response_status=final_status,
                    contact_id=contact_id,
                    use_codex_sync=True,
                )
            except Exception:
                LOGGER.exception(
                    "api_call.metrics_external_called_failed",
                    extra={"session_id": session_id, "ref_id": cmd.ref_id},
                )

        # --- TRACE ---
        batch = BatchEntry(selected=None)
        if _trace_enabled():
            trace: Dict[str, Any] = {
                "status": status,
                "exception": exc,
                "method": method,
                "url": url,
                "duration_ms": duration_ms,
            }
            if missing_all:
                trace["missing_vars"] = missing_all[:20]
            if resolved_all:
                trace["resolved_keys"] = list(resolved_all.keys())[:20]

            request_headers_preview = {k: _truncate(v, 120) for k, v in (headers_dict or {}).items()}
            if request_headers_preview:
                trace["request_headers"] = request_headers_preview

            if json_payload is not None or data_payload is not None:
                trace["request_body"] = _preview_payload(json_payload if json_payload is not None else data_payload)

            if response is not None:
                try:
                    trace["response_headers"] = {k: _truncate(v, 120) for k, v in dict(response.headers).items()}
                except Exception:
                    trace["response_headers"] = None
            if error_detail is not None:
                trace["error"] = _preview_error_detail(error_detail)

            if body_to_store is not None:
                trace["response_body"] = _preview_payload(body_to_store)

            batch.trace = trace

        # --- BRANCHING (2xx => success) ---
        success_outcome = status is not None and 200 <= status < 300 and exc is None
        outcome_candidates: List[str] = []
        if success_outcome:
            outcome_candidates.append(branch_success)
        else:
            if exc is not None:
                outcome_candidates.append("exception")
            outcome_candidates.append(branch_error)
        return self._dispatch_outcomes(helpers, cmd, batch, outcome_candidates)

    # --------------------------
    # Helpers de persistência
    # --------------------------
    def _store_result(
        self,
        *,
        state: Dict[str, Any],
        key_status: str,
        key_body: str,
        key_hdrs: Optional[str],
        key_error: Optional[str],
        key_request_url: Optional[str],
        key_request_payload: Optional[str],
        status: int,
        body: Any,
        headers: Optional[dict],
        error: Optional[str],
        request_url: Optional[str],
        request_payload: Any,
    ) -> None:
        """
        Persiste SEMPRE em variables.customs as chaves mapeadas por request.response.
        Limpa possíveis chaves legadas no topo.
        """
        vars_obj = state.get("variables") or {}
        customs  = dict(vars_obj.get("customs") or {})

        customs[key_status] = int(status or 0)
        customs[key_body]   = body

        if key_hdrs:
            customs[key_hdrs] = headers or {}
        if key_error:
            customs[key_error] = (error or None)
        if key_request_url:
            customs[key_request_url] = request_url
        if key_request_payload:
            customs[key_request_payload] = request_payload

        vars_obj["customs"] = customs

        for legacy in ("api_status", "api_body", "api_headers", "api_error"):
            if legacy in vars_obj:
                vars_obj.pop(legacy, None)

        state["variables"] = vars_obj

    def _dispatch_outcomes(
        self,
        helpers: Dict[str, Any],
        cmd,
        batch: BatchEntry,
        candidates: List[str],
    ) -> ExecResult:
        branch_to = helpers.get("branch_to")
        compute_cut = helpers.get("compute_cut")

        seen: set[str] = set()
        deduped: List[str] = []
        for candidate in candidates:
            label = str(candidate or "").strip()
            if not label or label in seen:
                continue
            seen.add(label)
            deduped.append(label)

        if batch.trace is not None:
            batch.trace.setdefault("branch_attempts", deduped[:5])

        for label in deduped:
            try:
                to_ref = branch_to(cmd.ref_id, label) if callable(branch_to) else None
            except Exception:
                to_ref = None
            if not to_ref:
                continue
            if batch.trace is not None:
                batch.trace["branch"] = label
            cut_idx = compute_cut(cmd.ref_id, to_ref) if callable(compute_cut) else None
            return ExecResult(
                continue_loop=True,
                to_ref=to_ref,
                cut_index=cut_idx,
                batch_entry=batch,
            )

        return ExecResult(continue_loop=True, to_ref=None, batch_entry=batch)
