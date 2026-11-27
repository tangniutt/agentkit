# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""End-to-end Qoder worker backend through task create/wait/verify."""
import json
import os
import sys

import pytest

from agentkit import sandbox
from conftest import git
from test_m2_tasks import CHECK, create, project, tjson, wait

pytestmark = pytest.mark.skipif(os.name != "posix", reason="dispatch mode requires Linux")


def _repo(repo, ak, script: str, *, allow_unsandboxed=True, prices=True, cost_known=False, sandbox_off=False):
    ak.env["QODER_PERSONAL_ACCESS_TOKEN"] = "test-pat"
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    fake = repo / "fake_qoder.py"
    fake.write_text(script, encoding="utf-8")
    cfg = repo / ".agentkit" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text(
        "[workers.qoder]\n"
        f"command = [{json.dumps(sys.executable)}, {json.dumps(str(fake))}]\n"
        "model = \"fake-model\"\n"
        "config_dir = \"qoder-config\"\n"
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
print(json.dumps({"type":"system","subtype":"init","uuid":"u1","session_id":"ses_qoder","qodercli_version":"1.1.98","apiKeySource":"user","cwd":os.getcwd(),"model":"fake","permissionMode":"dont_ask","tools":["Read","Edit","Write","Grep","Glob"],"slash_commands":[],"output_style":"default","skills":[],"plugins":[],"mcp_servers":[]}))
if "[QODER-FAIL]" in prompt:
    print(json.dumps({"type":"result","subtype":"error_during_execution","uuid":"u2","session_id":"ses_qoder","is_error":True,"num_turns":1,"error_code":113,"errors":["quota exhausted"],"usage":{"input_tokens":0,"output_tokens":0},"modelUsage":{},"permission_denials":[]}))
    sys.exit(1)
print(json.dumps({"type":"assistant","uuid":"u3","session_id":"ses_qoder","parent_tool_use_id":None,"message":{"role":"assistant","content":[{"type":"text","text":"fixed"}],"usage":{"input_tokens":100,"output_tokens":20,"cache_read_input_tokens":11,"credits":1.5}}}))
print(json.dumps({"type":"result","subtype":"success","uuid":"u4","session_id":"ses_qoder","duration_ms":10,"duration_api_ms":8,"is_error":False,"num_turns":1,"result":"fixed","total_cost_usd":0.0,"usage":{"input_tokens":100,"output_tokens":20},"modelUsage":{},"permission_denials":[]}))
'''


def test_qoder_pat_is_redacted_from_event_log(ak, repo):
    secret = "qoder-pat-test-secret-123"
    script = '''import json, os, sys
from pathlib import Path
sys.stdin.read()
print(json.dumps({"type":"system","subtype":"init","uuid":"u1","session_id":"ses_qoder","qodercli_version":"1.1.98","apiKeySource":"user","cwd":os.getcwd(),"model":"fake","permissionMode":"dont_ask","tools":["Read","Edit","Write","Grep","Glob"],"slash_commands":[],"output_style":"default","skills":[],"plugins":[],"mcp_servers":[]}))
print(json.dumps({"type":"result","subtype":"error_during_execution","uuid":"u2","session_id":"ses_qoder","is_error":True,"num_turns":0,"error_code":113,"errors":[os.environ["QODER_PERSONAL_ACCESS_TOKEN"]],"usage":{"input_tokens":0,"output_tokens":0},"modelUsage":{},"permission_denials":[]}))
sys.exit(1)
'''
    _repo(repo, ak, script)
    ak.env["QODER_PERSONAL_ACCESS_TOKEN"] = secret
    tid = ak("task", "create", "fix add [QODER-SECRET]", "--check", CHECK, "--backend", "qoder")["data"]["id"]
    assert wait(ak, tid)["status"] == "paused"
    log = (repo / ".agentkit" / "tasks" / tid / "qoder-events.jsonl").read_text(encoding="utf-8")
    assert secret not in log and "[REDACTED]" in log


def test_doctor_reports_dispatch_with_only_qoder_config(ak, repo):
    _repo(repo, ak, SCRIPT)
    result = ak("doctor")
    assert result["ok"] and result["data"]["mode"] == "dispatch", result
    check = next(c for c in result["data"]["checks"] if c["name"] == "qoder_backend")
    assert check["ok"] is True


def test_qoder_task_end_to_end_without_agentkit_model(ak, repo):
    _repo(repo, ak, SCRIPT)
    created = ak("task", "create", "fix add [QODER]", "--check", CHECK, "--backend", "qoder")
    assert created["ok"] and created["data"]["backend"] == "qoder", created
    tid = created["data"]["id"]
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["backend"] == "qoder" and task["model"] == "fake-model"
    assert task["tokens"] == 120 and task["credits"] == pytest.approx(1.5)
    assert task["backend_session"] == "ses_qoder"
    receipt = json.loads((repo / ".agentkit" / "tasks" / tid / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["worker"]["backend"] == "qoder"
    report = ak("task", "report", tid)["data"]
    assert report["cost"]["credits"] == pytest.approx(1.5)


def test_qoder_budget_resume_preserves_session_usage_and_diff(ak, repo):
    _repo(repo, ak, SCRIPT)
    created = ak("task", "create", "fix add [QODER-BUDGET]", "--check", CHECK,
                 "--backend", "qoder", "--budget-tokens", "119")
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
    assert task["tokens"] == first["tokens"] + 120
    events = [json.loads(line) for line in
              (repo / ".agentkit" / "tasks" / tid / "qoder-events.jsonl").read_text(encoding="utf-8").splitlines()]
    calls = [event["args"] for event in events if event.get("type") == "test_invocation"]
    assert "--resume" not in calls[0]
    assert calls[1][calls[1].index("--resume") + 1] == "ses_qoder"


def test_qoder_unknown_cost_rejects_usd_budget(ak, repo):
    _repo(repo, ak, SCRIPT, prices=False)
    result = ak("task", "create", "fix add", "--check", CHECK, "--backend", "qoder", "--budget-usd", "1")
    assert not result["ok"] and result["error"]["code"] == "AK-USAGE"


def test_qoder_declared_free_model_allows_usd_budget(ak, repo):
    _repo(repo, ak, SCRIPT, prices=False, cost_known=True)
    result = ak("task", "create", "fix add [QODER-FREE]", "--check", CHECK,
                "--backend", "qoder", "--budget-usd", "1")
    assert result["ok"], result
    assert wait(ak, result["data"]["id"])["verdict"] == "pass"


def test_qoder_requires_sandbox_unless_explicit_test_override(ak, repo):
    _repo(repo, ak, SCRIPT, allow_unsandboxed=False, sandbox_off=True)
    result = ak("task", "create", "fix add", "--check", CHECK, "--backend", "qoder")
    assert not result["ok"] and result["error"]["code"] == "AK-TASK-STATE"


def test_builtin_task_hands_off_to_qoder_in_same_worktree(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[TO-QODER]", [{"call": "read", "args": {"spec": "calc.py"},
                             "before": [{"http": 429, "retry_after": 0.01}, {"http": 429, "retry_after": 0.01}]}])
    tid = create(ak, "fix add [TO-QODER]", "--check", CHECK)
    paused = wait(ak, tid)
    assert paused["status"] == "paused", paused
    prior_tokens = tjson(repo, tid)["tokens"]
    ak.env["QODER_PERSONAL_ACCESS_TOKEN"] = "test-pat"
    script = repo / "fake_qoder.py"
    script.write_text(SCRIPT, encoding="utf-8")
    cfg = repo / ".agentkit" / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8") +
                   "\n[workers.qoder]\n" +
                   f"command = [{json.dumps(sys.executable)}, {json.dumps(str(script))}]\n" +
                   "model = \"fake-model\"\n" +
                   "config_dir = \"qoder-config\"\n" +
                   "allow_unsandboxed = true\nprice_in = 1\nprice_out = 1\n", encoding="utf-8")
    handed = ak("task", "handoff", tid, "--backend", "qoder")
    assert handed["ok"] and handed["data"]["to_backend"] == "qoder", handed
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["backend"] == "qoder" and task["backend_history"][-1]["to"] == "qoder"
    assert task["tokens"] >= prior_tokens + 120


def test_qoder_task_hands_off_to_opencode(ak, repo):
    _repo(repo, ak, SCRIPT)
    tid = ak("task", "create", "fix add [QODER-FAIL]", "--check", CHECK,
             "--backend", "qoder")["data"]["id"]
    assert wait(ak, tid)["status"] == "paused"
    opencode = repo / "fake_opencode.py"
    opencode.write_text('''import json, sys
from pathlib import Path
sys.stdin.read()
print(json.dumps({"type":"step_start","timestamp":1,"sessionID":"ses_oc","part":{"id":"p1","type":"step-start"}}))
print(json.dumps({"type":"text","timestamp":2,"sessionID":"ses_oc","part":{"id":"p2","type":"text","text":"fixed"}}))
print(json.dumps({"type":"step_finish","timestamp":3,"sessionID":"ses_oc","part":{"id":"p3","type":"step-finish","reason":"stop","cost":0.25,"tokens":{"input":100,"output":20,"reasoning":0,"cache":{"read":0,"write":0}}}}))
''', encoding="utf-8")
    cfg = repo / ".agentkit" / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8") +
                   "\n[workers.opencode]\n" +
                   f"command = [{json.dumps(sys.executable)}, {json.dumps(str(opencode))}]\n" +
                   "model = \"fake/model\"\nallow_unsandboxed = true\nprice_in = 1\nprice_out = 1\n", encoding="utf-8")
    handed = ak("task", "handoff", tid, "--backend", "opencode")
    assert handed["ok"] and handed["data"]["to_backend"] == "opencode", handed
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["backend"] == "opencode" and task["backend_history"][-1]["to"] == "opencode"
    assert task["backend_session"] == "ses_oc"


@pytest.mark.skipif(not sandbox.available(), reason="bubblewrap is unavailable")
def test_qoder_task_runs_inside_bubblewrap(ak, repo):
    _repo(repo, ak, SCRIPT, allow_unsandboxed=False)
    created = ak("task", "create", "fix add [QODER-SANDBOX]", "--check", CHECK, "--backend", "qoder")
    assert created["ok"], created
    tid = created["data"]["id"]
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["sandbox"] == "bwrap" and task["sandbox_network"] == "shared"
