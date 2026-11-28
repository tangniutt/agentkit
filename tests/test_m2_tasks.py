# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-12 worker loop, M-13 task system, M-14 isolation, M-16 budgets, M-17 key hygiene, M-21 auto-memory."""
import base64
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import git

PY = sys.executable
KEY = "fk" + "-" + "task" + "5" * 20  # fake, assembled at runtime
CHECK = f"{PY} -m pytest -q -p no:cacheprovider tests"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="dispatch mode requires Linux")

FIX = [{"call": "read", "args": {"spec": "calc.py"}},
       {"call": "edit", "args": {"path": "calc.py", "old": "a - b", "new": "a + b"}},
       {"call": "run", "args": {"command": CHECK}},
       {"call": "finish", "args": {"summary": "fixed"}}]


def project(repo, ak, fake, cfg: str = "", model_cfg: str = "", context: int = 32000):
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (repo / ".gitignore").write_text("__pycache__/\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    ak.env["FAKE_KEY"] = KEY
    r = ak("model", "add", "fake", "--base-url", fake.url, "--key-env", "FAKE_KEY", "--price-in", "1", "--price-out", "2",
           "--context", str(context))
    assert r["ok"], r
    p = repo / ".agentkit" / "config.toml"
    text = p.read_text()
    if model_cfg:
        text = text.rstrip("\n") + "\n" + model_cfg + "\n"
    text += "\n[worker]\nbackoff_s = 0.05\n" + cfg + "\n"
    p.write_text(text)


def create(ak, goal, *extra):
    r = ak("task", "create", goal, "--check", CHECK, *extra)
    assert r["ok"], r
    return r["data"]["id"]


def wait(ak, tid, timeout=90):
    r = ak("task", "wait", tid, "--timeout", str(timeout), "--no-report")
    assert r["data"]["done"], r
    return r["data"]["tasks"][0]


def tjson(repo, tid):
    return json.loads((repo / ".agentkit" / "tasks" / tid / "task.json").read_text())


def test_task_end_to_end_isolated(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[E2E]", FIX)
    tid = create(ak, "fix add [E2E]")
    st = ak("task", "status", tid)
    assert len(json.dumps(st["data"])) < 400, "status must stay under 100 tokens"
    s = wait(ak, tid)
    assert s["status"] == "done" and s["verdict"] == "pass"
    rep = ak("task", "report", tid)
    assert len(rep["_raw"]) <= 1200, "report must stay under 300 tokens"
    d = rep["data"]
    assert d["verdict"] == "pass" and d["changed"] == ["M calc.py"] and d["suspicious"] == []
    assert set(d) >= {"verdict", "checks", "changed", "suspicious", "cost"}
    assert d["cost"]["tokens"] > 0 and d["cost"]["usd"] > 0
    # main tree untouched, work lives on the task branch / worktree
    assert "a - b" in (repo / "calc.py").read_text()
    wt = repo / ".agentkit" / "worktrees" / tid
    assert "a + b" in (wt / "calc.py").read_text()
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "agentkit/*"], capture_output=True, text=True).stdout
    assert f"agentkit/{tid}" in branches
    # the worker started from the memory brief (system prompt carries it)
    first = fake.requests_for("[E2E]")[0]["body"]["messages"][0]["content"]
    assert "PROJECT MEMORY" in first and "fix add [E2E]" in first
    # git status of the main repo shows no worktree files
    st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "-uall"], capture_output=True, text=True).stdout
    assert "worktrees/" not in st and "tasks/" not in st
    # cleanup removes worktree + branch
    c = ak("task", "clean", tid)
    assert c["ok"] and not wt.exists()
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "agentkit/*"], capture_output=True, text=True).stdout
    assert tid not in branches


def test_lean_create_and_wait_with_inline_report(ak, repo, fake):
    """C-06: create stays lean but explicitly reports dedupe state; wait carries the report."""
    project(repo, ak, fake)
    fake.add("[LEAN]", FIX)
    r = ak("task", "create", "fix add [LEAN]", "--check", CHECK)
    assert set(r["data"]) == {"id", "status", "deduped"} and r["data"]["deduped"] is False
    assert r["next"].startswith("agentkit task wait " + r["data"]["id"])
    w = ak("task", "wait", r["data"]["id"], "--timeout", "90")
    d = w["data"]
    assert d["done"] and d["verdict"] == "pass" and d["changed"] == ["M calc.py"] and d["suspicious"] == []
    assert d["checks"] == [{"result": "pass", "exit": 0, "s": d["checks"][0]["s"]}], "passing check: no cmd/log echo"
    assert "env" not in d and "branch" not in d and "claim_untrusted" not in d and w["next"] is None
    assert len(w["_raw"]) <= 1200
    full = ak("task", "report", r["data"]["id"])["data"]
    assert {k: v for k, v in full.items()} == {k: v for k, v in d.items() if k != "done"}
    stored = json.loads((repo / ".agentkit" / "tasks" / r["data"]["id"] / "report.json").read_text(encoding="utf-8"))
    details = stored["round_details"]
    assert details and all({"round", "tokens_in", "tokens_out", "model_s", "actions"} <= set(item) for item in details)
    assert sum(item["tokens_in"] + item["tokens_out"] for item in details) == stored["cost"]["tokens"]
    assert "round_details" not in d and "round_details" not in full
    detailed = ak("task", "report", r["data"]["id"], "--details", budget=4000)["data"]
    assert detailed["round_details"] == details


def test_task_refuses_non_git_and_empty_repo(ak, repo, fake):
    ak.env["FAKE_KEY"] = KEY
    ak("model", "add", "fake", "--base-url", fake.url, "--key-env", "FAKE_KEY")
    r = ak("task", "create", "do x", "--check", "true")
    assert not r["ok"] and "not a git repository" in r["error"]["what"] and "git init" in r["error"]["next"]
    git(repo, "init", "-q", "-b", "main")
    r = ak("task", "create", "do x", "--check", "true")
    assert not r["ok"] and "no commits" in r["error"]["what"]


def test_task_missing_key_env_refused(ak, repo, fake):
    project(repo, ak, fake)
    r = ak("task", "create", "x", "--check", CHECK, env={"FAKE_KEY": ""})
    assert not r["ok"] and "FAKE_KEY" in r["error"]["what"]


def test_task_attachments_require_vision_and_reach_model(ak, repo, fake):
    project(repo, ak, fake)
    png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6nWQAAAAASUVORK5CYII="
    image = repo / "reference.png"
    image.write_bytes(base64.b64decode(png_b64))
    rejected = ak("task", "create", "fix add [VISION-NO]", "--check", CHECK, "--attach", "reference.png")
    assert not rejected["ok"] and rejected["error"]["code"] == "AK-MODEL-CAPABILITY"
    enabled = ak("model", "add", "fake", "--base-url", fake.url, "--key-env", "FAKE_KEY",
                 "--price-in", "1", "--price-out", "2", "--vision")
    assert enabled["ok"] and enabled["data"]["vision"] is True
    screenshot = f'''{PY} -c "import base64,pathlib; pathlib.Path('artifacts').mkdir(exist_ok=True); pathlib.Path('artifacts/proof.png').write_bytes(base64.b64decode('{png_b64}'))"'''
    fake.add("[VISION]", [
        {"call": "edit", "args": {"path": "calc.py", "old": "a - b", "new": "a + b"}},
        {"call": "run", "args": {"command": screenshot}},
        {"call": "run", "args": {"command": CHECK}},
        {"call": "finish", "args": {"summary": "fixed with visual evidence"}},
    ])
    tid = create(ak, "fix add [VISION]", "--attach", "reference.png")
    assert wait(ak, tid)["verdict"] == "pass"
    task = tjson(repo, tid)
    assert task["attachments"][0]["mime"] == "image/png"
    assert (repo / task["attachments"][0]["path"]).is_file()
    request = fake.requests_for("[VISION]")[0]["body"]["messages"]
    content = next(message["content"] for message in request if message["role"] == "user")
    assert any(part.get("type") == "image_url" for part in content)
    transcript = (repo / ".agentkit" / "tasks" / tid / "transcript.jsonl").read_text(encoding="utf-8")
    assert "data:image" not in transcript
    assert all(item["images"] == 1 for item in task["round_details"])
    report = ak("task", "report", tid, "--details", budget=4000)["data"]
    assert report["images"][0]["path"] == "artifacts/proof.png" and report["images"][0]["sha256"]
    receipt = json.loads((repo / ".agentkit" / "tasks" / tid / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["attachments"] and receipt["artifacts"][0]["path"] == "artifacts/proof.png"
    from agentkit.mcp_server import _meta, _state, handle

    response = handle(repo, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {**_meta("2025-11-25"), "name": "agentkit_task_report",
                                        "arguments": {"id": tid, "include_images": True}}}, _state())
    image_content = next(item for item in response["result"]["content"] if item["type"] == "image")
    assert base64.b64decode(image_content["data"]) == base64.b64decode(png_b64)


def test_dedupe_and_queue(ak, repo, fake):
    project(repo, ak, fake, cfg="[tasks]\nconcurrency = 1\n")
    fake.add("[SLOW1]", [{"call": "read", "args": {"spec": "calc.py"}, "before": [{"sleep": 2.5}]}] + FIX[1:])
    fake.add("[FAST2]", FIX)
    t1 = create(ak, "fix add [SLOW1]")
    again = ak("task", "create", "fix add [SLOW1]", "--check", CHECK)
    assert again["data"]["id"] == t1 and again["data"]["deduped"] is True
    assert again["data"]["reused"] is True and again["data"]["original_status"] in {"queued", "starting", "running"}
    t2 = create(ak, "fix add [FAST2]")
    s2 = ak("task", "status", t2)["data"]
    assert s2["status"] == "queued" and s2["pos"] == 1
    r = ak("task", "wait", t1, t2, "--timeout", "90")
    assert r["data"]["done"] and [t["verdict"] for t in r["data"]["tasks"]] == ["pass", "pass"]
    # a passed task is visibly reused unless the supervisor explicitly asks for a fresh run
    reused = ak("task", "create", "fix add [FAST2]", "--check", CHECK)["data"]
    assert reused["id"] == t2 and reused["deduped"] is True and reused["verdict"] == "pass"
    fresh = ak("task", "create", "fix add [FAST2]", "--check", CHECK, "--fresh")
    assert fresh["ok"] and fresh["data"]["id"] != t2 and fresh["data"]["deduped"] is False
    assert wait(ak, fresh["data"]["id"])["verdict"] == "pass"


def test_malformed_tool_call_repaired(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[REPAIR]", [
        {"call": "edit", "raw_args": "```json\n{'path': 'calc.py', 'old': 'a - b', 'new': 'a + b',}\n```"},
        {"call": "functions.finish", "args": {"summary": "done"}}])
    tid = create(ak, "fix add [REPAIR]")
    s = wait(ak, tid)
    assert s["verdict"] == "pass"
    assert tjson(repo, tid)["repairs"] >= 2


def test_three_malformed_calls_fail(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[BROKEN]", [{"call": "edit", "raw_args": "<<not json at all>>"}])
    tid = create(ak, "fix add [BROKEN]")
    s = wait(ak, tid)
    assert s["status"] == "failed" and "malformed" in s["why"]
    assert len(fake.requests_for("[BROKEN]")) == 3


def test_text_only_replies_fail(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[CHATTY]", [{"text": "I would fix it by changing the minus."}])
    tid = create(ak, "fix add [CHATTY]")
    s = wait(ak, tid)
    assert s["status"] == "failed" and "malformed" in s["why"]


def test_tool_call_in_text_is_repaired(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[INTEXT]", [{"text": 'Calling: {"name": "edit", "arguments": {"path": "calc.py", "old": "a - b", "new": "a + b"}}'},
                          {"call": "finish", "args": {"summary": "ok"}}])
    tid = create(ak, "fix add [INTEXT]")
    assert wait(ak, tid)["verdict"] == "pass"


def test_stuck_no_new_diff(ak, repo, fake):
    project(repo, ak, fake, cfg="stuck_rounds = 3\n")
    fake.add("[LOOP]", [{"call": "read", "args": {"spec": "calc.py"}}])
    tid = create(ak, "fix add [LOOP]")
    s = wait(ak, tid)
    assert s["status"] == "failed" and "no new diff" in s["why"]
    assert len(fake.requests_for("[LOOP]")) == 4  # the first read is new information; the 3 repeats count


def test_exploring_before_first_edit_is_not_stuck(ak, repo, fake):
    """NVIDIA NIM dogfood: the worker read new parts of the repo for 10 rounds before its first edit and was failed
    as stuck (failed is not resumable). Rounds that return results not seen before don't count until the first
    change; repeating the same calls still does (test_stuck_no_new_diff)."""
    project(repo, ak, fake, cfg="stuck_rounds = 3\n")
    (repo / "notes.txt").write_text("".join(f"line {i}\n" for i in range(40)))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "notes")
    explore = [{"call": "read", "args": {"spec": f"notes.txt:{i * 5 + 1}-{i * 5 + 5}"}} for i in range(6)]
    fake.add("[EXPLORE]", explore + FIX)
    tid = create(ak, "fix add [EXPLORE]")
    s = wait(ak, tid)
    assert s["verdict"] == "pass", s


def test_stuck_same_error(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[SAMEERR]", [{"call": "edit", "args": {"path": "calc.py", "old": "nonexistent text", "new": "x"}}])
    tid = create(ak, "fix add [SAMEERR]")
    s = wait(ak, tid)
    assert s["status"] == "failed" and "same error" in s["why"]


def test_context_compaction_keeps_goal(ak, repo, fake):
    project(repo, ak, fake, context=3000)
    (repo / "big.py").write_text("".join(f"VALUE_{i} = {i}  # filler line to make the file large\n" for i in range(300)))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "big")
    reads = [{"call": "read", "args": {"spec": f"big.py:{1 + 40 * i}-{40 + 40 * i}"}} for i in range(5)]
    fake.add("[COMPACT]", reads + FIX)
    tid = create(ak, "fix add [COMPACT]")
    s = wait(ak, tid)
    assert s["verdict"] == "pass"
    assert tjson(repo, tid)["compactions"] >= 1
    reqs = fake.requests_for("[COMPACT]")
    last = reqs[-1]["body"]["messages"]
    assert "fix add [COMPACT]" in last[0]["content"] and "PROJECT MEMORY" in last[0]["content"]
    assert any("[folded]" in str(m.get("content")) for m in last)
    est = (len(json.dumps(last)) + len(json.dumps(reqs[-1]["body"]["tools"]))) // 4
    assert est < 3000 * 0.85


def test_token_budget_stops(ak, repo, fake):
    project(repo, ak, fake)
    big = {"prompt_tokens": 2000, "completion_tokens": 10}
    fake.add("[TOKBUD]", [{"call": "read", "args": {"spec": "calc.py"}, "usage": big},
                          {"call": "read", "args": {"spec": "calc.py"}, "usage": big}])
    tid = create(ak, "fix add [TOKBUD]", "--budget-tokens", "3000")
    s = wait(ak, tid)
    assert s["status"] == "stopped" and s["stopped"] == "budget"
    rep = ak("task", "report", tid)["data"]
    assert rep["stopped"] == "budget" and "budget" in rep.get("why", "")
    assert len(fake.requests_for("[TOKBUD]")) <= 2


def test_usd_budget_stops(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[USDBUD]", [{"call": "read", "args": {"spec": "calc.py"}, "usage": {"prompt_tokens": 600000, "completion_tokens": 0}}])
    tid = create(ak, "fix add [USDBUD]", "--budget-usd", "0.5")
    s = wait(ak, tid)
    assert s["status"] == "stopped" and s["stopped"] == "budget"
    assert len(fake.requests_for("[USDBUD]")) == 1


def test_time_budget_stops_within_5s_during_model_call(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[TIMEBUD]", [{"call": "read", "args": {"spec": "calc.py"}, "before": [{"sleep": 40}]}])
    tid = create(ak, "fix add [TIMEBUD]", "--budget-minutes", "0.05")
    s = wait(ak, tid, timeout=40)
    assert s["status"] == "stopped" and s["stopped"] == "budget"
    t = tjson(repo, tid)
    assert t["stopped_at"] - (t["started"] + 3) < 5, "worker must stop within 5 s of the budget"


def test_time_budget_kills_running_command(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[CMDBUD]", [{"call": "run", "args": {"command": "sleep 60", "timeout": 120}}])
    tid = create(ak, "fix add [CMDBUD]", "--budget-minutes", "0.05")
    s = wait(ak, tid, timeout=40)
    assert s["stopped"] == "budget"
    t = tjson(repo, tid)
    assert t["stopped_at"] - (t["started"] + 3) < 5
    if Path("/proc").is_dir():
        leftovers = []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                cmd = (proc / "cmdline").read_bytes()
            except OSError:
                continue
            if b"sleep\x0060\x00" in cmd:
                leftovers.append(proc.name)
    else:
        out = subprocess.run(["pgrep", "-f", "sleep 60"], capture_output=True, text=True).stdout
        leftovers = [out.strip()] if out.strip() else []
    assert not leftovers, "the command's process group must be killed"


def test_rate_limit_pauses_and_reports(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[RL]", [{"call": "read", "args": {"spec": "calc.py"},
                       "before": [{"http": 429, "retry_after": 1}, {"http": 429, "retry_after": 1}]}])
    tid = create(ak, "fix add [RL]")
    s = wait(ak, tid)
    assert s["status"] == "paused" and "429" in s["why"]
    assert len(fake.requests_for("[RL]")) == 2, "honor Retry-After once, no bombardment"


def test_model_timeout_pauses_after_two_retries(ak, repo, fake):
    project(repo, ak, fake, model_cfg="timeout = 0.5")
    fake.add("[TO]", [{"call": "read", "args": {"spec": "calc.py"}, "before": [{"sleep": 1.5}] * 3}])
    tid = create(ak, "fix add [TO]")
    s = wait(ak, tid)
    assert s["status"] == "paused" and "timeout" in s["why"]
    assert len(fake.requests_for("[TO]")) == 3


def test_invalid_key_fails_immediately(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[AUTH]", [{"call": "read", "args": {"spec": "calc.py"}, "before": [{"http": 401}] * 3}])
    tid = create(ak, "fix add [AUTH]")
    s = wait(ak, tid)
    assert s["status"] == "failed" and "model test" in tjson(repo, tid)["reason"]
    assert len(fake.requests_for("[AUTH]")) == 1


def test_cancel_running_task(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[CANCEL]", [{"call": "read", "args": {"spec": "calc.py"}, "before": [{"sleep": 30}]}])
    tid = create(ak, "fix add [CANCEL]")
    for _ in range(50):
        if ak("task", "status", tid)["data"]["status"] == "running":
            break
        time.sleep(0.1)
    t0 = time.monotonic()
    r = ak("task", "cancel", tid)
    assert r["data"]["status"] == "cancelled" and time.monotonic() - t0 < 8


def test_crashed_worker_marked_interrupted_and_report_recovers(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[CRASH]", [{"call": "read", "args": {"spec": "calc.py"}, "before": [{"sleep": 30}]}])
    tid = create(ak, "fix add [CRASH]")
    pid = None
    for _ in range(50):
        t = tjson(repo, tid)
        if t["status"] == "running" and t.get("pid"):
            pid = t["pid"]
            break
        time.sleep(0.1)
    assert pid
    os.killpg(pid, signal.SIGKILL)
    time.sleep(0.5)
    s = ak("task", "status", tid)["data"]
    assert s["status"] == "interrupted"
    rep = ak("task", "report", tid)["data"]
    assert rep["status"] == "interrupted" and rep["verdict"] == "fail"


def test_keys_never_reach_model_logs_or_reports(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[LEAK]", [
        {"call": "run", "args": {"command": "env"}},
        {"call": "run", "args": {"command": "echo key=$FAKE_KEY; cat /proc/self/environ | tr '\\0' '\\n'"}},
        {"call": "run", "args": {"command": f"echo {KEY}"}},  # even if the value is typed literally
        {"call": "read", "args": {"spec": "../../config.toml"}},
    ] + FIX)
    tid = create(ak, "fix add [LEAK]")
    assert wait(ak, tid)["verdict"] == "pass"
    reqs = fake.requests_for("[LEAK]")
    assert reqs and all(r["headers"].get("Authorization") == "Bearer " + KEY for r in reqs)
    for r in fake.requests:
        assert KEY not in r["raw"], "key leaked into model context"
    assert "FAKE_KEY=" not in "".join(r["raw"] for r in reqs), "key env var must be scrubbed from tool processes"
    for p in (repo / ".agentkit").rglob("*"):
        if p.is_file():
            assert KEY.encode() not in p.read_bytes(), f"key leaked into {p}"
    assert KEY not in ak("task", "report", tid)["_raw"]


def test_auto_memory_records_results(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[MEMPASS]", FIX)
    fake.add("[MEMFAIL]", [{"call": "finish", "args": {"summary": "all good"}}])
    t1 = create(ak, "fix add [MEMPASS]")
    wait(ak, t1)
    t2 = create(ak, "fix add [MEMFAIL]")
    assert wait(ak, t2)["verdict"] == "fail"
    stats = ak("memory", "show", "model-stats")["data"]["body"]
    assert "fake | bugfix: 1 pass / 1 fail" in stats
    fails = ak("memory", "show", "task-failures")["data"]["body"]
    assert "check failed" in fails


def test_gemini_thought_signature_echoed(ak, repo, fake):
    project(repo, ak, fake)
    steps = [dict(s, sig="sig-abc") for s in FIX]
    fake.add("[SIG]", steps)
    tid = create(ak, "fix add [SIG]")
    assert wait(ak, tid)["verdict"] == "pass"
    later = fake.requests_for("[SIG]")[1]["body"]["messages"]
    asst = [m for m in later if m["role"] == "assistant"][0]
    assert asst["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "sig-abc"


def test_dirty_tree_warning_and_status_list(ak, repo, fake):
    project(repo, ak, fake)
    (repo / "calc.py").write_text("def add(a, b):\n    return a * b\n")
    fake.add("[DIRTY]", FIX)
    r = ak("task", "create", "fix add [DIRTY]", "--check", CHECK)
    assert any("uncommitted" in w for w in r["data"]["warnings"])
    wait(ak, r["data"]["id"])
    lst = ak("task", "status")["data"]
    assert lst["total"] == 1 and lst["tasks"][0]["id"] == r["data"]["id"]
