# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Symbol extraction and syntax checking.

Uses tree-sitter (tree_sitter_language_pack) when available; falls back to regex
symbol extraction and plain-text mode (no syntax check except Python's `ast`).
Approach inspired by Aider's repo map (aider/repomap.py, Apache-2.0): definitions
come from the syntax tree, ranking comes from how often other files mention them.
No code was copied; we walk node types instead of running .scm tag queries.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from typing import Optional

EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cxx": "cpp",
    ".rb": "ruby", ".cs": "csharp", ".kt": "kotlin", ".php": "php", ".swift": "swift",
}

# node type -> kind, per language. Name comes from field "name" unless noted.
DEF_NODES = {
    "python": {"function_definition": "def", "class_definition": "class"},
    "javascript": {
        "function_declaration": "function", "generator_function_declaration": "function",
        "class_declaration": "class", "method_definition": "method",
    },
    "typescript": {
        "function_declaration": "function", "class_declaration": "class",
        "abstract_class_declaration": "class", "method_definition": "method",
        "interface_declaration": "interface", "type_alias_declaration": "type",
        "enum_declaration": "enum", "module": "namespace",
    },
    "go": {"function_declaration": "func", "method_declaration": "method", "type_spec": "type"},
    "rust": {
        "function_item": "fn", "struct_item": "struct", "enum_item": "enum",
        "trait_item": "trait", "impl_item": "impl", "mod_item": "mod", "type_item": "type",
    },
    "java": {
        "class_declaration": "class", "interface_declaration": "interface",
        "enum_declaration": "enum", "record_declaration": "record",
        "method_declaration": "method", "constructor_declaration": "constructor",
    },
    "c": {"function_definition": "function", "struct_specifier": "struct"},
    "cpp": {"function_definition": "function", "class_specifier": "class", "struct_specifier": "struct"},
    "ruby": {"method": "def", "class": "class", "module": "module"},
}
DEF_NODES["tsx"] = DEF_NODES["typescript"]
CONTAINERS = {"class", "interface", "impl", "trait", "mod", "module", "namespace", "struct", "enum", "record"}

_parsers: dict = {}
_ts_failed: set = set()


@dataclass
class Symbol:
    name: str
    qualname: str
    kind: str
    start: int  # 1-based
    end: int  # 1-based inclusive
    signature: str


def lang_for(path: str) -> Optional[str]:
    return EXT_LANG.get(os.path.splitext(path)[1].lower())


def get_parser(lang: str):
    if os.environ.get("AGENTKIT_NO_TREESITTER"):
        return None
    if lang in _parsers:
        return _parsers[lang]
    if lang in _ts_failed:
        return None
    try:
        from tree_sitter_language_pack import get_parser as _gp  # type: ignore

        p = _gp(lang)
    except Exception:
        _ts_failed.add(lang)
        return None
    _parsers[lang] = p
    return p


def treesitter_available() -> bool:
    return get_parser("python") is not None


# ---------------------------------------------------------------- symbol extraction


def _node_name(node, lang: str, src: bytes) -> Optional[str]:
    n = node.child_by_field_name("name")
    if n is None and lang == "rust" and node.type == "impl_item":
        n = node.child_by_field_name("type")
    if n is None and lang in ("c", "cpp") and node.type == "function_definition":
        d = node.child_by_field_name("declarator")
        while d is not None and d.child_by_field_name("declarator") is not None:
            d = d.child_by_field_name("declarator")
        n = d
    if n is None:
        return None
    return src[n.start_byte:n.end_byte].decode("utf-8", "replace")


def _first_line(src_lines: list, start: int) -> str:
    s = src_lines[start - 1].strip() if 0 < start <= len(src_lines) else ""
    return s[:120]


def _ts_symbols(text: str, lang: str) -> Optional[list]:
    parser = get_parser(lang)
    if parser is None or lang not in DEF_NODES:
        return None
    src = text.encode("utf-8")
    try:
        tree = parser.parse(src)
    except Exception:
        return None
    lines = text.splitlines()
    defs = DEF_NODES[lang]
    out: list = []

    def walk(node, parents):
        kind = defs.get(node.type)
        name = None
        if kind:
            name = _node_name(node, lang, src)
        elif lang in ("javascript", "typescript", "tsx") and node.type == "variable_declarator":
            v = node.child_by_field_name("value")
            if v is not None and v.type in ("arrow_function", "function_expression", "function", "class"):
                kind = "function" if v.type != "class" else "class"
                name = _node_name(node, lang, src)
        if kind and name:
            start, end = node.start_point[0] + 1, node.end_point[0] + 1
            q = ".".join(parents + [name])
            out.append(Symbol(name, q, kind, start, end, _first_line(lines, start)))
            if kind in CONTAINERS or kind in ("class",):
                parents = parents + [name]
        for c in node.children:
            walk(c, parents)

    import sys

    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 10000))
    try:
        walk(tree.root_node, [])
    except RecursionError:
        pass
    finally:
        sys.setrecursionlimit(old)
    return out


_FALLBACK_RX = {
    "python": re.compile(r"^(?P<ind>[ \t]*)(?:async\s+)?(?P<kind>def|class)\s+(?P<name>\w+)"),
    "javascript": re.compile(
        r"^(?P<ind>[ \t]*)(?:export\s+)?(?:default\s+)?(?:async\s+)?(?P<kind>function\*?|class)\s+(?P<name>[\w$]+)"
    ),
    "go": re.compile(r"^(?P<ind>)(?P<kind>func|type)\s+(?:\([^)]*\)\s*)?(?P<name>\w+)"),
    "rust": re.compile(
        r"^(?P<ind>[ \t]*)(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?P<kind>fn|struct|enum|trait|impl|mod)\s+(?:<[^>]*>\s*)?(?P<name>\w+)"
    ),
    "java": re.compile(
        r"^(?P<ind>[ \t]*)(?:(?:public|private|protected|static|final|abstract)\s+)*(?P<kind>class|interface|enum|record)\s+(?P<name>\w+)"
    ),
}
_FALLBACK_RX["typescript"] = _FALLBACK_RX["tsx"] = _FALLBACK_RX["javascript"]


def _regex_symbols(text: str, lang: str) -> list:
    rx = _FALLBACK_RX.get(lang)
    if rx is None:
        return []
    lines = text.splitlines()
    found = []
    for i, ln in enumerate(lines, 1):
        m = rx.match(ln)
        if m:
            found.append((i, len(m.group("ind").expandtabs()), m.group("kind").rstrip("*"), m.group("name")))
    out = []
    stack: list = []  # (indent, name)
    for idx, (i, ind, kind, name) in enumerate(found):
        # end: line before next symbol with indent <= ind (python) or next symbol (others)
        end = len(lines)
        for j, ind2, _k, _n in found[idx + 1:]:
            if ind2 <= ind:
                end = j - 1
                break
        while end > i and not lines[end - 1].strip():
            end -= 1
        while stack and stack[-1][0] >= ind:
            stack.pop()
        q = ".".join([s[1] for s in stack] + [name])
        out.append(Symbol(name, q, kind, i, end, _first_line(lines, i)))
        if kind in ("class", "struct", "impl", "trait", "interface", "enum", "mod"):
            stack.append((ind, name))
    return out


def extract_symbols(text: str, path: str) -> tuple[list, str]:
    """Returns (symbols, engine) where engine is 'tree-sitter' | 'regex' | 'none'."""
    lang = lang_for(path)
    if not lang:
        return [], "none"
    syms = _ts_symbols(text, lang)
    if syms is not None:
        return syms, "tree-sitter"
    return _regex_symbols(text, lang), "regex"


def find_symbol(text: str, path: str, name: str) -> list:
    syms, _ = extract_symbols(text, path)
    exact = [s for s in syms if s.qualname == name]
    if exact:
        return exact
    return [s for s in syms if s.name == name or s.qualname.endswith("." + name)]


# ---------------------------------------------------------------- syntax check


@dataclass
class SyntaxIssue:
    line: int
    col: int
    msg: str


def syntax_errors(text: str, path: str) -> tuple[Optional[list], str]:
    """Returns (issues or None if unchecked, engine)."""
    lang = lang_for(path)
    if lang == "python":
        try:
            ast.parse(text, filename=path)
            return [], "python-ast"
        except SyntaxError as e:
            return [SyntaxIssue(e.lineno or 0, e.offset or 0, e.msg)], "python-ast"
        except ValueError as e:  # null bytes etc.
            return [SyntaxIssue(0, 0, str(e))], "python-ast"
    if not lang:
        return None, "none"
    parser = get_parser(lang)
    if parser is None:
        return None, "none"
    try:
        tree = parser.parse(text.encode("utf-8"))
    except Exception:
        return None, "none"
    issues: list = []
    if tree.root_node.has_error:
        stack = [tree.root_node]
        while stack and len(issues) < 20:
            n = stack.pop()
            if n.type == "ERROR" or n.is_missing:
                what = f"missing {n.type}" if n.is_missing else "syntax error"
                issues.append(SyntaxIssue(n.start_point[0] + 1, n.start_point[1] + 1, what))
                continue
            if n.has_error:
                stack.extend(reversed(n.children))
        issues.sort(key=lambda s: (s.line, s.col))
        if not issues:
            issues.append(SyntaxIssue(1, 1, "syntax error"))
    return issues, "tree-sitter"
