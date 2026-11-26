# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Task acceptance must discriminate the base commit from the fixed tree."""
import json
import os

import pytest

from agentkit.tasks import verify_task
from conftest import git
from test_m2_tasks import FIX, create, project, tjson, wait

pytestmark = pytest.mark.skipif(os.name != "posix", reason="dispatch mode requires Linux")


def test_task_requires_baseline_failure(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[FAIL-TO-PASS]", FIX)
    tid = create(ak, "fix add [FAIL-TO-PASS]")
    s = wait(ak, tid)
    assert s["status"] == "done" and s["verdict"] == "pass", s
    task = tjson(repo, tid)
    assert task["acceptance"]["baseline_must_fail"] is True
    assert task["acceptance_sha256"]
    assert task["sandbox_network"] == ("shared" if task["sandbox"] == "bwrap" else "host")
    rep = ak("task", "report", tid)["data"]
    assert rep["baseline"] == {"required": True, "result": "fail"}
    assert rep["sandbox_network"] == task["sandbox_network"]
    receipt = json.loads((repo / ".agentkit" / "tasks" / tid / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["worker"]["sandbox_network"] == task["sandbox_network"]
    receipt_path = repo / ".agentkit" / "evidence" / f"{receipt['receipt_id']}.json"
    receipt_path.write_text('{"forged":true}', encoding="utf-8")
    reverified = verify_task(repo, tid)
    current_path = repo / ".agentkit" / "evidence" / f"{reverified['receipt']['id']}.json"
    assert "forged" not in json.loads(current_path.read_text(encoding="utf-8"))


def test_task_rejects_acceptance_that_already_passes(ak, repo, fake):
    project(repo, ak, fake)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(repo, "add", "calc.py")
    git(repo, "commit", "-qm", "base passes")
    fake.add("[BASE-PASS]", [{"call": "finish", "args": {"summary": "nothing to change"}}])
    tid = create(ak, "inspect add [BASE-PASS]")
    s = wait(ak, tid)
    assert s["status"] == "done" and s["verdict"] == "invalid", s
    rep = ak("task", "report", tid)["data"]
    assert rep["baseline"] == {"required": True, "result": "pass"}
    assert "baseline checks did not fail" in rep["why"]


def test_allow_baseline_pass_is_explicit(ak, repo, fake):
    project(repo, ak, fake)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(repo, "add", "calc.py")
    git(repo, "commit", "-qm", "base passes")
    fake.add("[ALLOW-BASE-PASS]", [{"call": "finish", "args": {"summary": "already good"}}])
    tid = create(ak, "inspect add [ALLOW-BASE-PASS]", "--allow-baseline-pass")
    s = wait(ak, tid)
    assert s["status"] == "done" and s["verdict"] == "pass", s
    task = tjson(repo, tid)
    assert task["acceptance"]["baseline_must_fail"] is False
    rep = ak("task", "report", tid)["data"]
    assert rep["baseline"] == {"required": False, "result": "skipped"}
