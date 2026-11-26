# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""`agentkit task resume ID`: paused / interrupted / stopped tasks continue in the same worktree
with a compacted transcript (change C-05)."""
import json
import os
import signal
import time

import pytest

from test_m2_tasks import CHECK, create, project, tjson, wait

pytestmark = pytest.mark.skipif(os.name != "posix", reason="dispatch mode requires Linux")

RESUMED = "Resuming task"  # marker only present in the resume context -> its own fake script


def test_paused_task_resumes_in_same_worktree_with_compacted_transcript(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[RES1]", [
        {"call": "read", "args": {"spec": "calc.py"}},
        {"call": "edit", "args": {"path": "calc.py", "old": "a - b", "new": "a + b"}, "sig": "sig-old-turn"},
        {"call": "run", "args": {"command": CHECK}, "before": [{"http": 429, "retry_after": 1}, {"http": 429, "retry_after": 1}]},
    ])
    tid = create(ak, "fix add [RES1]")
    s = wait(ak, tid)
    assert s["status"] == "paused" and "429" in s["why"]
    before = tjson(repo, tid)
    rep = ak("task", "report", tid)
    assert rep["next"] == f"agentkit task resume {tid}"
    wt = repo / before["worktree"]
    assert "a + b" in (wt / "calc.py").read_text()

    fake.add(RESUMED, [{"call": "run", "args": {"command": CHECK}}, {"call": "finish", "args": {"summary": "done after resume"}}])
    n_before = len(fake.requests)
    r = ak("task", "resume", tid)
    assert r["ok"] and r["data"]["resumes"] == 1 and r["next"].startswith("agentkit task wait")
    s = wait(ak, tid)
    assert s["status"] == "done" and s["verdict"] == "pass", s
    after = tjson(repo, tid)
    assert after["worktree"] == before["worktree"] and after["branch"] == before["branch"]
    assert after["tokens"] > before["tokens"] and after["rounds"] > before["rounds"], "counters are cumulative"
    first = fake.requests[n_before]["body"]["messages"]
    assert [m["role"] for m in first] == ["system", "user"], "compacted: no raw assistant turns replayed"
    ctx = first[1]["content"]
    assert RESUMED in ctx and "edit" in ctx and "a + b" in ctx and "429" in ctx and "calc.py" in ctx
    assert "sig-old-turn" not in json.dumps(first), "stale thought signatures are not replayed"
    assert len(ctx) < 7000
    tr = (repo / ".agentkit" / "tasks" / tid / "transcript.jsonl").read_text()
    assert '"_resume"' in tr
    d = ak("task", "report", tid)["data"]
    assert d["verdict"] == "pass" and d["changed"] == ["M calc.py"]


def test_interrupted_task_resumes(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[RES2]", [{"call": "edit", "args": {"path": "calc.py", "old": "a - b", "new": "a + b"}},
                        {"call": "read", "args": {"spec": "calc.py"}, "before": [{"sleep": 30}]}])
    tid = create(ak, "fix add [RES2]")
    pid = None
    for _ in range(100):
        t = tjson(repo, tid)
        if t["status"] == "running" and t.get("rounds", 0) >= 1 and t.get("pid"):
            pid = t["pid"]
            break
        time.sleep(0.1)
    assert pid
    time.sleep(0.5)
    os.killpg(pid, signal.SIGKILL)
    time.sleep(0.5)
    assert ak("task", "status", tid)["data"]["status"] == "interrupted"
    assert "task resume" in tjson(repo, tid)["reason"]
    fake.add(RESUMED, [{"call": "finish", "args": {"summary": "already fixed"}}])
    assert ak("task", "resume", tid)["ok"]
    s = wait(ak, tid)
    assert s["status"] == "done" and s["verdict"] == "pass", s


def test_resume_refusals_and_budget(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[RES3]", [{"call": "read", "args": {"spec": "calc.py"}, "usage": {"prompt_tokens": 5000, "completion_tokens": 10}}])
    tid = create(ak, "fix add [RES3]", "--budget-tokens", "3000")
    s = wait(ak, tid)
    assert s["status"] == "stopped" and s["stopped"] == "budget"
    r = ak("task", "resume", tid)
    assert not r["ok"] and r["error"]["code"] == "AK-TASK-STATE" and "--budget-tokens" in r["error"]["next"]
    fake.add(RESUMED, [{"call": "edit", "args": {"path": "calc.py", "old": "a - b", "new": "a + b"}},
                       {"call": "finish", "args": {"summary": "ok"}}])
    r = ak("task", "resume", tid, "--budget-tokens", "100000")
    assert r["ok"], r
    s = wait(ak, tid)
    assert s["status"] == "done" and s["verdict"] == "pass", s
    # done tasks cannot be resumed; cleaned tasks neither
    r = ak("task", "resume", tid)
    assert not r["ok"] and r["error"]["code"] == "AK-TASK-STATE"
    assert not ak("task", "resume")["ok"]
    assert not ak("task", "resume", "tffffff")["ok"]


def test_cleaned_task_cannot_resume(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[RES4]", [{"call": "read", "args": {"spec": "calc.py"}, "before": [{"http": 429, "retry_after": 1}] * 2}])
    tid = create(ak, "fix add [RES4]")
    assert wait(ak, tid)["status"] == "paused"
    assert ak("task", "clean", tid)["ok"]
    r = ak("task", "resume", tid)
    assert not r["ok"] and "worktree is gone" in r["error"]["what"]


def test_handoff_switches_model_and_preserves_worktree_budget(ak, repo, fake):
    project(repo, ak, fake)
    assert ak("model", "add", "fake-b", "--base-url", fake.url, "--key-env", "FAKE_KEY",
              "--price-in", "1", "--price-out", "2")["ok"]
    fake.add("[HANDOFF]", [
        {"call": "edit", "args": {"path": "calc.py", "old": "a - b", "new": "a + b"}},
        {"call": "run", "args": {"command": CHECK}, "before": [{"http": 429, "retry_after": 1}] * 2},
    ])
    tid = create(ak, "fix add [HANDOFF]", "--budget-tokens", "100000")
    assert wait(ak, tid)["status"] == "paused"
    before = tjson(repo, tid)
    wt = repo / before["worktree"]
    assert "a + b" in (wt / "calc.py").read_text()

    fake.add(RESUMED, [{"call": "run", "args": {"command": CHECK}},
                       {"call": "finish", "args": {"summary": "done after handoff"}}])
    r = ak("task", "handoff", tid, "--model", "fake-b")
    assert r["ok"] and r["data"]["handoff"] and r["data"]["from_model"] == "fake"
    assert r["data"]["to_model"] == "fake-b" and r["data"]["handoffs"] == 1
    s = wait(ak, tid)
    assert s["status"] == "done" and s["verdict"] == "pass", s
    after = tjson(repo, tid)
    assert after["worktree"] == before["worktree"] and after["branch"] == before["branch"]
    assert after["model"] == "fake-b" and after["handoffs"] == 1
    assert after["model_history"][-1]["from"] == "fake" and after["model_history"][-1]["to"] == "fake-b"
    assert after["tokens"] >= before["tokens"] and after["rounds"] >= before["rounds"]


def test_handoff_requires_different_model_and_respects_limit(ak, repo, fake):
    project(repo, ak, fake, cfg="", model_cfg="")
    p = repo / ".agentkit" / "config.toml"
    p.write_text(p.read_text() + "\n[tasks]\nmax_handoffs = 0\n")
    assert ak("model", "add", "fake-b", "--base-url", fake.url, "--key-env", "FAKE_KEY")["ok"]
    fake.add("[HANDOFF-LIMIT]", [{"call": "read", "args": {"spec": "calc.py"},
                                  "before": [{"http": 429, "retry_after": 1}] * 2}])
    tid = create(ak, "fix add [HANDOFF-LIMIT]")
    assert wait(ak, tid)["status"] == "paused"
    same = ak("task", "handoff", tid, "--model", "fake")
    assert not same["ok"] and "already uses model" in same["error"]["what"]
    over = ak("task", "handoff", tid, "--model", "fake-b")
    assert not over["ok"] and "max_handoffs" in over["error"]["what"]
    missing = ak("task", "handoff", tid)
    assert not missing["ok"] and missing["error"]["code"] == "AK-USAGE"
