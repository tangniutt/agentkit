# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Fail-to-pass task verification without starting a worker process."""
import json
import subprocess
import sys

from conftest import git
from agentkit import tasks

CHECK = f"{sys.executable} -m pytest -q -p no:cacheprovider tests"


def _repo(repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n.agentkit/\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def _task(repo, base, baseline_must_fail=True):
    tid = "t123abc"
    wt = repo / ".agentkit" / "worktrees" / tid
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "-b", f"agentkit/{tid}", str(wt), base)
    d = repo / ".agentkit" / "tasks" / tid
    d.mkdir(parents=True)
    acceptance = {"checks": [CHECK], "allow": [], "baseline_must_fail": baseline_must_fail}
    task = {
        "id": tid, "goal": "fix add", "checks": [CHECK], "allow": [], "model": "fake", "tier": "low",
        "budget": {"tokens": 1000, "usd": 0, "minutes": 5}, "hash": "h", "base": base,
        "branch": f"agentkit/{tid}", "worktree": f".agentkit/worktrees/{tid}", "status": "verifying",
        "worker_status": "submitted", "created": 1, "rounds": 1, "tokens": 10, "usd": 0.0,
        "acceptance": acceptance, "acceptance_sha256": "a" * 64,
    }
    (d / "task.json").write_text(json.dumps(task), encoding="utf-8")
    return tid, wt


def test_verify_task_fail_to_pass(repo):
    base = _repo(repo)
    tid, wt = _task(repo, base)
    (wt / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    rep = tasks.verify_task(repo, tid)
    assert rep["verdict"] == "pass", rep
    assert rep["baseline"]["result"] == "fail"
    assert rep["acceptance_sha256"] == "a" * 64
    assert rep["receipt"]["id"].startswith("er_")
    receipt = json.loads((repo / ".agentkit" / "tasks" / tid / "receipt.json").read_text())
    assert receipt["verdict"] == "pass" and receipt["diff_sha256"] and receipt["environment_sha256"]
    assert (repo / ".agentkit" / "retrospectives" / f"rp_{tid}.json").exists()


def test_verify_task_rejects_baseline_pass(repo):
    base = _repo(repo)
    tid, _wt = _task(repo, base)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git(repo, "add", "calc.py")
    git(repo, "commit", "-qm", "already fixed")
    # Point the task base at the fixed commit so the acceptance already passes.
    p = repo / ".agentkit" / "tasks" / tid / "task.json"
    task = json.loads(p.read_text())
    task["base"] = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    p.write_text(json.dumps(task), encoding="utf-8")
    rep = tasks.verify_task(repo, tid)
    assert rep["verdict"] == "invalid", rep
    assert rep["baseline"]["result"] == "pass"
    assert "baseline checks did not fail" in rep["why"]


def test_verify_task_allows_explicit_baseline_pass(repo):
    base = _repo(repo)
    tid, wt = _task(repo, base, baseline_must_fail=False)
    (wt / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    rep = tasks.verify_task(repo, tid)
    assert rep["verdict"] == "pass", rep
    assert rep["baseline"] == {"required": False, "result": "skipped", "checks": []}
