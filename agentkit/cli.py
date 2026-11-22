# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-02 CLI. Every command prints ONE compact JSON object:

    {"ok":bool,"data":...,"error":{"what","why","next"}|null,"truncated":bool,"next":str|null}

Global options (anywhere on the line): --budget N (tokens, default 2000), --human, --root DIR.
"""

from __future__ import annotations

import argparse
import sys
import traceback

from . import __version__
from .core import AKError, Result, dumps, envelope, finalize, make_ctx

GLOBAL_VALUED = {"--budget", "--root"}
GLOBAL_FLAGS = {"--human", "--json"}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise AKError(f"usage: {message}", self.format_usage().strip()[:400], f"agentkit {self.prog.split(' ', 1)[-1]} --help".replace("agentkit agentkit", "agentkit"))


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="agentkit", description="Token-frugal coding tools for AI agents. Output: compact JSON.")
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="cmd", parser_class=_Parser)

    r = sub.add_parser("read", help="read file | file:START-END | file#Symbol")
    r.add_argument("spec")
    r.add_argument("--allow-secret-file", action="store_true")

    s = sub.add_parser("search", help="regex/keyword search, results path:line:text")
    s.add_argument("pattern")
    s.add_argument("path", nargs="?")
    s.add_argument("--fixed", "-F", action="store_true", help="literal, not regex")
    s.add_argument("--ignore-case", "-i", action="store_true")
    s.add_argument("--glob", "-g", help="only files matching this glob, e.g. '*.py'")
    s.add_argument("--no-ignore", action="store_true", help="do not respect .gitignore")
    s.add_argument("--offset", type=int, default=0)

    l = sub.add_parser("ls", help="list a directory (respects .gitignore)")
    l.add_argument("path", nargs="?")
    l.add_argument("--recursive", "-r", action="store_true")
    l.add_argument("--no-ignore", action="store_true")
    l.add_argument("--offset", type=int, default=0)

    g = sub.add_parser("glob", help="find files by glob, e.g. '**/*.py'")
    g.add_argument("pattern")
    g.add_argument("path", nargs="?")
    g.add_argument("--no-ignore", action="store_true")
    g.add_argument("--offset", type=int, default=0)

    m = sub.add_parser("repo_map", aliases=["repo-map", "map"], help="directories + top symbols")
    m.add_argument("path", nargs="?")
    m.add_argument("--offset", type=int, default=0)
    m.add_argument("--symbols-per-file", type=int, default=6)

    e = sub.add_parser("edit", help="replace a unique snippet")
    e.add_argument("path")
    e.add_argument("--old")
    e.add_argument("--new")
    e.add_argument("--old-file")
    e.add_argument("--new-file")
    e.add_argument("--stdin", action="store_true", help='read {"old":..,"new":..} JSON from stdin')
    e.add_argument("--all", action="store_true", help="replace every occurrence")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--no-syntax-check", action="store_true")

    pa = sub.add_parser("patch", help='atomic multi-file ops from JSON {"ops":[...]} (stdin or --file)')
    pa.add_argument("--file")
    pa.add_argument("--dry-run", action="store_true")
    pa.add_argument("--no-syntax-check", action="store_true")

    c = sub.add_parser("create", help="create a file (content arg, --content-file, or stdin)")
    c.add_argument("path")
    c.add_argument("content", nargs="?")
    c.add_argument("--content-file")
    c.add_argument("--overwrite", action="store_true")
    c.add_argument("--no-syntax-check", action="store_true")

    d = sub.add_parser("delete", help="delete files (checkpointed)")
    d.add_argument("paths", nargs="+")

    rn = sub.add_parser("rename", help="rename/move a file (checkpointed)")
    rn.add_argument("src")
    rn.add_argument("dst")

    ru = sub.add_parser("run", help="run a shell command with timeout + budgeted output")
    ru.add_argument("command")
    ru.add_argument("--timeout", type=float)
    ru.add_argument("--cwd")

    cp = sub.add_parser("checkpoint", help="snapshots: list | diff ID | rollback ID | create FILE..")
    cps = cp.add_subparsers(dest="cp_cmd", parser_class=_Parser)
    cpl = cps.add_parser("list")
    cpl.add_argument("--offset", type=int, default=0)
    cpd = cps.add_parser("diff")
    cpd.add_argument("id")
    cpd.add_argument("--offset", type=int, default=0)
    cpr = cps.add_parser("rollback")
    cpr.add_argument("id")
    cpc = cps.add_parser("create")
    cpc.add_argument("paths", nargs="*")
    cpc.add_argument("--reason")

    v = sub.add_parser("verify", help="run checks in a clean worktree/copy, ≤300-token report")
    v.add_argument("--check", action="append", default=[])
    v.add_argument("--timeout", type=float)
    v.add_argument("--base", default="HEAD")

    mem = sub.add_parser("memory", help="project memory: add|list|search|show|forget|brief")
    ms = mem.add_subparsers(dest="mem_cmd", parser_class=_Parser)
    ma = ms.add_parser("add")
    ma.add_argument("title")
    ma.add_argument("body", nargs="?")
    ma.add_argument("--category", "-c", default="project-info")
    ma.add_argument("--pin", action="store_true", help="core memory, always in brief")
    ma.add_argument("--related", action="append")
    ma.add_argument("--anchor", action="append", help="file or file#Symbol; memory goes maybe_stale when it changes")
    ma.add_argument("--id")
    mll = ms.add_parser("list")
    mll.add_argument("--category")
    mll.add_argument("--offset", type=int, default=0)
    mse = ms.add_parser("search")
    mse.add_argument("query")
    mse.add_argument("--offset", type=int, default=0)
    msh = ms.add_parser("show")
    msh.add_argument("id")
    msh.add_argument("--offset", type=int, default=0)
    mf = ms.add_parser("forget")
    mf.add_argument("id")
    ms.add_parser("brief")

    sub.add_parser("doctor", help="self check with fix hints")
    t = sub.add_parser("task", help="(M2) task dispatch; in direct mode tells you to use tools directly")
    t.add_argument("task_cmd", nargs="?", default="create")
    t.add_argument("rest", nargs=argparse.REMAINDER)
    return p


def _handlers():
    from . import checkpoint, doctor, edit, memory, read, repomap, run, search, verify

    return {
        "read": read.cmd_read, "search": search.cmd_search, "ls": search.cmd_ls, "glob": search.cmd_glob,
        "repo_map": repomap.cmd_repo_map, "repo-map": repomap.cmd_repo_map, "map": repomap.cmd_repo_map,
        "edit": edit.cmd_edit, "patch": edit.cmd_patch, "create": edit.cmd_create, "delete": edit.cmd_delete,
        "rename": edit.cmd_rename, "run": run.cmd_run, "checkpoint": checkpoint.cmd_checkpoint,
        "verify": verify.cmd_verify, "memory": memory.cmd_memory, "doctor": doctor.cmd_doctor,
        "task": doctor.cmd_task,
    }


def split_globals(argv: list):
    rest, budget, human, root = [], None, False, None
    i = 0
    while i < len(argv):
        t = argv[i]
        name, eq, val = t.partition("=")
        if name in GLOBAL_VALUED:
            if not eq:
                if i + 1 >= len(argv):
                    raise AKError(f"{name} needs a value", "", None)
                val = argv[i + 1]
                i += 1
            if name == "--budget":
                try:
                    budget = int(val)
                except ValueError:
                    raise AKError("--budget must be an integer (tokens)", val, None)
            else:
                root = val
        elif t == "--human":
            human = True
        elif t == "--json":
            pass
        else:
            rest.append(t)
        i += 1
    return rest, budget, human, root


def render_human(env: dict) -> str:
    out = []

    def walk(v, ind=0, key=None):
        pad = "  " * ind
        label = f"{pad}{key}: " if key is not None else pad
        if isinstance(v, dict):
            if key is not None:
                out.append(f"{pad}{key}:")
            for k, x in v.items():
                walk(x, ind + (1 if key is not None else 0), k)
        elif isinstance(v, list):
            out.append(label.rstrip() if key is not None else pad)
            for x in v:
                if isinstance(x, dict) and all(not isinstance(y, (dict, list)) for y in x.values()):
                    out.append(f"{pad}  - " + ", ".join(f"{k}={y}" for k, y in x.items()))
                elif isinstance(x, (dict, list)):
                    walk(x, ind + 1, "-")
                else:
                    out.append(f"{pad}  - {x}")
        elif isinstance(v, str) and "\n" in v:
            out.append(label.rstrip())
            out.extend(pad + "  " + ln for ln in v.splitlines())
        else:
            out.append(f"{label}{v}")

    walk({k: env[k] for k in ("ok", "error", "data", "truncated", "next") if env.get(k) not in (None, False) or k == "ok"})
    return "\n".join(out)


def run_cli(argv: list) -> tuple[dict, int, bool]:
    human = False
    ctx = None
    try:
        rest, budget, human, root = split_globals(argv)
        parser = build_parser()
        if not rest or rest[0] in ("-h", "--help", "help"):
            from .core import Ctx

            cmds = sorted(k for k in _handlers() if k not in ("repo-map", "map"))
            return envelope(True, {"usage": "agentkit <command> [args] [--budget N] [--human]",
                                   "commands": cmds, "help": "agentkit <command> --help", "docs": "llms.txt"}), 0, human
        if rest == ["--version"]:
            return envelope(True, {"version": __version__}), 0, human
        # argparse --help on subcommands prints text; turn into JSON
        if "--help" in rest or "-h" in rest:
            return envelope(True, {"help": _help_for(parser, [t for t in rest if t not in ("-h", "--help")])}), 0, human
        a = parser.parse_args(rest)
        ctx = make_ctx(root, budget, human, rest + (["--budget", str(budget)] if budget is not None else [])
                       + (["--root", root] if root else []))
        if a.cmd == "checkpoint" and not a.cp_cmd:
            a.cp_cmd, a.offset = "list", 0
        if a.cmd == "memory" and not a.mem_cmd:
            a.mem_cmd = "brief"
        res: Result = _handlers()[a.cmd](ctx, a)
        env = envelope(True, res.data, None, res.truncated, res.next)
        return finalize(ctx, env, res.budget_chars), 0, human
    except AKError as e:
        env = envelope(False, e.data, {"what": e.what, "why": e.why, "next": e.next}, False, e.next)
        code = 2 if e.what.startswith("usage:") else 1
        return (finalize(ctx, env) if ctx else _fin_noctx(env)), code, human
    except KeyboardInterrupt:
        return _fin_noctx(envelope(False, None, {"what": "interrupted", "why": "", "next": None})), 130, human
    except Exception as e:  # never crash with a traceback on stdout
        tb = traceback.format_exc().strip().splitlines()[-3:]
        env = envelope(False, None, {"what": f"internal error: {type(e).__name__}: {e}"[:300], "why": " | ".join(tb)[:400],
                                     "next": "agentkit doctor; please report at github.com/tangniutt/agentkit/issues"})
        return (finalize(ctx, env) if ctx else _fin_noctx(env)), 1, human


def _help_for(parser, words: list) -> str:
    p = parser
    for w in words:
        sub = next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)
        if sub is None or w not in sub.choices:
            break
        p = sub.choices[w]
    return p.format_help()


def _fin_noctx(env: dict) -> dict:
    from pathlib import Path

    from .core import Ctx

    c = Ctx(root=Path.cwd(), cwd=Path.cwd())
    return finalize(c, env)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env, code, human = run_cli(argv)
    sys.stdout.write((render_human(env) if human else dumps(env)) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
