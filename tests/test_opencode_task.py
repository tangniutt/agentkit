# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""End-to-end OpenCode worker backend through task create/wait/verify."""
import json
import os
import sys

import pytest

from agentkit import sandbox
from conftest import git
from test_m2_tasks import CHECK, create, project, tjson, wait

pytestmark = pytest.mark.skipif(os.name != "posix", reason="dispatch mode requires Linux")


def _repo(repo, ak, script: str, *, allow_unsandboxed=True, prices=True, cost_known=False, sandbox_off=False,
          version="v1", allow_account_login=False):
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    fake = repo / "fake_opencode.py"
    fake.write_text(script, encoding="utf-8")
    cfg = repo / ".agentkit" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text(
        "[workers.opencode]\n"
        f"command = [{json.dumps(sys.executable)}, {json.dumps(str(fake))}]\n"
        "model = \"fake/model\"\n"
        f"version = {json.dumps(version)}\n"
        f"allow_account_login = {str(allow_account_login).lower()}\n"
        f"allow_unsandboxed = {str(allow_unsandboxed).lower()}\n"
        f"cost_known = {str(cost_known).lower()}\n"
        f"price_in = {1 if prices else 0}\n"
        f"price_out = {1 if prices else 0}\n"
        + ("[sandbox]\nmode = \"off\"\n" if sandbox_off else ""), encoding="utf-8")
    return cfg


SCRIPT = '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
prompt = sys.stdin.read()
print(json.dumps({"type":"test_invocation","args":args}))
p = Path("calc.py")
p.write_text(p.read_text().replace("a - b", "a + b"))
print(json.dumps({"type":"step_start","timestamp":1,"sessionID":"ses_1","part":{"id":"prt_1","sessionID":"ses_1","messageID":"msg_1","type":"step-start"}}))
if "[OPENCODE-FAIL]" in prompt:
    print(json.dumps({"type":"error","timestamp":2,"sessionID":"ses_1","error":{"name":"APIError","data":{"message":"429 rate limit"}}}))
    sys.exit(1)
print(json.dumps({"type":"text","timestamp":3,"sessionID":"ses_1","part":{"id":"prt_2","sessionID":"ses_1","messageID":"msg_1","type":"text","text":"fixed","time":{"start":1,"end":2}}}))
print(json.dumps({"type":"step_finish","timestamp":4,"sessionID":"ses_1","part":{"id":"prt_3","sessionID":"ses_1","messageID":"msg_1","type":"step-finish","reason":"stop","cost":0.25,"tokens":{"input":100,"output":20,"reasoning":7,"cache":{"read":11,"write":13}}}}))
'''


def test_opencode_task_end_to_end_without_agentkit_model(ak, repo):
    _repo(repo, ak, SCRIPT)
    created = ak("task", "create", "fix add [OPENCODE]", "--check", CHECK, "--backend", "opencode")
    assert created["ok"] and created["data"]["backend"] == "opencode", created
    tid = created["data"]["id"]
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["backend"] == "opencode" and task["model"] == "fake/model"
    assert task["tokens"] == 151 and task["usd"] == pytest.approx(0.25)
    assert task["backend_session"] == "ses_1"
    receipt = json.loads((repo / ".agentkit" / "tasks" / tid / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["worker"]["backend"] == "opencode"
    assert ak("task", "report", tid)["data"]["cost"]["usd_known"] is True


def test_opencode_v2_task_end_to_end_with_free_model_contract(ak, repo):
    _repo(repo, ak, SCRIPT, version="v2")
    created = ak("task", "create", "fix add [OPENCODE-V2]", "--check", CHECK, "--backend", "opencode")
    assert created["ok"], created
    tid = created["data"]["id"]
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["tokens"] == 151 and task["backend_session"] == "ses_1"


def test_opencode_budget_resume_preserves_session_usage_and_diff(ak, repo):
    _repo(repo, ak, SCRIPT)
    created = ak("task", "create", "fix add [OPENCODE-BUDGET]", "--check", CHECK,
                 "--backend", "opencode", "--budget-tokens", "150")
    assert created["ok"], created
    tid = created["data"]["id"]
    stopped = wait(ak, tid)
    assert stopped["status"] == "stopped" and stopped["stopped"] == "budget", stopped
    first = tjson(repo, tid)
    resumed = ak("task", "resume", tid, "--budget-tokens", "1000")
    assert resumed["ok"], resumed
    done = wait(ak, tid)
    assert done["status"] == "done" and done["verdict"] == "pass", done
    task = tjson(repo, tid)
    assert task["tokens"] == first["tokens"] + 151
    events = [json.loads(line) for line in
              (repo / ".agentkit" / "tasks" / tid / "opencode-events.jsonl").read_text(encoding="utf-8").splitlines()]
    calls = [event["args"] for event in events if event.get("type") == "test_invocation"]
    assert "--session" not in calls[0]
    assert calls[1][calls[1].index("--session") + 1] == "ses_1"


def test_opencode_unknown_cost_rejects_usd_budget(ak, repo):
    _repo(repo, ak, SCRIPT, prices=False)
    result = ak("task", "create", "fix add", "--check", CHECK, "--backend", "opencode", "--budget-usd", "1")
    assert not result["ok"] and result["error"]["code"] == "AK-USAGE"


def test_opencode_declared_free_model_allows_usd_budget(ak, repo):
    _repo(repo, ak, SCRIPT, prices=False, cost_known=True)
    result = ak("task", "create", "fix add [OPENCODE-FREE]", "--check", CHECK,
                "--backend", "opencode", "--budget-usd", "1")
    assert result["ok"], result
    assert wait(ak, result["data"]["id"])["verdict"] == "pass"


def test_opencode_requires_sandbox_unless_explicit_test_override(ak, repo):
    _repo(repo, ak, SCRIPT, allow_unsandboxed=False, sandbox_off=True)
    result = ak("task", "create", "fix add", "--check", CHECK, "--backend", "opencode")
    assert not result["ok"] and result["error"]["code"] == "AK-TASK-STATE"


def test_builtin_task_hands_off_to_opencode_in_same_worktree(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[TO-OPENCODE]", [{"call": "read", "args": {"spec": "calc.py"},
                                "before": [{"http": 429, "retry_after": 0.01}, {"http": 429, "retry_after": 0.01}]}])
    tid = create(ak, "fix add [TO-OPENCODE]", "--check", CHECK)
    paused = wait(ak, tid)
    assert paused["status"] == "paused", paused
    prior_tokens = tjson(repo, tid)["tokens"]
    script = repo / "fake_opencode.py"
    script.write_text(SCRIPT, encoding="utf-8")
    cfg = repo / ".agentkit" / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8") +
                   "\n[workers.opencode]\n" +
                   f"command = [{json.dumps(sys.executable)}, {json.dumps(str(script))}]\n" +
                   "model = \"fake/model\"\nallow_unsandboxed = true\nprice_in = 1\nprice_out = 1\n", encoding="utf-8")
    handed = ak("task", "handoff", tid, "--backend", "opencode")
    assert handed["ok"] and handed["data"]["to_backend"] == "opencode", handed
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["backend"] == "opencode" and task["backend_history"][-1]["to"] == "opencode"
    assert task["tokens"] >= prior_tokens + 151


def test_opencode_task_hands_off_to_codex(ak, repo):
    _repo(repo, ak, SCRIPT)
    tid = ak("task", "create", "fix add [OPENCODE-FAIL]", "--check", CHECK,
             "--backend", "opencode")["data"]["id"]
    assert wait(ak, tid)["status"] == "paused"
    codex = repo / "fake_codex.py"
    codex.write_text('''import json, sys
from pathlib import Path
sys.stdin.read()
print(json.dumps({"type":"thread.started","thread_id":"thread-1"}))
print(json.dumps({"type":"turn.started"}))
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"fixed"}}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":20}}))
''', encoding="utf-8")
    cfg = repo / ".agentkit" / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8") +
                   "\n[workers.codex]\n" +
                   f"command = [{json.dumps(sys.executable)}, {json.dumps(str(codex))}]\n" +
                   "model = \"fake-model\"\nallow_unsandboxed = true\nprice_in = 1\nprice_out = 1\n", encoding="utf-8")
    handed = ak("task", "handoff", tid, "--backend", "codex")
    assert handed["ok"] and handed["data"]["to_backend"] == "codex", handed
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["backend"] == "codex" and task["backend_history"][-1]["to"] == "codex"
    assert task["backend_session"] == "thread-1"


@pytest.mark.skipif(not sandbox.available(), reason="bubblewrap is unavailable")
def test_opencode_task_runs_inside_bubblewrap(ak, repo):
    _repo(repo, ak, SCRIPT, allow_unsandboxed=False)
    created = ak("task", "create", "fix add [OPENCODE-SANDBOX]", "--check", CHECK, "--backend", "opencode")
    assert created["ok"], created
    tid = created["data"]["id"]
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["sandbox"] == "bwrap" and task["sandbox_network"] == "shared"
