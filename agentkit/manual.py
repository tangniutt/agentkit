# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""C-09 operating manual for the supervising AI (the "AI 管家" that drives AgentKit).

Generated from the real CLI: the command tree, every argument's type / default / help and the
purpose of top-level commands come from `cli.build_parser()`; error causes and fixes come from the
`events.ERRORS` registry (= docs/errors.md). Only what argparse cannot know lives in SPEC below
(output keys, error codes, side effects, one example). tests/test_manual.py fails when a command has
no SPEC entry, an example does not parse, or a code is not in the registry.

    agentkit manual                 overview + one-line index (fits the default 2000-token budget)
    agentkit manual task create     full detail for one command (or a group: agentkit manual task)
    agentkit manual --all           everything as JSON
    agentkit manual --format md     the same, as Markdown (docs/agent-manual.md is this output)
"""

from __future__ import annotations

import argparse
import shlex
from typing import Optional

from .core import AKError, CHARS_PER_TOKEN, Result, dumps

HIDDEN = {"_task-run"}

# side-effect tags (legend in the manual)
FX = {
    "ro": "read-only (may write .agentkit/ logs)",
    "repo": "changes repo files; checkpointed first (undo: checkpoint rollback)",
    "state": "writes .agentkit/ state or config only",
    "exec": "executes shell commands (permission rules apply)",
    "tokens": "calls a model: spends tokens / USD",
    "destructive": "deletes data that has no checkpoint",
    "net": "network access",
}

# what argparse cannot know: output keys, error codes, side effects, example
SPEC: dict = {
    "read": {"out": "path,lines[a,b],total_lines,sha,content('N|text')", "err": ["AK-PATH", "AK-READ-BINARY", "AK-READ-SECRET", "AK-READ-SPEC"],
             "fx": ["ro"], "ex": "agentkit read src/app.py:40-80"},
    "search": {"out": "pattern,engine,total,offset,matches['path:line:text']", "err": ["AK-PATH", "AK-SEARCH-REGEX"],
               "fx": ["ro"], "ex": "agentkit search 'def add' src -g '*.py'"},
    "ls": {"out": "dir,total,offset,entries", "err": ["AK-PATH"], "fx": ["ro"], "ex": "agentkit ls src -r"},
    "glob": {"out": "pattern,total,offset,paths", "err": ["AK-PATH"], "fx": ["ro"], "ex": "agentkit glob '**/*.py'"},
    "repo_map": {"out": "files,langs,engine,dirs,symbols{path:['def f:LINE']}", "err": ["AK-PATH"], "fx": ["ro"],
                 "ex": "agentkit repo_map"},
    "edit": {"out": "checkpoint,files[{path,action,sha}],undo | dry_run,would_change",
             "err": ["AK-PATH", "AK-TOOL-EDIT-NOMATCH", "AK-TOOL-EDIT-MULTI", "AK-TOOL-EDIT-SYNTAX", "AK-TOOL-EDIT-STALE", "AK-TOOL-EDIT-OP", "AK-TOOL-WRITE"],
             "fx": ["repo"], "ex": "agentkit edit calc.py --old 'a - b' --new 'a + b'"},
    "patch": {"out": "checkpoint,files[{path,action,sha}],undo",
              "err": ["AK-TOOL-EDIT-OP", "AK-TOOL-EDIT-NOMATCH", "AK-TOOL-EDIT-MULTI", "AK-TOOL-EDIT-SYNTAX", "AK-TOOL-WRITE"],
              "fx": ["repo"], "ex": "agentkit patch --file ops.json --dry-run"},
    "create": {"out": "checkpoint,files[{path,action,sha}],undo", "err": ["AK-PATH", "AK-TOOL-EDIT-OP", "AK-TOOL-EDIT-SYNTAX", "AK-TOOL-WRITE"],
               "fx": ["repo"], "ex": "agentkit create notes.txt 'hello'"},
    "delete": {"out": "checkpoint,files[{path,action}],undo", "err": ["AK-PATH", "AK-TOOL-EDIT-OP"], "fx": ["repo"],
               "ex": "agentkit delete old.py"},
    "rename": {"out": "checkpoint,files[{path,action}],undo", "err": ["AK-PATH", "AK-TOOL-EDIT-OP"], "fx": ["repo"],
               "ex": "agentkit rename a.py b.py"},
    "run": {"out": "exit_code,timed_out,duration_s,stderr_tail,stdout_tail,logs,filtered{filter,lines,raw}?",
            "err": ["AK-RUN-DENIED", "AK-PATH"], "fx": ["exec"], "ex": "agentkit run 'pytest -q' --timeout 120"},
    "checkpoint list": {"do": "list snapshots, newest first", "out": "total,offset,checkpoints['ID time reason [files]']",
                        "err": ["AK-CHECKPOINT"], "fx": ["ro"], "ex": "agentkit checkpoint list"},
    "checkpoint diff": {"do": "diff a snapshot against the current files", "out": "id,changed,offset,diff[lines]",
                        "err": ["AK-CHECKPOINT"], "fx": ["ro"], "ex": "agentkit checkpoint diff cp-00003"},
    "checkpoint rollback": {"do": "restore files from a snapshot (takes a safety snapshot first)",
                            "out": "rolled_back_to,hash_match,files,undo", "err": ["AK-CHECKPOINT"], "fx": ["repo"],
                            "ex": "agentkit checkpoint rollback cp-00003"},
    "checkpoint create": {"do": "snapshot files now", "out": "id,files", "err": ["AK-CHECKPOINT", "AK-PATH"], "fx": ["state"],
                          "ex": "agentkit checkpoint create src/app.py --reason before-refactor"},
    "verify": {"out": "verdict(pass|fail|invalid),checks[{result,exit,s,why?,log?}],changed,suspicious,cost,code?",
               "err": ["AK-VERIFY-NOCHECK", "AK-VERIFY-INVALID", "AK-RUN-DENIED", "AK-GIT", "AK-DISK-FULL"],
               "fx": ["exec"], "ex": "agentkit verify --check 'pytest -q'"},
    "memory add": {"do": "save a project memory", "out": "id,category,pinned,anchors", "err": ["AK-MEMORY"], "fx": ["state"],
                   "ex": "agentkit memory add 'run tests' 'pytest -q' -c tools-models --pin"},
    "memory list": {"do": "list memories", "out": "total,offset,memories['id: title [category]']", "err": ["AK-MEMORY"],
                    "fx": ["ro"], "ex": "agentkit memory list"},
    "memory search": {"do": "full-text search memories", "out": "query,engine,total,offset,results", "err": ["AK-MEMORY"],
                      "fx": ["ro"], "ex": "agentkit memory search 'pytest'"},
    "memory show": {"do": "one memory with body", "out": "id,title,category,pinned,anchors,maybe_stale,body",
                    "err": ["AK-MEMORY"], "fx": ["ro"], "ex": "agentkit memory show run-tests"},
    "memory forget": {"do": "delete a memory", "out": "forgotten", "err": ["AK-MEMORY"], "fx": ["state"],
                      "ex": "agentkit memory forget run-tests"},
    "memory brief": {"do": "AGENTS.md head + pinned memories, ≤500 tokens (default for bare `memory`)",
                     "out": "agents_md,core,index,total", "err": [], "fx": ["ro"], "ex": "agentkit memory brief"},
    "doctor": {"out": "healthy,mode(direct|dispatch),checks[{name,ok,value,fix?}]", "err": [], "fx": ["ro"],
               "fx_online": True, "ex": "agentkit doctor"},
    "model add": {"do": "register an OpenAI-compatible worker model (stores the key's env var NAME only)",
                  "out": "added,config,key_env_set,preset?,warning?", "err": ["AK-USAGE"], "fx": ["state"],
                  "ex": "agentkit model add nim --preset nvidia --model nvidia/nemotron-3.5-lightning-30b-a3b"},
    "model list": {"do": "configured models (no keys)", "out": "models[{name,model,tier,context,price,key_env,key_set,base_url}],default",
                   "err": [], "fx": ["ro"], "ex": "agentkit model list"},
    "model test": {"do": "one tiny chat call to a model", "out": "model,ok,latency_s,reply,usage",
                   "err": ["AK-MODEL-NONE", "AK-MODEL-KEYENV", "AK-MODEL-AUTH", "AK-MODEL-429", "AK-MODEL-TIMEOUT", "AK-MODEL-NETWORK",
                           "AK-MODEL-UNAVAILABLE", "AK-MODEL-HTTP", "AK-MODEL-BAD-RESPONSE"],
                   "fx": ["tokens", "net"], "ex": "agentkit model test cheap"},
    "model remove": {"do": "remove a model from config", "out": "removed", "err": ["AK-MODEL-NONE"], "fx": ["state"],
                     "ex": "agentkit model remove cheap"},
    "task create": {"do": "dispatch a goal to a worker in its own worktree/branch agentkit/<id>",
                    "out": "id,status,deduped?,warnings? (next: task wait)",
                    "err": ["AK-MODEL-NONE", "AK-MODEL-KEYENV", "AK-VERIFY-NOCHECK", "AK-TASK-NOGIT", "AK-DISK-FULL", "AK-GIT"],
                    "fx": ["tokens", "net", "exec", "state"],
                    "ex": "agentkit task create 'fix the failing test in calc.py' --check 'pytest -q' --budget-tokens 200000"},
    "task status": {"do": "<100 tokens per task; bare `task` = recent tasks",
                    "out": "id,status,verdict?,rounds,tok,s,queue?,code?,at?,log?", "err": ["AK-TASK-UNKNOWN"], "fx": ["ro"],
                    "ex": "agentkit task status t1a2b3c"},
    "task wait": {"do": "block until done/timeout; one finished task returns its report inline",
                  "out": "done + report keys | done,tasks[status]", "err": ["AK-TASK-UNKNOWN"], "fx": ["ro"],
                  "ex": "agentkit task wait t1a2b3c --timeout 600"},
    "task report": {"do": "≤300-token verdict; the worker's claim is never trusted",
                    "out": "task,status,verdict(pass|fail|invalid|needs_review),checks,changed,suspicious,cost,error{code,component,step,log}?",
                    "err": ["AK-TASK-UNKNOWN"], "fx": ["ro"], "ex": "agentkit task report t1a2b3c"},
    "task resume": {"do": "continue a paused/interrupted/stopped task in its worktree",
                    "out": "id,status (next: task wait)", "err": ["AK-TASK-UNKNOWN", "AK-TASK-STATE", "AK-BUDGET", "AK-MODEL-KEYENV"],
                    "fx": ["tokens", "net", "exec", "state"], "ex": "agentkit task resume t1a2b3c --budget-tokens 400000"},
    "task cancel": {"do": "stop a queued/running task (this is 'stop')", "out": "status of each task", "err": ["AK-TASK-UNKNOWN"],
                    "fx": ["state"], "ex": "agentkit task cancel t1a2b3c"},
    "task clean": {"do": "remove finished tasks' worktrees (and branches unless --keep-branch)", "out": "cleaned,branches_kept",
                   "err": ["AK-TASK-UNKNOWN", "AK-TASK-STATE", "AK-GIT"], "fx": ["destructive"],
                   "ex": "agentkit task clean --all --keep-branch"},
    "logs": {"do": "structured event log of a run/task (where it failed); --raw: unfiltered run output", "out": "id,events,shown,format,lines,hints{code:fix} | --raw: id,stdout,stderr,logs", "err": ["AK-LOGS-UNKNOWN"],
             "fx": ["ro"], "ex": "agentkit logs t1a2b3c --errors"},
    "debug-bundle": {"do": "redacted .tar.gz of config (no keys), events, report for a bug report", "out": "bundle,bytes,files,error_codes,note (next: feedback --bug)", "err": ["AK-LOGS-UNKNOWN"], "fx": ["state"],
                     "ex": "agentkit debug-bundle t1a2b3c"},
    "feedback": {"out": "posted(false),title,url,search_first,body,rules", "err": ["AK-LOGS-UNKNOWN"], "fx": ["ro"],
                 "ex": "agentkit feedback --idea 'task wait --json-lines'"},
    "uninstall": {"do": "remove package, user config/data, .agentkit/ + worktrees; dry run unless --yes", "out": "plan: repos,user_paths,package,dry_run,hint (next: --yes) | uninstalled,repos,user_paths_removed",
                  "err": ["AK-TASK-STATE"], "fx": ["destructive"], "ex": "agentkit uninstall --this-repo"},
    "manual": {"do": "this manual: overview | <command> detail | --all", "out": "overview | commands{path:detail}", "err": [], "fx": ["ro"], "ex": "agentkit manual task create"},
}

QUICK_START = [
    "agentkit doctor  # healthy:true? mode direct|dispatch; each failed check has fix",
    "agentkit memory brief; agentkit repo_map  # orient (≤500 + paged tokens)",
    "agentkit model add NAME --base-url URL --model ID --key-env VAR  # once, only for delegate mode; then model test NAME",
    "agentkit task create 'GOAL' --check 'pytest -q' --budget-tokens N  # -> id",
    "agentkit task wait ID --timeout 600  # finished -> ≤300-token report inline",
    "review report: pass -> git diff HEAD...agentkit/ID, then git merge agentkit/ID yourself; "
    "needs_review -> inspect suspicious flags + diff; fail/invalid -> read checks[].why",
    "paused/interrupted/stopped -> agentkit task resume ID [--budget-tokens N]; stop -> agentkit task cancel ID; "
    "done with it -> agentkit task clean ID",
]

MODES = {
    "direct": "you read/edit/run with the tools, no model needed; every byte you read costs YOUR (expensive) tokens. "
              "Choose for small/precise edits, hard reasoning, no model configured, or workers keep failing.",
    "delegate": "task create: a cheap/free worker spends its own tokens in an isolated worktree; you pay ~200-600 "
                "(create + wait + ≤300-token report). Choose for clear goals with a test check, multi-file or exploratory "
                "work. Measured: worker 42k-70k vs supervisor 181-235 tokens per real multi-file task.",
    "hybrid": "hard part direct, rest delegated; trust the verifier's verdict, never the worker's claim.",
}

SAFETY = [
    "keys: only the env var NAME is stored (--key-env); values are redacted everywhere and never printed; never pass a key as an argument",
    "feedback only drafts (posted:false); ask your human before filing; no secrets/private code",
    "uninstall is a dry run unless --yes; show the plan to your human first; never touches source or git history",
    "workers cannot commit/push; you merge agentkit/<id>; run/verify refuse git push/commit, sudo, rm -rf /|~, curl|sh, .env/~/.ssh",
    "edit/create/delete/rename/patch are checkpointed: checkpoint rollback ID",
    "money: task create --budget-tokens/--budget-usd/--budget-minutes (stops <5 s); usd needs --price-in/--price-out; ':free' ids cost 0",
    "output fits --budget N tokens (default 2000); truncated:true -> run next",
]

RECOVER = ["AK-MODEL-NONE", "AK-MODEL-KEYENV", "AK-MODEL-AUTH", "AK-MODEL-429", "AK-TASK-NOGIT", "AK-VERIFY-NOCHECK",
           "AK-VERIFY-INVALID", "AK-VERIFY-SUSPICIOUS", "AK-BUDGET", "AK-WORKER-STUCK", "AK-TASK-INTERRUPTED",
           "AK-TOOL-EDIT-NOMATCH", "AK-TOOL-EDIT-STALE", "AK-RUN-DENIED", "AK-INTERNAL"]

COMPRESSION = ("C-08: run and check output is filtered per command (data.filtered{filter,lines,raw}); lines with "
               "error/fail/assert/traceback are never dropped. Unfiltered (redacted) text: agentkit logs RUN --raw "
               "(RUN = filtered.raw or a check's log), or run --raw. Worker context: repeated results become same_as_turn, "
               "old results age to one-line summaries. Tune/disable in .agentkit/config.toml [compress].")

NOTATION = "args: name:type, ? optional, * zero+, + one+, =default, ! required, [r] repeatable, flags are bool"
COMMON_ERR = ["AK-USAGE", "AK-CONFIG", "AK-INTERNAL"]


# ---------------------------------------------------------------- introspection of the real parser


def _subparsers(p: argparse.ArgumentParser):
    return next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)


def walk(parser: Optional[argparse.ArgumentParser] = None) -> dict:
    """{path: (parser, help, aliases)} for every user-visible leaf command, from the live parser."""
    if parser is None:
        from .cli import build_parser

        parser = build_parser()
    out: dict = {}

    def rec(p, prefix):
        sp = _subparsers(p)
        if sp is None:
            return False
        helps = {ca.dest: ca.help for ca in sp._choices_actions}
        seen = {}
        for name, child in sp.choices.items():
            seen.setdefault(id(child), []).append(name)
        for names in seen.values():
            name = names[0]
            if name in HIDDEN or helps.get(name) == argparse.SUPPRESS:
                continue
            child = sp.choices[name]
            path = f"{prefix} {name}".strip()
            if not rec(child, path):
                out[path] = (child, helps.get(name), names[1:])
        return True

    rec(parser, "")
    return out


def _type(a) -> str:
    if a.nargs == 0:
        return "bool"
    t = a.type
    return {int: "int", float: "float"}.get(t, "str") if t else "str"


def arg_info(p: argparse.ArgumentParser) -> list:
    res = []
    for a in p._actions:
        if isinstance(a, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        d = {"name": a.dest if not a.option_strings else max(a.option_strings, key=len), "type": _type(a)}
        if a.option_strings and len(a.option_strings) > 1:
            d["alias"] = min(a.option_strings, key=len)
        if not a.option_strings:
            d["nargs"] = {None: "1", "?": "?", "*": "*", "+": "+"}.get(a.nargs, str(a.nargs))
        if a.required and a.option_strings:
            d["required"] = True
        if isinstance(a, argparse._AppendAction):
            d["repeatable"] = True
        if a.nargs != 0 and not (a.default is None or a.default is False or a.default == [] or a.default == argparse.SUPPRESS):
            d["default"] = a.default
        if a.choices:
            d["choices"] = list(a.choices)
        if a.help and a.help != argparse.SUPPRESS:
            d["help"] = a.help
        groups = [g for g in p._mutually_exclusive_groups if a in g._group_actions]
        if groups:
            d["one_of"] = [max(x.option_strings, key=len) for x in groups[0]._group_actions]
        res.append(d)
    return res


def compact_arg(d: dict) -> str:
    s = d["name"]
    if d["type"] != "bool":
        s += ":" + d["type"]
    if d.get("nargs") not in (None, "1"):
        s += d["nargs"]
    if "default" in d:
        s += f"={d['default']}"
    if d.get("required"):
        s += "!"
    if d.get("repeatable"):
        s += "[r]"
    return s


def fx_of(path: str) -> list:
    s = SPEC[path]
    return list(s["fx"]) + (["tokens if --online"] if s.get("fx_online") else [])


def purpose(path: str, help_: Optional[str]) -> str:
    return SPEC[path].get("do") or help_ or ""


def detail(path: str, entry) -> dict:
    p, help_, aliases = entry
    s = SPEC[path]
    d = {"do": purpose(path, help_), "args": arg_info(p), "out": s["out"], "err": s["err"],
         "fx": fx_of(path), "ex": s["ex"]}
    if aliases:
        d["aliases"] = aliases
    return d


def compact_detail(path: str, entry) -> dict:
    d = detail(path, entry)
    d["args"] = [compact_arg(x) for x in d["args"]]
    d["fx"] = ",".join(d["fx"])
    if not d["args"]:
        del d["args"]
    if not d["err"]:
        del d["err"]
    return d


def detail_json(path: str, entry) -> dict:
    """Per-command view: full arg objects (type, default, help) since the caller asked for just this command."""
    d = detail(path, entry)
    d["fx"] = ",".join(d["fx"])
    for k in ("args", "err"):
        if not d[k]:
            del d[k]
    return d


def recover() -> dict:
    """code -> fix, straight from the error registry (causes: docs/errors.md / agentkit manual --format md)."""
    from .events import ERRORS

    return {c: ERRORS[c][1] for c in RECOVER}


def short(text: str, n: int = 64) -> str:
    text = text.split(" (")[0] if len(text) > n else text
    return text if len(text) <= n else text[: n].rsplit(" ", 1)[0] + "…"


def overview(tree: Optional[dict] = None) -> dict:
    tree = tree or walk()
    return {
        "about": "AgentKit: token-frugal coding tools for AI agents + optional cheap worker with independent verification. "
                 "Every command prints ONE JSON line {ok,data,error{what,why,next,code,run},truncated,next}.",
        "globals": "--budget N (output tokens, default 2000), --human (text), --root DIR; anywhere on the line",
        "quick_start": QUICK_START,
        "modes": MODES,
        "safety": SAFETY,
        "recover": recover(),
        "compression": COMPRESSION,
        "fx_legend": FX,
        "commands": {k: f"{short(purpose(k, v[1]))} [{','.join(fx_of(k))}]" for k, v in tree.items()},
        "more": "agentkit manual <command> (detail: args, out, err, fx, ex) | agentkit manual --all | docs/agent-manual.md | docs/errors.md",
    }


def full(tree: Optional[dict] = None) -> dict:
    tree = tree or walk()
    ov = overview(tree)
    ov.pop("more")
    ov["notation"] = NOTATION
    ov["common_err"] = COMMON_ERR
    ov["commands"] = {k: compact_detail(k, v) for k, v in tree.items()}
    return ov


def select(words: list, tree: Optional[dict] = None) -> dict:
    tree = tree or walk()
    key = " ".join(words)
    alias = {a: k for k, (_, _, al) in tree.items() for a in al}
    key = alias.get(key, key)
    hits = {k: v for k, v in tree.items() if k == key or k.startswith(key + " ")}
    if not hits:
        raise AKError(f"no manual entry for '{key}'", "not a command", "agentkit manual", code="AK-USAGE")
    return hits


def tokens(obj) -> int:
    return len(dumps(obj)) // CHARS_PER_TOKEN


# ---------------------------------------------------------------- markdown


def _md_cmd(path: str, d: dict) -> list:
    from .events import ERRORS

    out = [f"### `agentkit {path}`", "", d["do"], ""]
    if d.get("aliases"):
        out += [f"Aliases: {', '.join('`' + a + '`' for a in d['aliases'])}", ""]
    if d["args"]:
        out += ["| arg | type | default | notes |", "|---|---|---|---|"]
        for a in d["args"]:
            notes = []
            if a.get("alias"):
                notes.append(f"alias `{a['alias']}`")
            if a.get("nargs") and a["nargs"] != "1":
                notes.append({"?": "optional", "*": "zero or more", "+": "one or more"}.get(a["nargs"], a["nargs"]))
            if a.get("required"):
                notes.append("required")
            if a.get("repeatable"):
                notes.append("repeatable")
            if a.get("one_of"):
                notes.append("one of " + " / ".join(a["one_of"]))
            if a.get("help"):
                notes.append(a["help"].replace("|", "\\|"))
            out.append(f"| `{a['name']}` | {a['type']} | {('`' + str(a['default']) + '`') if 'default' in a else ''} | {'; '.join(notes)} |")
        out.append("")
    out += [f"- **Output `data`:** `{d['out']}`",
            "- **Errors:** " + (", ".join(f"`{c}` ({ERRORS[c][0]})" for c in d["err"]) or "only the common ones"),
            "- **Side effects:** " + ", ".join(d["fx"]),
            f"- **Example:** `{d['ex']}`", ""]
    return out


def markdown_overview() -> str:
    """--human text for bare `agentkit manual`: the guide sections + a one-line command index."""
    return markdown(index_only=True)


def markdown(tree: Optional[dict] = None, words: Optional[list] = None, index_only: bool = False) -> str:
    from .events import ERRORS

    tree = tree or walk()
    if words:
        sel = select(words, tree)
        return "\n".join(ln for k, v in sel.items() for ln in _md_cmd(k, detail(k, v))).rstrip() + "\n"
    ov = overview(tree)
    out = ["# AgentKit operating manual (for the supervising AI)", "",
           "<!-- GENERATED by `agentkit manual --format md > docs/agent-manual.md` from the CLI definitions "
           "(agentkit/cli.py, agentkit/manual.py) and the error registry. Do not edit by hand; tests fail on drift. -->", "",
           ov["about"], "", f"Global options: {ov['globals']}.", "",
           f"JSON form: `agentkit manual` (overview, ~{tokens(ov)} tokens), `agentkit manual <command>` (one command), "
           f"`agentkit manual --all` (everything, ~{tokens(full(tree))} tokens).", "",
           "## Quick start (supervisor)", ""]
    out += [f"{i}. `{s.split('  # ')[0]}`" + (f" — {s.split('  # ')[1]}" if "  # " in s else "") if s.startswith("agentkit") else f"{i}. {s}"
            for i, s in enumerate(QUICK_START, 1)]
    out += ["", "## Direct vs delegate mode", ""] + [f"- **{k}**: {v}" for k, v in MODES.items()]
    out += ["", "## Budget and safety rules", ""] + [f"- {s}" for s in SAFETY]
    out += ["", "## Recovering from common errors", "", "| code | cause | fix |", "|---|---|---|"]
    out += [f"| `{c}` | {ERRORS[c][0]} | {ERRORS[c][1].replace('|', chr(92) + '|')} |" for c in RECOVER]
    out += ["", f"Every command can also fail with {', '.join('`' + c + '`' for c in COMMON_ERR)}. Full registry: docs/errors.md.",
            "", "## Output compression", "", COMPRESSION, "", "## Side-effect legend", ""]
    out += [f"- `{k}`: {v}" for k, v in FX.items()]
    out += ["", "## Commands", ""]
    if index_only:
        out += [f"- `agentkit {k}` — {v}" for k, v in ov["commands"].items()]
        out += ["", "Detail: `agentkit manual <command> --human`."]
        return "\n".join(out).rstrip() + "\n"
    for k, v in tree.items():
        out += _md_cmd(k, detail(k, v))
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- command


def cmd_manual(ctx, a) -> Result:
    tree = walk()
    if a.words:
        sel = select(a.words, tree)
        return Result({"commands": {k: detail_json(k, v) for k, v in sel.items()},
                       "common_err": COMMON_ERR, "fx_legend": {t: FX[t] for t in FX if any(t in SPEC[k]["fx"] for k in sel)}})
    if a.all:
        data = full(tree)
        need = len(dumps({"ok": True, "data": data, "error": None, "truncated": False, "next": None})) // CHARS_PER_TOKEN + 50
        if need > ctx.budget:
            n = (need // 500 + 1) * 500
            return Result({"tokens": need, "hint": "the full manual is larger than --budget; rerun with next, or fetch "
                           "agentkit manual (overview) + agentkit manual <command>", "commands": list(tree)},
                          True, f"agentkit manual --all --budget {n}")
        return Result(data)
    return Result(overview(tree))


def main() -> int:  # python -m agentkit.manual > docs/agent-manual.md
    import sys

    sys.stdout.write(markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
