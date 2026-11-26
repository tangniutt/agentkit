# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Pluggable code intelligence: builtin lexical/symbol search and optional XERJ backend."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from .core import AKError, Ctx, Result
from .fsutil import list_files, read_text


class CodeIntelError(RuntimeError):
    pass


_SENSITIVE_ENV = re.compile(r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?|AUTH)(?:$|_)", re.IGNORECASE)


def _lsp_env(config: dict) -> dict:
    env = dict(os.environ)
    model_keys = {model["key_env"] for model in (config.get("models") or {}).values()
                  if isinstance(model, dict) and model.get("key_env")}
    for name in tuple(env):
        if name in model_keys or _SENSITIVE_ENV.search(name):
            env.pop(name)
    return env


def _cfg(ctx: Ctx) -> dict:
    return (ctx.config or {}).get("codeintel") or {}


def _runtime_cfg(ctx: Ctx) -> dict:
    return {**(ctx.config or {}), **(_cfg(ctx) or {})}


def _backend(cfg: dict) -> str:
    value = str(cfg.get("backend", "auto")).lower()
    return value if value in ("auto", "builtin", "xerj") else "auto"


def xerj_executable(cfg: dict) -> Optional[str]:
    return str(cfg.get("xerj_path") or shutil.which("xerj") or "") or None


def xerj_available(cfg: dict) -> bool:
    return xerj_executable(cfg) is not None


def lsp_server(cfg: dict, language: str) -> dict:
    servers = cfg.get("lsp") or {}
    server = servers.get(language) or {}
    if not isinstance(server, dict):
        raise CodeIntelError(f"codeintel.lsp.{language} must be a table")
    return server


def lsp_available(cfg: dict, language: str) -> bool:
    try:
        server = lsp_server(cfg, language)
        raw = server.get("command") or []
        if isinstance(raw, str):
            raw = shlex.split(raw)
        return bool(raw) and (shutil.which(str(raw[0])) is not None or Path(str(raw[0])).is_file())
    except (CodeIntelError, ValueError):
        return False


def _xerj_env(cfg: dict) -> dict:
    env = dict(os.environ)
    env["XERJ_DISABLE_FEEDBACK"] = "true"
    key_env = str(cfg.get("api_key_env") or "XERJ_API_KEY")
    key = os.environ.get(key_env)
    for model in (cfg.get("models") or {}).values():
        if isinstance(model, dict) and model.get("key_env") and model["key_env"] != key_env:
            env.pop(model["key_env"], None)
    if key:
        env["XERJ_API_KEY"] = key
    return env


def _xerj_run(cfg: dict, args: list[str], *, ok_codes=(0,)) -> dict:
    exe = xerj_executable(cfg)
    if not exe:
        raise CodeIntelError("xerj executable not found")
    cmd = [exe, *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=_xerj_env(cfg), timeout=float(cfg.get("timeout", 120)))
    except (OSError, subprocess.SubprocessError) as e:
        raise CodeIntelError(str(e)) from e
    if r.returncode not in ok_codes:
        raise CodeIntelError((r.stderr or r.stdout or f"xerj exited {r.returncode}").strip()[-500:])
    try:
        return json.loads(r.stdout)
    except ValueError as e:
        raise CodeIntelError("xerj returned non-JSON output") from e


def _scope_args(cfg: dict, prefix: str) -> list:
    return ["--url", str(cfg.get("url") or "http://127.0.0.1:9200"), "--prefix", prefix or str(cfg.get("prefix") or "ax")]


def _passage(hit: dict) -> str:
    fields = hit.get("fields") or {}
    passages = fields.get("_passage") or []
    if passages:
        p = passages[0]
        return str(p.get("text") or "") if isinstance(p, dict) else str(p)
    source = hit.get("_source") or {}
    return str(source.get("body") or source.get("code") or "")[:2000]


def _normalize_search(data: dict) -> list:
    out = []
    for hit in ((data.get("hits") or {}).get("hits") or []):
        src = hit.get("_source") or {}
        out.append({
            "path": src.get("ax_path") or src.get("path") or src.get("title") or "",
            "line": hit.get("match_line") or src.get("start_line") or src.get("line") or 1,
            "score": hit.get("_score"),
            "language": src.get("language"),
            "text": _passage(hit),
        })
    return out


def _normalize_defs(data: dict) -> list:
    out = []
    for hit in ((data.get("hits") or {}).get("hits") or []):
        src = hit.get("_source") or {}
        out.append({
            "path": src.get("ax_path") or src.get("path") or "",
            "line": src.get("line") or src.get("start_line") or 1,
            "name": src.get("name") or "",
            "kind": src.get("kind") or "",
            "language": src.get("language"),
            "code": src.get("code") or _passage(hit),
        })
    return out


class XerjBackend:
    name = "xerj"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def index(self, root: Path, prefix: str) -> dict:
        data = _xerj_run(self.cfg, ["autoindex", str(root), *_scope_args(self.cfg, prefix),
                                    "--json", "--progress", "none"], ok_codes=(0, 3))
        keys = ("prefix", "files_total", "files_indexed", "files_junk", "records_total", "wall_seconds", "code_files")
        return {k: data.get(k) for k in keys if k in data}

    def search(self, query: str, prefix: str, k: int, full: int) -> list:
        data = _xerj_run(self.cfg, ["search", query, *_scope_args(self.cfg, prefix),
                                    "--json", "-k", str(k), "--full", str(full)])
        hits = _normalize_search(data)
        for hit in hits:
            hit["text"] = hit["text"][:full]
        return hits

    def definitions(self, symbol: str, prefix: str, k: int, kind: str = "", lang: str = "") -> list:
        args = ["def", symbol, *_scope_args(self.cfg, prefix), "--json", "-k", str(k)]
        if kind:
            args += ["--kind", kind]
        if lang:
            args += ["--lang", lang]
        return _normalize_defs(_xerj_run(self.cfg, args))

    def _url(self) -> str:
        return str(self.cfg.get("url") or "http://127.0.0.1:9200").rstrip("/")

    def _request(self, path: str, body: dict) -> dict:
        key = _xerj_env(self.cfg).get("XERJ_API_KEY")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "ApiKey " + key
        req = urllib.request.Request(self._url() + path, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=float(self.cfg.get("timeout", 120))) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (OSError, ValueError, urllib.error.URLError) as e:
            raise CodeIntelError(str(e)) from e

    def memory_store(self, namespace: str, text: str, mid: str = "") -> dict:
        ns = urllib.parse.quote(namespace, safe="")
        body = {"text": text, "metadata": {"source": "agentkit"}, "dedup": True}
        if mid:
            body["id"] = mid
        return self._request(f"/_memory/{ns}", body)

    def memory_recall(self, namespace: str, query: str, k: int = 5) -> list:
        ns = urllib.parse.quote(namespace, safe="")
        data = self._request(f"/_memory/{ns}/_recall", {"query": query, "k": k, "hybrid": True})
        return [{"id": hit.get("id", ""), "text": hit.get("text", ""),
                 "metadata": hit.get("metadata") or {}, "score": hit.get("score")}
                for hit in (data.get("hits") or []) if isinstance(hit, dict)]


class BuiltinBackend:
    name = "builtin"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def search(self, query: str, prefix: str, k: int, full: int) -> list:
        from .symbols import lang_for

        stop = {"the", "and", "for", "with", "from", "that", "this", "into", "how", "what", "where"}
        words = [w.casefold() for w in re.findall(r"\w+", query, flags=re.UNICODE)
                 if len(w) > 2 and w.casefold() not in stop]
        if not words:
            words = [query.casefold()]
        phrase = query.casefold()
        ranked = []
        for rel in list_files(self.ctx)[:5000]:
            path_lower = rel.casefold()
            path_hits = sum(1 for w in words if w in path_lower)
            try:
                handle = open(self.ctx.root / rel, "r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with handle:
                for number, line in enumerate(handle, 1):
                    lower = line.casefold()
                    matched = sum(1 for w in words if w in lower)
                    if not matched:
                        continue
                    score = matched * 10.0 + path_hits * 3.0
                    if phrase and phrase in lower:
                        score += 15.0
                    if re.match(r"\s*(async\s+def|def|class|function|fn|struct|impl|interface|func)\b", line):
                        score += 5.0
                    if path_lower.startswith("agentkit/"):
                        score += 4.0
                    if path_lower.startswith(("tests/", "eval/", "docs/")) or "/tests/" in path_lower:
                        score -= 3.0
                    ranked.append((score, rel, number, line[:full]))
        ranked.sort(key=lambda x: (-x[0], x[1], x[2]))
        return [{"path": rel, "line": number, "score": round(score, 3),
                 "language": lang_for(rel), "text": line}
                for score, rel, number, line in ranked[:k]]

    def definitions(self, symbol: str, prefix: str, k: int, kind: str = "", lang: str = "") -> list:
        from .symbols import extract_symbols, lang_for

        found = []
        for rel in list_files(self.ctx):
            if lang and lang_for(rel) != lang:
                continue
            try:
                syms, _eng = extract_symbols(read_text(self.ctx.root / rel), rel)
            except Exception:
                continue
            for s in syms:
                if symbol.casefold() not in (s.name + " " + s.qualname).casefold():
                    continue
                if kind and s.kind != kind:
                    continue
                found.append({"path": rel, "line": s.start, "name": s.name, "kind": s.kind,
                              "language": lang_for(rel), "code": s.signature})
                if len(found) >= k:
                    return found
        return found

    def references(self, symbol: str, prefix: str, k: int, lang: str = "", include_declaration: bool = False) -> list:
        from .symbols import lang_for

        pattern = re.compile(r"(?<![\w])" + re.escape(symbol) + r"(?![\w])")
        definition = re.compile(r"\s*(async\s+def|def|class|function|fn|struct|impl|interface|func)\b")
        found = []
        for rel in list_files(self.ctx):
            if lang and lang_for(rel) != lang:
                continue
            try:
                text = read_text(self.ctx.root / rel)
            except Exception:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if not include_declaration and definition.match(line) and symbol in line:
                    continue
                for match in pattern.finditer(line):
                    found.append({"path": rel, "line": number, "column": match.start() + 1,
                                  "text": line.strip()[:300]})
                    if len(found) >= k:
                        return found
        return found


class LSPBackend:
    name = "lsp"

    def __init__(self, ctx: Ctx, language: str, server: dict):
        self.ctx = ctx
        self.language = language
        self.server = server

    def _command(self) -> list[str]:
        raw = self.server.get("command") or []
        if isinstance(raw, str):
            raw = shlex.split(raw)
        if not isinstance(raw, list) or not raw or any(not isinstance(item, str) or not item for item in raw):
            raise CodeIntelError("LSP command must be a non-empty argv array")
        return [*raw, *[str(item) for item in self.server.get("args") or []]]

    def _client(self):
        from .lsp import LSPClient

        return LSPClient(self._command(), self.ctx.root, timeout=float(self.server.get("timeout", 30)),
                         env=_lsp_env(self.ctx.config))

    def references(self, path: Path, line: int, column: int, k: int, include_declaration: bool = False) -> list:
        from .lsp import normalize_locations

        try:
            with self._client() as client:
                client.open_document(path, path.read_text(encoding="utf-8"), self.language)
                result = client.references(path, line, column, include_declaration)
        except Exception as e:
            raise CodeIntelError(str(e)) from e
        return normalize_locations(result, self.ctx.root, k)


def select(ctx: Ctx, requested: str = "auto") -> object:
    cfg = _cfg(ctx)
    backend = requested if requested in ("builtin", "xerj") else _backend(cfg)
    if backend == "xerj":
        return XerjBackend(_runtime_cfg(ctx))
    if backend == "builtin":
        return BuiltinBackend(ctx)
    return XerjBackend(_runtime_cfg(ctx)) if xerj_available(cfg) else BuiltinBackend(ctx)


def _fallback(ctx: Ctx, requested: str, method: str, *args):
    builtin = BuiltinBackend(ctx)
    if not hasattr(builtin, method):
        raise CodeIntelError(f"builtin backend does not support {method}")
    return getattr(builtin, method)(*args)


def search(ctx: Ctx, query: str, *, prefix: str = "", k: int = 5, full: int = 600, backend: str = "auto") -> Result:
    if not query.strip():
        raise AKError("code search needs a query", "", "agentkit code search 'query'", code="AK-USAGE")
    k = max(1, min(int(k), 50))
    full = max(100, min(int(full), 5000))
    try:
        selected = select(ctx, backend)
        hits = selected.search(query, prefix, k, full)
        used = selected.name
    except CodeIntelError as e:
        if backend == "xerj" or _backend(_cfg(ctx)) == "xerj":
            raise AKError(f"xerj search failed: {e}", "check xerj_path/url/prefix and that the node is running", None, code="AK-INTERNAL")
        hits = _fallback(ctx, backend, "search", query, prefix, k, full)
        used = "builtin"
    return Result({"backend": used, "query": query, "hits": hits})


def definitions(ctx: Ctx, symbol: str, *, prefix: str = "", k: int = 5, kind: str = "", lang: str = "",
                backend: str = "auto") -> Result:
    if not symbol.strip():
        raise AKError("code defs needs a symbol", "", "agentkit code defs SYMBOL", code="AK-USAGE")
    k = max(1, min(int(k), 50))
    try:
        selected = select(ctx, backend)
        hits = selected.definitions(symbol, prefix, k, kind, lang)
        used = selected.name
    except CodeIntelError as e:
        if backend == "xerj" or _backend(_cfg(ctx)) == "xerj":
            raise AKError(f"xerj def failed: {e}", "check xerj_path/url/prefix and that the node is running", None, code="AK-INTERNAL")
        hits = _fallback(ctx, backend, "definitions", symbol, prefix, k, kind, lang)
        used = "builtin"
    return Result({"backend": used, "symbol": symbol, "hits": hits})


def _reference_position(ctx: Ctx, symbol: str, path: str, line: Optional[int], column: Optional[int]) -> tuple[Path, str, int, int]:
    from .symbols import lang_for

    if path:
        target = ctx.resolve(path)
        if not target.is_file():
            raise AKError("reference path is not a file", str(target), None, code="AK-PATH")
    else:
        definitions = BuiltinBackend(ctx).definitions(symbol, "", 50)
        exact = next((item for item in definitions if item.get("name") == symbol), None)
        if exact is None:
            raise AKError("symbol definition not found", "pass --path FILE to identify the symbol precisely",
                          "agentkit code defs SYMBOL", code="AK-USAGE")
        target = ctx.resolve(exact["path"])
        line = line or int(exact["line"])
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise AKError("cannot read reference file", str(e), None, code="AK-PATH") from e
    line_number = int(line or 1)
    if line_number < 1 or line_number > len(lines):
        raise AKError("reference line is out of range", f"line={line_number}", None, code="AK-USAGE")
    source_line = lines[line_number - 1]
    if column is None:
        index = source_line.find(symbol)
        if index < 0:
            raise AKError("symbol not found on reference line", source_line[:200], None, code="AK-USAGE")
        column = index + 1
    column_number = int(column)
    if column_number < 1 or column_number > len(source_line) + 1:
        raise AKError("reference column is out of range", f"column={column_number}", None, code="AK-USAGE")
    return target, lang_for(target), line_number - 1, column_number - 1


def references(ctx: Ctx, symbol: str, *, path: str = "", line: Optional[int] = None, column: Optional[int] = None,
               k: int = 5, include_declaration: bool = False, backend: str = "auto") -> Result:
    if not symbol.strip():
        raise AKError("code refs needs a symbol", "", "agentkit code refs SYMBOL", code="AK-USAGE")
    if backend not in ("auto", "builtin", "lsp"):
        raise AKError("code refs supports auto, builtin or lsp", str(backend), "agentkit code refs --help", code="AK-USAGE")
    k = max(1, min(int(k), 50))
    target, language, line0, column0 = _reference_position(ctx, symbol, path, line, column)
    try:
        server = lsp_server(_cfg(ctx), language)
    except CodeIntelError as e:
        raise AKError(str(e), "fix [codeintel.lsp]", None, code="AK-CONFIG") from e
    used = "builtin"
    hits = []
    if server and backend in ("auto", "lsp"):
        try:
            hits = LSPBackend(ctx, language, server).references(target, line0, column0, k, include_declaration)
            used = "lsp"
        except CodeIntelError as e:
            if backend == "lsp":
                raise AKError(f"lsp references failed: {e}", "check [codeintel.lsp.%s] command/args" % language,
                              None, code="AK-INTERNAL") from e
    elif backend == "lsp":
        raise AKError(f"no LSP server configured for {language}", f"add [codeintel.lsp.{language}] command",
                      "agentkit doctor", code="AK-CONFIG")
    if used == "builtin":
        hits = BuiltinBackend(ctx).references(symbol, "", k, language, include_declaration)
    return Result({"backend": used, "symbol": symbol, "semantic": used == "lsp", "hits": hits})


def index(ctx: Ctx, path: str = ".", *, prefix: str = "", backend: str = "auto") -> Result:
    cfg = _cfg(ctx)
    if not xerj_available(cfg):
        raise AKError("xerj is not installed", "set codeintel.xerj_path or install xerj; builtin search remains available",
                      "agentkit doctor", code="AK-MODEL-NONE")
    root = ctx.resolve(path)
    if not root.is_dir():
        raise AKError("index path is not a directory", str(root), None, code="AK-PATH")
    try:
        summary = XerjBackend(_runtime_cfg(ctx)).index(root, prefix)
    except CodeIntelError as e:
        raise AKError(f"xerj autoindex failed: {e}", "check that the XERJ node is running", None, code="AK-INTERNAL") from e
    return Result({"backend": "xerj", **summary})


def memory_store(ctx: Ctx, namespace: str, text: str, mid: str = "") -> Result:
    try:
        data = XerjBackend(_runtime_cfg(ctx)).memory_store(namespace, text, mid)
    except CodeIntelError as e:
        raise AKError(f"xerj memory store failed: {e}", "check XERJ_URL and authentication", None, code="AK-INTERNAL") from e
    return Result({"backend": "xerj", "namespace": namespace, "result": data})


def memory_recall(ctx: Ctx, namespace: str, query: str, k: int = 5) -> Result:
    try:
        hits = XerjBackend(_runtime_cfg(ctx)).memory_recall(namespace, query, k)
    except CodeIntelError as e:
        raise AKError(f"xerj memory recall failed: {e}", "check XERJ_URL and authentication", None, code="AK-INTERNAL") from e
    return Result({"backend": "xerj", "namespace": namespace, "hits": hits})


def cmd_code(ctx: Ctx, a) -> Result:
    if a.code_cmd == "search":
        return search(ctx, a.query, prefix=a.prefix, k=a.k, full=a.full, backend=a.backend)
    if a.code_cmd == "defs":
        return definitions(ctx, a.symbol, prefix=a.prefix, k=a.k, kind=a.kind, lang=a.lang, backend=a.backend)
    if a.code_cmd == "refs":
        return references(ctx, a.symbol, path=a.path, line=a.line, column=a.column, k=a.k,
                          include_declaration=a.include_declaration, backend=a.backend)
    if a.code_cmd == "index":
        return index(ctx, a.path, prefix=a.prefix, backend=a.backend)
    if a.code_cmd == "memory" and a.memory_cmd == "store":
        return memory_store(ctx, a.namespace, a.text, a.id or "")
    if a.code_cmd == "memory" and a.memory_cmd == "recall":
        return memory_recall(ctx, a.namespace, a.query, a.k)
    raise AKError("code needs a subcommand", "use search|defs|index|memory store|memory recall", "agentkit code --help", code="AK-USAGE")
