from __future__ import annotations

import re
from typing import Iterable, List, Optional

from app.commands.utils import transform_export_default

_MAIN_FN_RE = re.compile(r"function\s+main\s*\(\s*ctx\s*\)\s*\{", re.MULTILINE)
_DECLARATION_RE = re.compile(r"\b(?:const|let|var)\b([^;]*)", re.MULTILINE)
_CTX_CUSTOMS_DOT_ASSIGN_RE = re.compile(
    r"\bctx\s*\.\s*(?:variables\s*\.\s*customs|customs)\s*\.\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*"
    r"(?:\+=|-=|\*=|/=|%=|\?\?=|\|\|=|&&=|=(?!=|>))",
    re.MULTILINE,
)
_CTX_CUSTOMS_BRACKET_ASSIGN_RE = re.compile(
    r"\bctx\s*\.\s*(?:variables\s*\.\s*customs|customs)\s*\[\s*['\"]([^'\"\n\r]+)['\"]\s*\]\s*"
    r"(?:\+=|-=|\*=|/=|%=|\?\?=|\|\|=|&&=|=(?!=|>))",
    re.MULTILINE,
)

_JS_RESERVED = {
    "await",
    "break",
    "case",
    "catch",
    "class",
    "const",
    "continue",
    "debugger",
    "default",
    "delete",
    "do",
    "else",
    "enum",
    "export",
    "extends",
    "false",
    "finally",
    "for",
    "function",
    "if",
    "import",
    "in",
    "instanceof",
    "let",
    "new",
    "null",
    "return",
    "super",
    "switch",
    "this",
    "throw",
    "true",
    "try",
    "typeof",
    "var",
    "void",
    "while",
    "with",
    "yield",
}


def _is_identifier(name: str) -> bool:
    return bool(re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", name))


def _find_matching_brace(code: str, open_brace_index: int) -> Optional[int]:
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

    return None


def _extract_main_body(code: str) -> str:
    main_match = _MAIN_FN_RE.search(code)
    if not main_match:
        return ""
    open_brace = code.find("{", main_match.end() - 1)
    if open_brace < 0:
        return ""
    close_brace = _find_matching_brace(code, open_brace)
    if close_brace is None or close_brace <= open_brace:
        return ""
    return code[open_brace + 1 : close_brace]


def _extract_identifiers_from_declaration(chunk: str) -> Iterable[str]:
    for item in chunk.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        name = candidate.split("=", 1)[0].strip()
        if not name:
            continue
        if _is_identifier(name):
            yield name


def _extract_ctx_custom_assignments(body: str) -> Iterable[str]:
    for match in _CTX_CUSTOMS_DOT_ASSIGN_RE.finditer(body):
        name = (match.group(1) or "").strip()
        if not name:
            continue
        yield name

    for match in _CTX_CUSTOMS_BRACKET_ASSIGN_RE.finditer(body):
        name = (match.group(1) or "").strip()
        if not name:
            continue
        yield name


def _extract_main_body_from_code(code: str) -> str:
    if not isinstance(code, str) or not code.strip():
        return ""

    try:
        transformed = transform_export_default(code)
    except Exception:
        transformed = code

    body = _extract_main_body(transformed)
    if not body:
        return ""
    return body


def infer_code_editor_custom_assignments(code: str) -> List[str]:
    body = _extract_main_body_from_code(code)
    if not body:
        return []

    found: set[str] = set()
    for name in _extract_ctx_custom_assignments(body):
        if name in _JS_RESERVED:
            continue
        if name.startswith("__"):
            continue
        found.add(name)

    return sorted(found)


def infer_code_editor_custom_variables(code: str) -> List[str]:
    body = _extract_main_body_from_code(code)
    if not body:
        return []

    found: set[str] = set()
    for decl_match in _DECLARATION_RE.finditer(body):
        for name in _extract_identifiers_from_declaration(decl_match.group(1) or ""):
            if name in _JS_RESERVED:
                continue
            if name.startswith("__"):
                continue
            found.add(name)

    for name in _extract_ctx_custom_assignments(body):
        if name in _JS_RESERVED:
            continue
        if name.startswith("__"):
            continue
        found.add(name)

    return sorted(found)


__all__ = [
    "infer_code_editor_custom_variables",
    "infer_code_editor_custom_assignments",
]
