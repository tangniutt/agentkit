# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-12 built-in worker loop.

Shape follows mini-swe-agent's DefaultAgent (MIT): query the model, execute its actions,
append observations, stop on an exit condition, count consecutive format errors. The code
is our own: OpenAI tool calling instead of bash blocks, AgentKit's M1 tools as the only
actions, plus stuck detection, context compaction (idea from LooppooL's compactToolResults,
MIT) and hard budgets (M-16).

The worker never decides whether the task is done: `finish` only records its claim; the
verifier re-runs the checks in a clean worktree (tasks.verify_task).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from .core import dumps
from .models import ChatClient, ModelError, ModelSpec, reasoning_text, usage_of
from .redact import redact, redact_text

COMPACT_AT = 0.70
KEEP_TURNS = 4

TOOLS_SPEC = [
    ("read", "Read a file: 'path', 'path:START-END' or 'path#Symbol'. Output is budgeted; follow 'next' to page.",
     {"spec": "string"}, ["spec"]),
    ("search", "Regex search in the repo (respects .gitignore). Results path:line:text.",
     {"pattern": "string", "path": "string", "glob": "string", "fixed": "boolean", "ignore_case": "boolean"}, ["pattern"]),
    ("ls", "List a directory.", {"path": "string", "recursive": "boolean"}, []),
    ("glob", "Find files by glob, e.g. '**/*.py'.", {"pattern": "string"}, ["pattern"]),
    ("repo_map", "Directories + top symbols of the repo (or a sub path).", {"path": "string"}, []),
    ("edit", "Replace an exact, unique snippet in a file (syntax-checked; 0 or >1 matches returns candidates).",
     {"path": "string", "old": "string", "new": "string", "all": "boolean"}, ["path", "old", "new"]),
    ("create", "Create a file with content (set overwrite=true to replace an existing file).",
     {"path": "string", "content": "string", "overwrite": "boolean"}, ["path", "content"]),
    ("delete", "Delete files.", {"paths": "array"}, ["paths"]),
    ("rename", "Rename / move a file.", {"src": "string", "dst": "string"}, ["src", "dst"]),
    ("run", "Run a shell command in the repo (timeout seconds, default 120). git commit/push are refused.",
     {"command": "string", "timeout": "number"}, ["command"]),
    ("memory_search", "Search project memory.", {"query": "string"}, ["query"]),
    ("memory_show", "Show one project memory by id.", {"id": "string"}, ["id"]),
    ("ask", "Ask the supervisor a blocking question instead of guessing.",
     {"question": "string", "options": "array", "recommendation": "string", "blocking": "boolean"}, ["question"]),
    ("code_search", "Search code passages with the configured code-intelligence backend.",
     {"query": "string", "prefix": "string", "k": "integer"}, ["query"]),
    ("code_defs", "Find symbol definitions with the configured code-intelligence backend.",
     {"symbol": "string", "prefix": "string", "k": "integer", "kind": "string", "lang": "string"}, ["symbol"]),
    ("finish", "Call when you believe the goal is met. Your claim is NOT trusted: the checks are re-run independently.",
     {"summary": "string"}, ["summary"]),
]
TOOL_NAMES = {t[0] for t in TOOLS_SPEC}


def tool_schemas(include_code: bool = False) -> list:
    out = []
    for name, desc, props, req in TOOLS_SPEC:
        if name in ("code_search", "code_defs") and not include_code:
            continue
        p = {}
        for k, t in props.items():
            p[k] = {"type": t} if t != "array" else {"type": "array", "items": {"type": "string"}}
        out.append({"type": "function", "function": {"name": name, "description": desc,
                                                       "parameters": {"type": "object", "properties": p, "required": req}}})
    return out


STYLE_MIN_FILES = 40       # auto style: repos with at least this many tracked files get the note from round 1
STYLE_AFTER_CHARS = 4000   # auto style: otherwise append it once the transcript beyond the prefix is this long

STYLE_TERSE = """- Output style: terse; text between tool calls at most one short sentence. Keep code, paths, errors exact.
- Tool results are compressed: old ones become one-line [aged] summaries, repeats say same_as_turn; run again
  if you need the text. `run` output is filtered; `filtered.raw` names the command that shows the raw log.
"""

SYSTEM = """You are an AgentKit worker: a coding agent working alone in an isolated git worktree.
Use ONLY the provided tools (always via tool calls). Every tool returns compact JSON {{ok,data,error,truncated,next}}.
Rules:
- Make the smallest change that meets the goal. Do not modify, skip or delete tests, test config or check scripts
  unless the goal explicitly asks for it. Stay inside the allowed paths.
- You cannot commit or push; leave changes uncommitted.
- Run the checks yourself with `run` before calling `finish`. Your claim is not trusted: the verifier re-runs
  the checks in a clean worktree and flags suspicious changes.
- Be frugal: prefer search / read with line ranges over reading whole files.
- Commands run from the worktree root: use relative paths; never cd to an absolute path.
{style}
GOAL:
{goal}

CHECKS the verifier will run:
{checks}

ALLOWED PATHS: {allow}

PROJECT MEMORY (brief):
{brief}
"""


class StopWorker(BaseException):
    """Raised in the worker thread to stop at once (budget, cancel). BaseException so tool code can't swallow it."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Outcome:
    def __init__(self, status: str, reason: str = "", stopped: Optional[str] = None, claim: str = "",
                 code: Optional[str] = None, question: Optional[dict] = None):
        self.status = status  # submitted | failed | stopped | paused
        self.reason = reason
        self.stopped = stopped
        self.claim = claim
        self.question = question
        if code is None and stopped == "budget":
            code = "AK-BUDGET"
        elif code is None and stopped == "rounds":
            code = "AK-WORKER-ROUNDS"
        elif code is None and stopped == "cancelled":
            code = "AK-TASK-CANCELLED"
        elif code is None and stopped == "stuck":
            code = "AK-WORKER-STUCK"
        self.code = code


# ---------------------------------------------------------------- tool-call repair


def repair_args(raw) -> Optional[dict]:
    """Best-effort repair of a malformed arguments string. Returns a dict or None."""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    s = str(raw).strip()
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except ValueError:
        pass
    s2 = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    s2 = re.sub(r",\s*([}\]])", r"\1", s2)
    m = re.search(r"\{.*\}", s2, re.S)
    if m:
        s2 = m.group(0)
    for attempt in (s2, s2.replace("\n", "\\n")):
        try:
            v = json.loads(attempt)
            if isinstance(v, dict):
                return v
        except ValueError:
            pass
    try:
        py = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", re.sub(r"\bnull\b", "None", s2)))
        v = ast.literal_eval(py)
        if isinstance(v, dict):
            return {str(k): x for k, x in v.items()}
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        pass
    return None


def _norm_name(name: str) -> Optional[str]:
    n = (name or "").strip()
    n = n.split(".")[-1].split(":")[-1].strip().lower().replace("-", "_")
    alias = {"repomap": "repo_map", "read_file": "read", "write_file": "create", "bash": "run", "shell": "run",
             "submit": "finish", "done": "finish"}
    n = alias.get(n, n)
    return n if n in TOOL_NAMES else None


def _text_tool_call(content: str):
    """Some models put a tool call in plain text. Find {"name":..,"arguments":..} JSON."""
    if not content:
        return None
    for m in re.finditer(r"\{.*\}", content, re.S):
        try:
            v = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(v, dict) and ("name" in v or "tool" in v):
            name = v.get("name") or v.get("tool")
            args = v.get("arguments") or v.get("args") or v.get("parameters") or {}
            if isinstance(args, str):
                args = repair_args(args)
            if _norm_name(str(name)) and isinstance(args, dict):
                return _norm_name(str(name)), args
    return None


# ---------------------------------------------------------------- worker


class Worker:
    def __init__(self, spec: ModelSpec, worktree: Path, main_root: Path, task: dict, *,
                 on_progress: Callable[[dict], None] = lambda d: None,
                 transcript: Optional[Path] = None, client: Optional[ChatClient] = None,
                 cfg: Optional[dict] = None, check_stop: Callable[[], Optional[str]] = lambda: None,
                 events=None):
        cfg = cfg or {}
        self.events = events
        self.last_component = "worker"
        self.last_step = 0
        self.spec = spec
        self.wt = worktree
        self.main_root = main_root
        self.task = task
        self.on_progress = on_progress
        self.transcript = transcript
        self.client = client or ChatClient(spec, backoff=float(cfg.get("backoff_s", 1.0)))
        self.check_stop = check_stop
        self.tool_budget = int(cfg.get("tool_budget", 1500))
        self.max_rounds = int(cfg.get("max_rounds", 40))
        self.stuck_rounds = int(cfg.get("stuck_rounds", 10))
        self.same_error_limit = int(cfg.get("same_error_limit", 3))
        self.max_format_errors = int(cfg.get("max_format_errors", 3))
        b = task.get("budget") or {}
        self.budget_tokens = int(b.get("tokens") or 0)
        self.budget_usd = float(b.get("usd") or 0)
        prior = task.get("prior") or {}  # resumed task: budgets cover the whole task, not one segment
        self.tokens_in = int(prior.get("tokens_in") or 0)
        self.tokens_out = int(prior.get("tokens_out") or 0)
        self.usd = float(prior.get("usd") or 0.0)
        self.rounds = int(prior.get("rounds") or 0)
        self.start_round = self.rounds
        self.compactions = 0
        self.repairs = 0
        self.denied: list = []
        self.messages: list = []
        self.tools = tool_schemas(bool((cfg.get("codeintel") or {}).get("worker_tools", False)))
        from .context import TranscriptCompressor

        self.ccfg = dict(cfg.get("compress") or {})
        self.cc = TranscriptCompressor(self.ccfg)
        on = bool(self.ccfg.get("enabled", True))
        self.run_filters = on and bool(self.ccfg.get("run_filters", True))
        # C-08 follow-up: the terse-style note costs ~70 tokens on every request, which made tiny tasks
        # +5.7% worker tokens offline. "auto" (default) puts it in the system prompt only for a big repo
        # (decided once at start, so the prefix stays byte-stable), otherwise appends it once, as a user
        # message at the end of the transcript, when the task turns out long (transcript beyond the prefix
        # >= style_after_chars) or compression first rewrites a result. "terse" = always at start; "normal" = never.
        self.style = str(self.ccfg.get("style", "auto")) if on else "normal"
        if self.style not in ("auto", "terse", "normal"):
            self.style = "auto"
        self.style_min_files = int(self.ccfg.get("style_min_files", STYLE_MIN_FILES))
        self.style_after_chars = int(self.ccfg.get("style_after_chars", STYLE_AFTER_CHARS))
        self.style_at: Optional[str] = None  # "start" | "round N" | None (never sent)

    # ---------------------------------------------------------- helpers
    def stats(self) -> dict:
        return {"rounds": self.rounds, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "usd": round(self.usd, 6), "compactions": self.compactions, "repairs": self.repairs,
                "compression": dict(self.cc.stats(), style=self.style_at or "off")}

    def _ev(self, event: str, component: str, duration=None, error_code=None, **extra):
        self.last_component, self.last_step = component, self.rounds
        if self.events is not None:
            self.events.emit(event, step=self.rounds, component=component, duration=duration, error_code=error_code, **extra)

    def _log(self, obj: dict):
        if self.transcript:
            with open(self.transcript, "a", encoding="utf-8") as f:
                f.write(dumps(redact(obj)) + "\n")

    def _add(self, msg: dict):
        self.messages.append(msg)
        self._log(msg)

    def est_tokens(self) -> int:
        return (len(dumps(self.messages)) + len(dumps(self.tools))) // 4

    def _over_budget(self) -> Optional[str]:
        if self.budget_tokens and self.tokens_in + self.tokens_out >= self.budget_tokens:
            return "budget"
        if self.budget_usd and self.usd >= self.budget_usd:
            return "budget"
        return None

    def diff_fingerprint(self) -> str:
        try:
            st = subprocess.run(["git", "-C", str(self.wt), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                                capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return ""
        h = hashlib.sha256()
        for ent in sorted(e for e in st.split("\0") if e):
            path = ent[3:]
            if path.startswith(".agentkit/") or "__pycache__/" in path or path.endswith(".pyc") or ".pytest_cache/" in path:
                continue
            h.update(ent.encode())
            p = self.wt / path
            if p.is_file():
                try:
                    h.update(p.read_bytes())
                except OSError:
                    pass
        return h.hexdigest()

    # ---------------------------------------------------------- tools
    def _cli(self, argv: list, root: Optional[Path] = None) -> dict:
        from .cli import run_cli

        full = list(argv) + ["--budget", str(self.tool_budget)]
        if root is not None:
            full += ["--root", str(root)]
        env, _code, _h = run_cli(full)
        return env

    def run_tool(self, name: str, args: dict) -> dict:
        def s(k, d=None):
            v = args.get(k, d)
            return d if v is None else v

        tmpfiles = []

        def tf(text) -> str:
            fd, p = tempfile.mkstemp(prefix="ak-arg-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(text))
            tmpfiles.append(p)
            return p

        try:
            if name == "read":
                return self._cli(["read", str(s("spec", s("path", "")))])
            if name == "search":
                argv = ["search", str(s("pattern", ""))]
                if s("path"):
                    argv.append(str(s("path")))
                if s("glob"):
                    argv += ["--glob", str(s("glob"))]
                if s("fixed"):
                    argv.append("--fixed")
                if s("ignore_case"):
                    argv.append("--ignore-case")
                return self._cli(argv)
            if name == "ls":
                return self._cli(["ls"] + ([str(s("path"))] if s("path") else []) + (["--recursive"] if s("recursive") else []))
            if name == "glob":
                return self._cli(["glob", str(s("pattern", "*"))])
            if name == "repo_map":
                return self._cli(["repo_map"] + ([str(s("path"))] if s("path") else []))
            if name == "edit":
                argv = ["edit", str(s("path", "")), "--old-file", tf(s("old", "")), "--new-file", tf(s("new", ""))]
                if s("all"):
                    argv.append("--all")
                return self._cli(argv)
            if name == "create":
                argv = ["create", str(s("path", "")), "--content-file", tf(s("content", ""))]
                if s("overwrite"):
                    argv.append("--overwrite")
                return self._cli(argv)
            if name == "delete":
                paths = s("paths") or ([s("path")] if s("path") else [])
                if isinstance(paths, str):
                    paths = [paths]
                return self._cli(["delete"] + [str(p) for p in paths]) if paths else _err("delete needs paths")
            if name == "rename":
                return self._cli(["rename", str(s("src", "")), str(s("dst", ""))])
            if name == "run":
                argv = ["run", str(s("command", ""))]
                if not self.run_filters:
                    argv.append("--raw")
                if s("timeout"):
                    argv += ["--timeout", str(float(s("timeout")))]
                env = self._cli(argv)
                if not env.get("ok") and (env.get("data") or {}).get("denied"):
                    self.denied.append(str(s("command", ""))[:120])
                return env
            if name == "memory_search":
                return self._cli(["memory", "search", str(s("query", ""))], root=self.main_root)
            if name == "memory_show":
                return self._cli(["memory", "show", str(s("id", ""))], root=self.main_root)
            if name == "code_search":
                argv = ["code", "search", str(s("query", "")), "-k", str(int(s("k", 5)))]
                if s("prefix"):
                    argv += ["--prefix", str(s("prefix"))]
                return self._cli(argv)
            if name == "code_defs":
                argv = ["code", "defs", str(s("symbol", "")), "-k", str(int(s("k", 5)))]
                for key, flag in (("prefix", "--prefix"), ("kind", "--kind"), ("lang", "--lang")):
                    if s(key):
                        argv += [flag, str(s(key))]
                return self._cli(argv)
        finally:
            for p in tmpfiles:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        return _err(f"unknown tool {name}")

    # ---------------------------------------------------------- style note (C-08 follow-up)
    def _tracked_files(self) -> int:
        try:
            out = subprocess.run(["git", "-C", str(self.wt), "ls-files", "-z"], capture_output=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            return 0
        return sum(1 for e in out.split(b"\0") if e and not e.startswith(b".agentkit/"))

    def _style_at_start(self) -> bool:
        if self.style == "terse" or (self.style == "auto" and self._tracked_files() >= self.style_min_files):
            self.style_at = "start"
            return True
        return False

    def _maybe_late_style(self):
        """Append the style note once (append-only: messages[0..1] and earlier turns stay byte-identical)."""
        if self.style != "auto" or self.style_at or len(self.messages) < 3:
            return
        st = self.cc.stats()
        tail = sum(len(str(m.get("content") or "")) + len(dumps(m.get("tool_calls") or [])) for m in self.messages[2:])
        if tail < self.style_after_chars and not st.get("deduped") and not st.get("aged"):
            return
        self.style_at = f"round {self.rounds + 1}"
        self._add({"role": "user", "content": "Note (this task is getting long):\n" + STYLE_TERSE})
        self._ev("style_note", "worker", chars=tail)

    # ---------------------------------------------------------- compaction
    def maybe_compact(self):
        limit = int(self.spec.context * COMPACT_AT)
        if self.est_tokens() <= limit:
            return
        self.compactions += 1
        asst = [i for i, m in enumerate(self.messages) if m.get("role") == "assistant"]
        for keep in range(KEEP_TURNS, 0, -1):
            cut = asst[-keep] if len(asst) >= keep else 0
            for i in range(2, cut):
                m = self.messages[i]
                if m.get("role") == "tool" and not str(m.get("content", "")).startswith("[folded]"):
                    m["content"] = _fold(m.get("content", ""))
                elif m.get("role") == "assistant":
                    if m.get("content") and len(str(m["content"])) > 200:
                        m["content"] = str(m["content"])[:200] + " …[folded]"
                    for tc in m.get("tool_calls") or []:
                        a = tc.get("function", {}).get("arguments", "")
                        if len(a) > 300:
                            tc["function"]["arguments"] = json.dumps({"_folded": a[:120]})
                elif m.get("role") == "user" and len(str(m.get("content", ""))) > 300:
                    m["content"] = str(m["content"])[:300] + " …[folded]"
            if self.est_tokens() <= limit:
                break
        self._log({"role": "_compaction", "content": f"compacted to ~{self.est_tokens()} tokens (limit {limit})"})

    def _model_fail(self, e: ModelError) -> Outcome:
        from .events import model_code

        code = model_code(e.kind)
        if e.kind == "auth":
            return Outcome("failed", f"invalid or expired key: run `agentkit model test {self.spec.name}`", code=code)
        if e.kind == "rate_limited":
            ra = f", retry after {e.retry_after:g}s" if e.retry_after else ""
            if e.capacity:  # busy model (concurrency 429) still busy after a few waits: resume later or switch model
                return Outcome("paused", f"rate limited (429): model at capacity after retries{ra}", code=code)
            return Outcome("paused", f"rate limited (429){ra}", code=code)
        if e.kind in ("timeout", "network", "unavailable"):
            return Outcome("paused", f"model {e.kind} after retries: {str(e)[:120]}", code=code)
        return Outcome("failed", f"model error: {str(e)[:200]}", code=code)

    # ---------------------------------------------------------- loop
    def run(self, brief: str, resume: Optional[str] = None) -> Outcome:
        t = self.task
        sysmsg = SYSTEM.format(goal=t["goal"], checks="\n".join(f"- {c}" for c in t.get("checks") or []) or "- (none given)",
                               allow=", ".join(t.get("allow") or []) or "(any)", brief=brief or "(none)",
                               style=STYLE_TERSE if self._style_at_start() else "")
        if resume:
            self._log({"role": "_resume", "content": f"resumed at round {self.rounds}"})
        self._add({"role": "system", "content": sysmsg})
        if resume:
            self._add({"role": "user", "content": resume})
        else:
            self._add({"role": "user", "content": f"Task {t['id']}: {t['goal']}\nStart with the memory brief above, then explore with repo_map/search/read."})
        format_errors = 0
        last_fp = self.diff_fingerprint()
        changed_once = bool(resume)  # a resumed task usually carries a diff already
        seen_results: set = set()
        no_change = 0
        last_err, same_err, err_round = None, 0, -1
        while True:
            stop = self.check_stop()
            if stop:
                return Outcome("stopped", stop, stopped=stop)
            if self.rounds - self.start_round >= self.max_rounds:
                return Outcome("stopped", f"max rounds ({self.max_rounds})", stopped="rounds")
            self.cc.age(self.messages)  # C-08 progressive aging (batched; never touches the prefix)
            self._maybe_late_style()
            self.maybe_compact()
            if self.budget_tokens and self.tokens_in + self.tokens_out + self.est_tokens() > self.budget_tokens:
                return Outcome("stopped", "token budget would be exceeded by the next call", stopped="budget")
            self.rounds += 1
            tq = time.monotonic()
            try:
                resp = self.client.chat(self.messages, self.tools)
            except ModelError as e:
                from .events import model_code

                self._ev("model_error", "model", time.monotonic() - tq, model_code(e.kind), what=str(e)[:200])
                return self._model_fail(e)
            self._ev("model_call", "model", time.monotonic() - tq)
            tin, tout = usage_of(resp, self.messages)
            self.tokens_in += tin
            self.tokens_out += tout
            self.usd += self.spec.cost(tin, tout)
            msg = resp["choices"][0]["message"]
            amsg = {k: v for k, v in msg.items() if v is not None and k in ("role", "content", "tool_calls", "extra_content")}
            amsg["role"] = "assistant"
            calls = list(amsg.get("tool_calls") or [])
            self.on_progress(self.stats())
            ob = self._over_budget()
            if ob:
                self._add(amsg)
                return Outcome("stopped", "budget exceeded", stopped="budget")

            if not calls:
                found = _text_tool_call(str(msg.get("content") or ""))
                if found:
                    self.repairs += 1
                    name, args = found
                    cid = f"repaired_{self.rounds}"
                    amsg["tool_calls"] = [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]
                    calls = amsg["tool_calls"]
                else:
                    self._add(amsg)
                    format_errors += 1
                    if format_errors >= self.max_format_errors:
                        self._ev("malformed", "worker", error_code="AK-WORKER-MALFORMED")
                        return Outcome("failed", f"malformed tool calls: {format_errors} consecutive", code="AK-WORKER-MALFORMED")
                    finish = resp["choices"][0].get("finish_reason")
                    if finish == "length":
                        # reasoning models (reasoning_content) can spend the whole output budget thinking:
                        # say so, instead of the generic error (the reasoning itself is never sent back)
                        self._ev("output_cut", "model", what=f"finish_reason=length, reasoning {len(reasoning_text(msg))} chars")
                        self._add({"role": "user", "content": "Error: your reply hit the output token limit before any tool "
                                   "call. Think briefly, then respond with one tool call (use `finish` when done)."})
                    else:
                        self._add({"role": "user", "content": "Error: no tool call. Respond with a tool call (use `finish` when done)."})
                    continue

            # validate / repair every call before executing any
            prepared, bad = [], []
            for i, tc in enumerate(calls):
                fn = tc.get("function") or {}
                if not tc.get("id"):
                    tc["id"] = f"call_{self.rounds}_{i}"
                name = _norm_name(fn.get("name", ""))
                raw = fn.get("arguments")
                args = repair_args(raw)
                ok_json = isinstance(raw, dict) or _is_json_obj(raw)
                if name and args is not None:
                    missing = [r for r in _required(name) if r not in args and not (name == "read" and "path" in args)]
                    if missing:
                        bad.append((tc, f"missing required argument(s) {missing} for {name}"))
                        continue
                    if not ok_json or name != fn.get("name"):
                        self.repairs += 1
                        fn["name"] = name
                    fn["arguments"] = json.dumps(args)
                    prepared.append((tc, name, args))
                else:
                    why = f"unknown tool '{fn.get('name')}'" if not name else "arguments are not valid JSON"
                    fn["arguments"] = "{}"
                    fn["name"] = fn.get("name") or "unknown"
                    bad.append((tc, why))
            self._add(amsg)
            for tc, why in bad:
                self._add({"role": "tool", "tool_call_id": tc["id"], "content": dumps(
                    {"ok": False, "error": {"what": "malformed tool call", "why": why,
                                            "next": "re-issue the call with valid JSON arguments; tools: " + ", ".join(sorted(TOOL_NAMES))}})})
            if bad and not prepared:
                format_errors += 1
                if format_errors >= self.max_format_errors:
                    self._ev("malformed", "worker", error_code="AK-WORKER-MALFORMED")
                    return Outcome("failed", f"malformed tool calls: {format_errors} consecutive ({bad[0][1]})", code="AK-WORKER-MALFORMED")
                continue
            format_errors = 0

            finished = None
            new_info = False
            for tc, name, args in prepared:
                stop = self.check_stop()
                if stop:
                    # still answer the call so the transcript stays well-formed
                    self._add({"role": "tool", "tool_call_id": tc["id"], "content": '{"ok":false,"error":{"what":"stopped"}}'})
                    return Outcome("stopped", stop, stopped=stop)
                if name == "finish":
                    finished = str(args.get("summary", ""))[:500]
                    self._add({"role": "tool", "tool_call_id": tc["id"], "content": '{"ok":true,"data":"submitted for independent verification"}'})
                    continue
                if name == "ask":
                    question = str(args.get("question") or "").strip()
                    if not question:
                        self._add({"role": "tool", "tool_call_id": tc["id"], "content": dumps(_err("ask needs a question"))})
                        continue
                    q = {"question": question[:500], "options": [str(x)[:200] for x in (args.get("options") or [])[:4]],
                         "recommendation": str(args.get("recommendation") or "")[:300],
                         "blocking": bool(args.get("blocking", True))}
                    self._add({"role": "tool", "tool_call_id": tc["id"], "content": '{"ok":true,"data":"waiting for supervisor"}'})
                    return Outcome("paused", "waiting for supervisor", code="AK-TASK-WAITING", question=q)
                tt = time.monotonic()
                env = self.run_tool(name, args)
                self._ev("tool", f"tool:{name}", time.monotonic() - tt,
                         None if env.get("ok") else (env.get("error") or {}).get("code"))
                content = redact_text(dumps(env))
                from .context import result_key

                rk = result_key(name, args, env)
                if (rk is None and env.get("ok")) or (rk is not None and rk not in seen_results):
                    new_info = True
                if rk is not None:
                    seen_results.add(rk)
                content = self.cc.tool_result(self.messages, self.rounds, tc["id"], name, args, env, content)
                self._add({"role": "tool", "tool_call_id": tc["id"], "content": content})
                sig = _error_sig(name, env)
                if sig:
                    # count repeats across rounds only: parallel calls in one reply never saw each other's result
                    # (Agnes: 3 failing edits in one reply were failed as "same error 3x" with the fix already done)
                    if sig == last_err:
                        if err_round != self.rounds:
                            same_err += 1
                    else:
                        last_err, same_err = sig, 1
                    err_round = self.rounds
                    if same_err >= self.same_error_limit:
                        return Outcome("failed", f"stuck: same error {same_err}x: {sig[:120]}", stopped="stuck")
            if finished is not None:
                return Outcome("submitted", "worker called finish", claim=finished)
            fp = self.diff_fingerprint()
            if fp != last_fp:
                last_fp, no_change, changed_once = fp, 0, True
                last_err, same_err = None, 0
            elif not changed_once and new_info:
                # still exploring before the first edit and this round learned something new (a file range or
                # search not seen before): not stuck. Found on NVIDIA NIM dogfood: a 10-round exploration of a
                # 200-line file was failed as "stuck" (not resumable); max_rounds still bounds exploration.
                pass
            else:
                no_change += 1
                if no_change >= self.stuck_rounds:
                    return Outcome("failed", f"stuck: no new diff in {no_change} rounds", stopped="stuck")


# ---------------------------------------------------------------- resume (paused / interrupted / stopped tasks)

RESUME_CHARS = 6000


def resume_context(transcript: Optional[Path], wt: Path, base: str, task: dict, max_chars: int = RESUME_CHARS) -> str:
    """A compacted transcript for `task resume`: what was done so far (one line per tool call with
    its outcome), the worker's last note, why it stopped, and the current diff of the worktree.
    Raw assistant turns are NOT replayed: that keeps the context small, avoids dangling tool calls
    of an interrupted turn, and sidesteps provider-specific state (e.g. Gemini thought signatures
    are only valid inside the conversation that produced them)."""
    calls, results, last_text, segments = {}, {}, "", 0
    order: list = []
    if transcript and transcript.exists():
        for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            role = m.get("role")
            if role == "_resume":
                segments += 1
            elif role == "assistant":
                if m.get("content"):
                    last_text = str(m["content"])
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    cid = tc.get("id") or f"_{len(order)}"
                    calls[cid] = (fn.get("name", "?"), str(fn.get("arguments", ""))[:100])
                    order.append(cid)
            elif role == "tool":
                results[m.get("tool_call_id")] = _outcome(str(m.get("content", "")))
    lines = []
    for cid in order:
        name, args = calls[cid]
        lines.append(f"- {name} {args} -> {results.get(cid, 'no result (interrupted)')}")
    if len(lines) > 25:
        lines = [f"- ... {len(lines) - 25} earlier actions omitted"] + lines[-25:]
    try:
        stat = subprocess.run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"],
                              capture_output=True, text=True, timeout=60).stdout
        stat = "\n".join(l for l in stat.splitlines() if ".agentkit/" not in l and "__pycache__" not in l and not l.endswith(".pyc"))
        diff = subprocess.run(["git", "-C", str(wt), "diff", base, "--", ".", ":(exclude).agentkit"],
                              capture_output=True, text=True, timeout=60).stdout
        for l in stat.splitlines():  # new files are untracked: show them too (index left untouched)
            if l.startswith("?? "):
                p = wt / l[3:]
                try:
                    diff += f"\n+++ new file {l[3:]}\n" + p.read_text(errors="replace")[:1500]
                except OSError:
                    pass
    except (OSError, subprocess.SubprocessError):
        stat, diff = "", ""
    head = (f"Resuming task {task['id']} (resume #{segments + 1}) after: "
            f"{task.get('resumed_from') or task.get('reason') or task.get('status')}.\n"
            f"GOAL: {task['goal']}\nThe worktree still holds your earlier changes. Do not redo work that is already in the diff;"
            f" continue from here, run the checks, then call finish.\n")
    body = "ACTIONS SO FAR (compacted):\n" + ("\n".join(lines) or "- none") + "\n"
    if last_text:
        body += f"YOUR LAST NOTE: {last_text[:300]}\n"
    body += "CHANGED FILES:\n" + (stat or "(none)") + "\n"
    room = max(400, max_chars - len(head) - len(body) - 40)
    d = diff if len(diff) <= room else diff[:room] + "\n...[diff truncated; read the files for the rest]"
    return redact_text(head + body + "CURRENT DIFF vs base:\n" + (d or "(empty)"))


def _outcome(content: str) -> str:
    try:
        env = json.loads(content)
    except ValueError:
        return content[:80]
    if not isinstance(env, dict):
        return content[:80]
    if not env.get("ok"):
        return "error: " + str((env.get("error") or {}).get("what", ""))[:80]
    d = env.get("data")
    if isinstance(d, dict) and "exit_code" in d:
        return f"exit {d.get('exit_code')}"
    return "ok"


def _is_json_obj(raw) -> bool:
    try:
        return isinstance(json.loads(raw), dict)
    except (ValueError, TypeError):
        return False


def _required(name: str) -> list:
    for n, _d, _p, req in TOOLS_SPEC:
        if n == name:
            return req
    return []


def _err(what: str) -> dict:
    return {"ok": False, "data": None, "error": {"what": what, "why": "", "next": None}, "truncated": False, "next": None}


def _fold(content: str) -> str:
    c = str(content)
    try:
        env = json.loads(c)
        ok = env.get("ok")
        what = (env.get("error") or {}).get("what") if not ok else None
        head = dumps(env.get("data"))[:140]
        return f"[folded] ok={ok}" + (f" error={what}" if what else "") + f" {len(c)} chars; head: {head}"
    except (ValueError, AttributeError):
        return f"[folded] {len(c)} chars; head: {c[:140]}"


def _error_sig(name: str, env: dict) -> Optional[str]:
    if not env.get("ok"):
        return f"{name}: {(env.get('error') or {}).get('what', '')}"
    if name == "run":
        d = env.get("data") or {}
        if d.get("exit_code") not in (0, None):
            tail = (d.get("stderr_tail") or d.get("stdout_tail") or "").strip().splitlines()[-1:] or [""]
            return f"run exit {d.get('exit_code')}: {tail[0][-160:]}"
    return None
