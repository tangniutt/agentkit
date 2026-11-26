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
    r.add_argument("--full-lines", action="store_true", help="keep long lines; follow next with a larger budget")

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
    e.add_argument("--old", help="exact text to replace (must match once unless --all)")
    e.add_argument("--new", help="replacement text")
    e.add_argument("--old-file", help="read --old from this file")
    e.add_argument("--new-file", help="read --new from this file")
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
    ru.add_argument("--timeout", type=float, help="seconds (default [run] timeout in config)")
    ru.add_argument("--cwd", help="working directory inside the repo")
    ru.add_argument("--raw", action="store_true", help="no command-aware output filter (C-08)")

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
    v.add_argument("--check", action="append", default=[], help="check command, e.g. 'pytest -q'")
    v.add_argument("--timeout", type=float, help="seconds per check")
    v.add_argument("--base", default="HEAD", help="git ref the clean worktree starts from")

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

    dr = sub.add_parser("doctor", help="self check with fix hints")
    dr.add_argument("--online", action="store_true", help="also call each configured model once")
    mo = sub.add_parser("model", help="worker models: add|list|test|remove (keys only by env var name)")
    mos = mo.add_subparsers(dest="model_cmd", parser_class=_Parser)
    moa = mos.add_parser("add")
    moa.add_argument("name")
    moa.add_argument("--preset", help="provider defaults: nvidia | gemini | openrouter | agnes | amd-cn (explicit flags win)")
    moa.add_argument("--base-url", help="OpenAI-compatible base URL (required unless --preset)")
    moa.add_argument("--model", help="provider model id (default: NAME)")
    moa.add_argument("--key-env", help="NAME of the env var holding the key (never the key itself)")
    moa.add_argument("--context", type=int, default=None, help="context window, tokens (default 32000 or preset)")
    moa.add_argument("--rpm", type=float, default=None, help="max requests per minute this worker sends (free-tier pacing)")
    moa.add_argument("--rate-wait", type=float, default=None,
                     help="seconds to sit out a 429 that has no Retry-After, once per call (0 = pause the task at once)")
    moa.add_argument("--max-wait", type=float, default=None, help="longest Retry-After (s) to sit out before pausing (default 30)")
    moa.add_argument("--max-tokens", type=int, default=None, help="max_tokens per call (reasoning models need room)")
    moa.add_argument("--price-in", type=float, default=0.0, help="USD per 1M input tokens")
    moa.add_argument("--price-out", type=float, default=0.0, help="USD per 1M output tokens")
    moa.add_argument("--tier", default="low", help="low|mid|high")
    moa.add_argument("--default", action="store_true")
    mos.add_parser("list")
    mot = mos.add_parser("test")
    mot.add_argument("name", nargs="?")
    mot.add_argument("--timeout", type=float, default=60)
    mor = mos.add_parser("remove")
    mor.add_argument("name")

    t = sub.add_parser("task", help="dispatch to a worker: create|status|wait|report|resume|handoff|cancel|clean")
    ts = t.add_subparsers(dest="task_cmd", parser_class=_Parser)
    tc = ts.add_parser("create")
    tc.add_argument("goal", nargs="?", help="what the worker must achieve")
    tc.add_argument("--goal", dest="goal_opt", help="same as the positional goal")
    tc.add_argument("--check", action="append", default=[], help="acceptance command run by the verifier, e.g. 'pytest -q'")
    tc.add_argument("--allow-baseline-pass", action="store_true", help="allow checks that already pass on the base commit (default: baseline must fail)")
    tc.add_argument("--allow", action="append", default=[], help="glob/dir the worker may change")
    tc.add_argument("--budget-tokens", type=int, help="stop after this many worker tokens")
    tc.add_argument("--budget-usd", type=float, help="stop after this many USD (needs model prices)")
    tc.add_argument("--budget-minutes", type=float, help="stop after this many minutes")
    tc.add_argument("--tier", help="low|mid|high: pick a model by tier")
    tc.add_argument("--model", help="builtin name, Codex id, OpenCode provider/model, Qoder model, or Cursor model")
    tc.add_argument("--backend", choices=("builtin", "codex", "opencode", "qoder", "cursor"), default="builtin", help="worker backend")
    for name in ("status", "report", "cancel"):
        x = ts.add_parser(name)
        x.add_argument("ids", nargs="*")
    tw = ts.add_parser("wait")
    tw.add_argument("ids", nargs="*")
    tw.add_argument("--timeout", type=float, default=300, help="seconds to wait")
    tw.add_argument("--no-report", action="store_true", help="one finished task: status only, no inline report")
    trs = ts.add_parser("resume", help="continue a paused/interrupted/stopped task in its worktree (compacted transcript)")
    trs.add_argument("ids", nargs="*")
    trs.add_argument("--budget-tokens", type=int, help="new total token budget for the task")
    trs.add_argument("--budget-usd", type=float)
    trs.add_argument("--budget-minutes", type=float, help="time budget for this resumed run")
    trs.add_argument("--model", help="target model")
    trs.add_argument("--backend", choices=("builtin", "codex", "opencode", "qoder", "cursor"), help="target backend")
    th = ts.add_parser("handoff", help="resume a paused/interrupted/stopped task with another model/backend")
    th.add_argument("ids", nargs="*")
    th.add_argument("--model", help="target model")
    th.add_argument("--backend", choices=("builtin", "codex", "opencode", "qoder", "cursor"), help="target backend")
    th.add_argument("--budget-tokens", type=int, help="new total token budget for the task")
    th.add_argument("--budget-usd", type=float)
    th.add_argument("--budget-minutes", type=float, help="time budget for this resumed run")
    td = ts.add_parser("diff")
    td.add_argument("id")
    td.add_argument("--offset", type=int, default=0)
    tst = ts.add_parser("steer")
    tst.add_argument("id")
    tst.add_argument("text")
    ta = ts.add_parser("answer")
    ta.add_argument("id")
    ta.add_argument("text")
    tac = ts.add_parser("accept")
    tac.add_argument("id")
    trej = ts.add_parser("reject")
    trej.add_argument("id")
    trej.add_argument("reason", nargs="?")
    tcl = ts.add_parser("clean")
    tcl.add_argument("ids", nargs="*")
    tcl.add_argument("--all", action="store_true", help="every finished task")
    tcl.add_argument("--keep-branch", action="store_true", help="keep agentkit/<id> branches")

    g = sub.add_parser("goal", help="bounded result loop: create|run|status|wait|report|steer|answer|cancel|resume")
    gs = g.add_subparsers(dest="goal_cmd", parser_class=_Parser)
    gc = gs.add_parser("create")
    gc.add_argument("outcome", nargs="?", help="verifiable outcome")
    gc.add_argument("--input", help="GoalSpec JSON file")
    gc.add_argument("--check", action="append", default=[], help="acceptance command (repeatable)")
    gc.add_argument("--non-goal", action="append", default=[])
    gc.add_argument("--allow", action="append", default=[])
    gc.add_argument("--allow-baseline-pass", action="store_true")
    gc.add_argument("--budget-tokens", type=int)
    gc.add_argument("--budget-usd", type=float)
    gc.add_argument("--budget-minutes", type=float)
    gc.add_argument("--max-rounds", type=int)
    gc.add_argument("--max-iterations", type=int, default=3)
    gc.add_argument("--max-handoffs", type=int, default=2)
    gc.add_argument("--failover", action="append", default=[])
    gc.add_argument("--model")
    gc.add_argument("--tier")
    gx = gs.add_parser("status")
    gx.add_argument("id", nargs="?")
    for name in ("report", "cancel"):
        gx = gs.add_parser(name)
        gx.add_argument("id")
    gr = gs.add_parser("run")
    gr.add_argument("id")
    gr.add_argument("--timeout", type=float, default=600)
    gr.add_argument("--budget-tokens", type=int)
    gr.add_argument("--budget-usd", type=float)
    gr.add_argument("--budget-minutes", type=float)
    gw = gs.add_parser("wait")
    gw.add_argument("id")
    gw.add_argument("--timeout", type=float, default=600)
    gst = gs.add_parser("steer")
    gst.add_argument("id")
    gst.add_argument("text")
    gres = gs.add_parser("resume")
    gres.add_argument("id")
    gres.add_argument("--model")
    gres.add_argument("--budget-tokens", type=int)
    gres.add_argument("--budget-usd", type=float)
    gres.add_argument("--budget-minutes", type=float)
    gans = gs.add_parser("answer")
    gans.add_argument("id")
    gans.add_argument("text")

    c = sub.add_parser("code", help="pluggable code intelligence: search|defs|index|memory")
    cs = c.add_subparsers(dest="code_cmd", parser_class=_Parser)
    cse = cs.add_parser("search")
    cse.add_argument("query")
    cse.add_argument("--prefix", default="")
    cse.add_argument("-k", type=int, default=5)
    cse.add_argument("--full", type=int, default=600)
    cse.add_argument("--backend", choices=["auto", "builtin", "xerj"], default="auto")
    cdf = cs.add_parser("defs")
    cdf.add_argument("symbol")
    cdf.add_argument("--prefix", default="")
    cdf.add_argument("-k", type=int, default=5)
    cdf.add_argument("--kind", default="")
    cdf.add_argument("--lang", default="")
    cdf.add_argument("--backend", choices=["auto", "builtin", "xerj"], default="auto")
    crf = cs.add_parser("refs")
    crf.add_argument("symbol")
    crf.add_argument("--path")
    crf.add_argument("--line", type=int)
    crf.add_argument("--column", type=int)
    crf.add_argument("-k", type=int, default=5)
    crf.add_argument("--include-declaration", action="store_true")
    crf.add_argument("--backend", choices=["auto", "builtin", "lsp"], default="auto")
    cin = cs.add_parser("index")
    cin.add_argument("path", nargs="?", default=".")
    cin.add_argument("--prefix", default="")
    cin.add_argument("--backend", choices=["auto", "builtin", "xerj"], default="auto")
    cm = cs.add_parser("memory")
    cms = cm.add_subparsers(dest="memory_cmd", parser_class=_Parser)
    cmst = cms.add_parser("store")
    cmst.add_argument("namespace")
    cmst.add_argument("text")
    cmst.add_argument("--id", default="")
    cmrc = cms.add_parser("recall")
    cmrc.add_argument("namespace")
    cmrc.add_argument("query")
    cmrc.add_argument("-k", type=int, default=5)

    lg = sub.add_parser("logs", help="structured event log of a run / task: logs ID [--errors] [--tail N]")
    lg.add_argument("id")
    lg.add_argument("--errors", action="store_true", help="only events with an error code")
    lg.add_argument("--tail", type=int, help="last N events")
    lg.add_argument("--raw", action="store_true", help="raw (redacted) stdout/stderr of a run log id (run-...)")
    db = sub.add_parser("debug-bundle", help="redacted .tar.gz (config sans keys, versions, events, report) for bug reports")
    db.add_argument("id")
    db.add_argument("--with-transcript", action="store_true", help="include the worker transcript (may contain code)")
    fb = sub.add_parser("feedback", help="draft a GitHub issue (never posts): --bug RUN_ID | --improve TEXT --from ID | --idea TEXT")
    fbg = fb.add_mutually_exclusive_group(required=True)
    fbg.add_argument("--bug", help="run or task id of the failure")
    fbg.add_argument("--improve", nargs="?", const="", help="design proposal after real use (C-12)")
    fbg.add_argument("--idea", help="feature request text (humans)")
    fb.add_argument("--from", dest="from_", action="append", metavar="ID", help="evidence run/task id (repeat)")
    fb.add_argument("--observed", help="what you saw")
    fb.add_argument("--proposal", help="default: the --improve text")
    fb.add_argument("--gain", help="expected gain")
    fb.add_argument("--cost", help="cost / risk")
    fb.add_argument("--prior-art", help="references")
    fb.add_argument("--license", help="of borrowed code")
    fb.add_argument("--verify-by", help="test / eval idea")
    fb.add_argument("--title")
    fb.add_argument("--input", metavar="FILE|-", help="JSON with the same keys (verify, from[])")

    rel = sub.add_parser("release", help="local release: plan | build | verify | rollback (never publishes)")
    rel.add_argument("action", nargs="?", choices=["plan", "build", "verify", "rollback"], default="plan")
    rel.add_argument("artifact", nargs="?")
    rel.add_argument("--out", default="dist")
    rel.add_argument("--allow-dirty", action="store_true")
    rel.add_argument("--overwrite", action="store_true")
    rel.add_argument("--yes", action="store_true")

    un = sub.add_parser("uninstall", help="remove agentkit: package, ~/.config + ~/.local/share/agentkit, .agentkit/ + worktrees "
                                        "in every repo it used (dry run unless --yes; never touches your code or git history)")
    un.add_argument("--yes", action="store_true", help="really delete (default: print the plan only)")
    un.add_argument("--this-repo", action="store_true", help="only this repo's .agentkit/ and worktrees; keep package + user config")
    un.add_argument("--keep-package", action="store_true", help="do not pip uninstall the package")
    un.add_argument("--delete-branches", action="store_true", help="also delete agentkit/<task> branches")

    mn = sub.add_parser("manual", help="operating manual for the supervising AI: overview | <command> detail | --all (C-09)")
    mn.add_argument("words", nargs="*", help="a command or group, e.g. task create")
    mn.add_argument("--all", action="store_true", help="every command in full (≈4k tokens; may need --budget)")
    mn.add_argument("--format", choices=["json", "md"], default="json", help="md prints Markdown text (docs/agent-manual.md)")

    sub.add_parser("mcp", help="run the MCP stdio server (JSON-RPC on stdin/stdout)")

    tr = sub.add_parser("_task-run", help=argparse.SUPPRESS)
    tr.add_argument("id")
    return p


def _handlers():
    from . import checkpoint, doctor, edit, memory, read, repomap, run, search, verify

    return {
        "read": read.cmd_read, "search": search.cmd_search, "ls": search.cmd_ls, "glob": search.cmd_glob,
        "repo_map": repomap.cmd_repo_map, "repo-map": repomap.cmd_repo_map, "map": repomap.cmd_repo_map,
        "edit": edit.cmd_edit, "patch": edit.cmd_patch, "create": edit.cmd_create, "delete": edit.cmd_delete,
        "rename": edit.cmd_rename, "run": run.cmd_run, "checkpoint": checkpoint.cmd_checkpoint,
        "verify": verify.cmd_verify, "memory": memory.cmd_memory, "doctor": doctor.cmd_doctor,
        "task": _task, "goal": _goal, "code": _code, "model": _model, "_task-run": _task_run,
        "logs": _diag("cmd_logs"), "uninstall": _uninstall, "debug-bundle": _diag("cmd_debug_bundle"), "feedback": _diag("cmd_feedback"),
        "manual": _manual, "mcp": _mcp, "release": _release,
    }


def _manual(ctx, a):
    from . import manual

    return manual.cmd_manual(ctx, a)


def _mcp(ctx, a):
    return Result({"server": "mcp", "hint": "use agentkit mcp as a subprocess"})


def _diag(name):
    def h(ctx, a):
        from . import diag

        return getattr(diag, name)(ctx, a)

    return h


def _release(ctx, a):
    from . import release

    a.release_cmd = a.action or "plan"
    if a.artifact:
        a.manifest = a.artifact
    elif not hasattr(a, "manifest"):
        a.manifest = ""
    return release.cmd_release(ctx, a)


def _uninstall(ctx, a):
    from . import lifecycle

    return lifecycle.cmd_uninstall(ctx, a)


def _task(ctx, a):
    from . import tasks

    if not a.task_cmd:
        a.task_cmd, a.ids = "status", []
    if a.task_cmd == "create":
        a.goal = a.goal or getattr(a, "goal_opt", None)
    return tasks.cmd_task(ctx, a)


def _goal(ctx, a):
    from . import goals

    return goals.cmd_goal(ctx, a)


def _code(ctx, a):
    from . import codeintel

    return codeintel.cmd_code(ctx, a)


def _model(ctx, a):
    from . import models

    return models.cmd_model(ctx, a)


def _task_run(ctx, a):
    from . import tasks

    tasks.run_task(ctx.root, a.id)
    return Result({"ran": a.id})


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
    import time as _time

    from .events import EventLog, classify, new_run_id

    human = False
    ctx = None
    run_id = new_run_id()
    t0 = _time.monotonic()
    cmd = None
    root_for_log = None
    try:
        rest, budget, human, root = split_globals(argv)
        parser = build_parser()
        if not rest or rest[0] in ("-h", "--help", "help"):
            cmds = sorted(k for k in _handlers() if k not in ("repo-map", "map", "_task-run"))
            return envelope(True, {"usage": "agentkit <command> [args] [--budget N] [--human]",
                                   "commands": cmds, "help": "agentkit <command> --help",
                                   "manual": "agentkit manual [command]", "docs": "llms.txt"}), 0, human
        if rest == ["--version"]:
            return envelope(True, {"version": __version__}), 0, human
        # argparse --help on subcommands prints text; turn into JSON
        if "--help" in rest or "-h" in rest:
            return envelope(True, {"help": _help_for(parser, [t for t in rest if t not in ("-h", "--help")])}), 0, human
        cmd = rest[0]
        a = parser.parse_args(rest)
        ctx = make_ctx(root, budget, human, rest + (["--budget", str(budget)] if budget is not None else [])
                       + (["--root", root] if root else []))
        ctx.run_id = run_id
        root_for_log = ctx.root
        if a.cmd == "checkpoint" and not a.cp_cmd:
            a.cp_cmd, a.offset = "list", 0
        if a.cmd == "memory" and not a.mem_cmd:
            a.mem_cmd = "brief"
        if a.cmd == "manual" and (a.format == "md" or human):
            # C-09: Markdown / --human text, generated from the same source as the JSON manual
            from . import manual

            return {"_text": manual.markdown(words=a.words or None) if (a.words or a.all or a.format == "md")
                    else manual.markdown_overview()}, 0, human
        res: Result = _handlers()[a.cmd](ctx, a)
        env = envelope(True, res.data, None, res.truncated, res.next)
        if a.cmd not in ("logs", "_task-run", "feedback", "debug-bundle", "uninstall") and (ctx.root / ".agentkit").is_dir():
            EventLog(ctx.root, run_id).emit("command", step=a.cmd, duration=_time.monotonic() - t0)
            if a.cmd != "_task-run":
                from .lifecycle import prune_logs

                prune_logs(ctx.root, ctx.config)  # C-03 / baseline 5.3: logs kept [logs] retain_days (7)
        return finalize(ctx, env, res.budget_chars), 0, human
    except AKError as e:
        code = e.code or classify(e.what, cmd)
        err = {"what": e.what, "why": e.why, "next": e.next, "code": code, "run": run_id}
        env = envelope(False, e.data, err, False, e.next)
        if root_for_log is not None and (root_for_log / ".agentkit").is_dir():
            EventLog(root_for_log, run_id).emit("error", step=cmd, duration=_time.monotonic() - t0, error_code=code,
                                                what=e.what[:200])
        rc = 2 if e.what.startswith("usage:") else 1
        return (finalize(ctx, env) if ctx else _fin_noctx(env)), rc, human
    except KeyboardInterrupt:
        return _fin_noctx(envelope(False, None, {"what": "interrupted", "why": "", "next": None, "code": "AK-USAGE",
                                                 "run": run_id})), 130, human
    except Exception as e:  # never crash with a traceback on stdout (C-03: traceback goes to a log file)
        log_ptr = _log_crash(root_for_log, run_id, cmd, _time.monotonic() - t0)
        env = envelope(False, None, {"what": f"internal error: {type(e).__name__}: {e}"[:200],
                                     "why": (f"bug in agentkit; traceback in {log_ptr}; " if log_ptr else "bug in agentkit; ")
                                            + f"details: agentkit logs {run_id}",
                                     "next": f"agentkit feedback --bug {run_id}  (drafts an issue; ask your human before filing)",
                                     "code": "AK-INTERNAL", "run": run_id})
        return (finalize(ctx, env) if ctx else _fin_noctx(env)), 1, human


def _log_crash(root, run_id: str, cmd, duration: float):
    """Write the traceback to .agentkit/logs/crash-<run>.log (redacted) + an AK-INTERNAL event."""
    from pathlib import Path

    from .events import EventLog
    from .redact import redact_text

    try:
        r = Path(root) if root is not None else Path.cwd()
        d = r / ".agentkit" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"crash-{run_id}.log"
        p.write_text(redact_text(traceback.format_exc()))
        EventLog(r, run_id).emit("crash", step=cmd, duration=duration, error_code="AK-INTERNAL", log=p.name)
        return f".agentkit/logs/{p.name}"
    except Exception:
        return None


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
    rest, _budget, _human, root = split_globals(argv)
    if rest == ["mcp"]:
        from .mcp_server import main as mcp_main

        return mcp_main(["--root", root] if root else [])
    env, code, human = run_cli(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "_text" in env:
        sys.stdout.write(env["_text"])
    else:
        sys.stdout.write((render_human(env) if human else dumps(env)) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
