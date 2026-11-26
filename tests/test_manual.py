# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""C-09: the supervisor manual is generated from the live CLI and cannot drift."""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agentkit import manual as M
from agentkit.cli import build_parser, split_globals
from agentkit.events import ERRORS

ROOT = Path(__file__).resolve().parent.parent
TOK = 4  # chars per token


def test_every_registered_command_has_an_entry_and_no_stale_entries():
    tree = M.walk()
    assert set(tree) == set(M.SPEC), f"missing: {set(tree) - set(M.SPEC)}, stale: {set(M.SPEC) - set(tree)}"
    # every user-visible handler is covered (aliases resolve to their primary name)
    from agentkit.cli import _handlers

    top = {k.split(" ")[0] for k in tree}
    assert {h for h in _handlers() if h not in ("_task-run", "repo-map", "map")} == top


def test_a_new_command_without_an_entry_is_detected():
    p = build_parser()
    sp = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    sp.add_parser("brand-new", help="x")
    tree = M.walk(p)
    assert set(tree) - set(M.SPEC) == {"brand-new"}
    with pytest.raises(KeyError):
        M.overview(tree)


@pytest.mark.parametrize("path", sorted(M.SPEC))
def test_example_parses_to_its_own_command(path):
    ex = M.SPEC[path]["ex"]
    words = shlex.split(ex)
    assert words[0] == "agentkit"
    rest, *_ = split_globals(words[1:])
    a = build_parser().parse_args(rest)
    got = [a.cmd] + [getattr(a, d) for d in ("cp_cmd", "mem_cmd", "model_cmd", "task_cmd", "goal_cmd", "code_cmd", "memory_cmd", "release_cmd") if getattr(a, d, None)]
    primary = {"repo-map": "repo_map", "map": "repo_map"}
    got[0] = primary.get(got[0], got[0])
    assert " ".join(got) == path, (ex, got)


def test_error_codes_exist_in_registry_and_docs():
    docs = (ROOT / "docs" / "errors.md").read_text(encoding="utf-8")
    used = set(M.COMMON_ERR) | set(M.RECOVER) | {c for s in M.SPEC.values() for c in s["err"]}
    for c in used:
        assert c in ERRORS, c
        assert f"`{c}`" in docs, c


def test_side_effect_tags_are_known_and_writers_are_flagged():
    for path, s in M.SPEC.items():
        assert s["fx"] and set(s["fx"]) <= set(M.FX), path
    for p in ("edit", "patch", "create", "delete", "rename", "checkpoint rollback"):
        assert "repo" in M.SPEC[p]["fx"]
    for p in ("task create", "task resume", "model test"):
        assert "tokens" in M.SPEC[p]["fx"]
    assert "destructive" in M.SPEC["uninstall"]["fx"] and "destructive" in M.SPEC["task clean"]["fx"]


def test_args_come_from_the_parser():
    d = M.detail("task wait", M.walk()["task wait"])
    names = {a["name"]: a for a in d["args"]}
    assert names["--timeout"]["type"] == "float" and names["--timeout"]["default"] == 300
    assert names["--no-report"]["type"] == "bool" and names["ids"]["nargs"] == "*"
    ma = {a["name"]: a for a in M.detail("model add", M.walk()["model add"])["args"]}
    assert "--preset" in ma and "required" not in ma["--base-url"] and ma["--context"]["type"] == "int"
    assert M.compact_arg({"name": "--offset", "type": "int", "default": 0}) == "--offset:int=0"
    assert "--fixed:bool" not in M.full()["commands"]["search"]["args"] and "--fixed" in M.full()["commands"]["search"]["args"]


def test_overview_is_valid_json_fits_default_budget(ak):
    r = ak("manual")
    assert r["ok"] and not r["truncated"]
    d = r["data"]
    for k in ("quick_start", "modes", "safety", "recover", "compression", "commands"):
        assert d[k], k
    assert set(d["modes"]) >= {"direct", "delegate"} and "logs" in d["compression"] and "--raw" in d["compression"]
    assert set(d["commands"]) == set(M.SPEC)
    assert any("--yes" in s for s in d["safety"]) and any("never printed" in s for s in d["safety"])
    assert any("posted:false" in s for s in d["safety"])
    assert len(r["_raw"]) // TOK < 2000, len(r["_raw"]) // TOK


def test_full_manual_compact_and_paged_by_budget(ak):
    r = ak("manual", "--all")
    assert r["truncated"] and r["next"].startswith("agentkit manual --all --budget")
    full = ak.next(r)
    assert full["ok"] and not full["truncated"] and set(full["data"]["commands"]) == set(M.SPEC)
    n = len(full["_raw"]) // TOK
    assert n < 5000, f"full manual is {n} tokens"
    for path, d in full["data"]["commands"].items():
        assert d["do"] and d["out"] and d["fx"] and d["ex"].startswith("agentkit "), path


@pytest.mark.parametrize("words", [["task", "create"], ["task"], ["map"], ["manual"]])
def test_per_command_detail(ak, words):
    r = ak("manual", *words)
    cmds = r["data"]["commands"]
    assert r["ok"] and cmds and not r["truncated"]
    if words == ["map"]:
        assert list(cmds) == ["repo_map"] and cmds["repo_map"]["aliases"] == ["repo-map", "map"]
    if words == ["task"]:
        assert set(cmds) == {k for k in M.SPEC if k.startswith("task ")}


def test_per_command_detail_sizes():
    tree = M.walk()
    for k, v in tree.items():
        env = {"ok": True, "data": {"commands": {k: M.detail_json(k, v)}, "common_err": M.COMMON_ERR}}
        assert len(json.dumps(env, separators=(",", ":"))) // TOK < 450, k


def test_unknown_command_is_usage_error(ak):
    r = ak("manual", "nope")
    assert not r["ok"] and r["error"]["code"] == "AK-USAGE" and r["_code"] == 1


def test_markdown_doc_is_generated_and_committed():
    doc = (ROOT / "docs" / "agent-manual.md").read_text(encoding="utf-8")
    assert doc == M.markdown(), "docs/agent-manual.md is stale: agentkit manual --format md > docs/agent-manual.md"
    for k in M.SPEC:
        assert f"### `agentkit {k}`" in doc
    p = subprocess.run([sys.executable, "-m", "agentkit", "manual", "--format", "md"], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert p.returncode == 0 and p.stdout == doc


def test_human_renders_text(repo):
    p = subprocess.run([sys.executable, "-m", "agentkit", "manual", "--human"], cwd=repo, capture_output=True,
                       text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert p.returncode == 0 and p.stdout.startswith("# AgentKit operating manual") and "## Quick start" in p.stdout
    assert "### `agentkit" not in p.stdout  # overview: index only
    p = subprocess.run([sys.executable, "-m", "agentkit", "manual", "task", "wait", "--human"], cwd=repo, capture_output=True,
                       text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert "### `agentkit task wait`" in p.stdout and "--no-report" in p.stdout


def test_manual_is_linked_from_entry_docs():
    for f in ("llms.txt", "AGENTS.md", "README.md"):
        t = (ROOT / f).read_text(encoding="utf-8")
        assert "agentkit manual" in t and "docs/agent-manual.md" in t, f


def test_top_level_help_points_to_manual(ak):
    assert ak()["data"]["manual"].startswith("agentkit manual")


def test_no_secret_like_strings_in_manual():
    doc = M.markdown() + json.dumps(M.full())
    assert not re.search(r"(sk-[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{12,})", doc)
