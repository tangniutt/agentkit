# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""End-to-end Codex worker backend through task create/wait/verify."""
import json
import os
import sys

import pytest

from agentkit import credentials, sandbox
from conftest import git
from test_m2_tasks import CHECK, create, project, tjson, wait

pytestmark = pytest.mark.skipif(os.name != "posix", reason="dispatch mode requires Linux")


def _repo(repo, ak, script: str, *, allow_unsandboxed=True, prices=True, sandbox_off=False):
    ak.env["CODEX_TEST_KEY"] = "codex-test-key"
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    fake = repo / "fake_codex.py"
    fake.write_text(script, encoding="utf-8")
    cfg = repo / ".agentkit" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text(
        "[workers.codex]\n"
        f"command = [{json.dumps(sys.executable)}, {json.dumps(str(fake))}]\n"
        "model = \"fake-model\"\nkey_env = \"CODEX_TEST_KEY\"\n"
        f"allow_unsandboxed = {str(allow_unsandboxed).lower()}\n"
        f"price_in = {1 if prices else 0}\n"
        f"price_out = {1 if prices else 0}\n"
        + ("[sandbox]\nmode = \"off\"\n" if sandbox_off else ""), encoding="utf-8")
    return cfg


SCRIPT = '''import json, os, sys
from pathlib import Path
sys.stdin.read()
if "CODEX_TEST_KEY" in os.environ:
    assert os.environ["CODEX_TEST_KEY"] == "codex-test-key"
parent_env = Path(f"/proc/{os.getppid()}/environ").read_bytes()
assert not any(entry.startswith(b"CODEX_TEST_KEY=") for entry in parent_env.split(b"\\0"))
p = Path("calc.py")
p.write_text(p.read_text().replace("a - b", "a + b"))
print(json.dumps({"type":"thread.started","thread_id":"thread-1"}))
print(json.dumps({"type":"turn.started"}))
print(json.dumps({"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"fixed"}}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":20,"reasoning_output_tokens":0}}))
'''


def test_codex_task_end_to_end_without_agentkit_model(ak, repo):
    _repo(repo, ak, SCRIPT)
    created = ak("task", "create", "fix add [CODEX]", "--check", CHECK, "--backend", "codex")
    assert created["ok"] and created["data"]["backend"] == "codex", created
    tid = created["data"]["id"]
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["backend"] == "codex" and task["model"] == "fake-model"
    assert task["backend_capabilities"]["event_protocol"] == "jsonl"
    assert task["backend_capabilities"]["session_resume"] is False
    assert task["sandbox_status"]["state"] == "completed"
    assert task["sandbox_status"]["provider"] == "bubblewrap"
    assert task["sandbox_status"]["capabilities"]["snapshot_kinds"] == ["git", "files"]
    binding = credentials.load_binding(repo, task["credential_binding_id"])
    assert binding["status"] == "closed" and binding["session_id"] == "thread-1"
    audit = [json.loads(line) for line in (repo / ".agentkit/state/credentials/audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {"granted", "bound", "used", "closed"} <= {row["action"] for row in audit}
    assert task["tokens"] == 120 and task["cost_known"] is True
    receipt = json.loads((repo / ".agentkit" / "tasks" / tid / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["worker"]["backend"] == "codex"
    report = ak("task", "report", tid)["data"]
    assert report["cost"]["usd_known"] is True


def test_verifier_does_not_inherit_worker_api_key(ak, repo):
    _repo(repo, ak, SCRIPT)
    check = (f'"{sys.executable}" -c "import os; from calc import add; '
             "assert add(2, 3) == 5; assert 'CODEX_TEST_KEY' not in os.environ\"")
    created = ak("task", "create", "fix add [CODEX-NOKEY]", "--check", check, "--backend", "codex")
    assert created["ok"], created
    status = wait(ak, created["data"]["id"])
    assert status["status"] == "done" and status["verdict"] == "pass", status


def test_codex_budget_resume_preserves_usage_and_diff(ak, repo):
    _repo(repo, ak, SCRIPT)
    created = ak("task", "create", "fix add [CODEX-BUDGET]", "--check", CHECK,
                 "--backend", "codex", "--budget-tokens", "50")
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


def test_codex_unknown_cost_rejects_usd_budget(ak, repo):
    _repo(repo, ak, SCRIPT, prices=False)
    result = ak("task", "create", "fix add", "--check", CHECK, "--backend", "codex", "--budget-usd", "1")
    assert not result["ok"] and result["error"]["code"] == "AK-USAGE"


def test_codex_requires_sandbox_unless_explicit_test_override(ak, repo):
    _repo(repo, ak, SCRIPT, allow_unsandboxed=False, sandbox_off=True)
    result = ak("task", "create", "fix add", "--check", CHECK, "--backend", "codex")
    assert not result["ok"] and result["error"]["code"] == "AK-TASK-STATE"


def test_builtin_task_hands_off_to_codex_in_same_worktree(ak, repo, fake):
    project(repo, ak, fake)
    fake.add("[TO-CODEX]", [{"call": "read", "args": {"spec": "calc.py"},
                             "before": [{"http": 429, "retry_after": 0.01}, {"http": 429, "retry_after": 0.01}]}])
    tid = create(ak, "fix add [TO-CODEX]", "--check", CHECK)
    paused = wait(ak, tid)
    assert paused["status"] == "paused", paused
    prior_tokens = tjson(repo, tid)["tokens"]
    script = repo / "fake_codex.py"
    script.write_text(SCRIPT, encoding="utf-8")
    cfg = repo / ".agentkit" / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8") +
                   "\n[workers.codex]\n" +
                   f"command = [{json.dumps(sys.executable)}, {json.dumps(str(script))}]\n" +
                   "model = \"fake-model\"\nallow_unsandboxed = true\nprice_in = 1\nprice_out = 1\n", encoding="utf-8")
    handed = ak("task", "handoff", tid, "--backend", "codex")
    assert handed["ok"] and handed["data"]["to_backend"] == "codex", handed
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["backend"] == "codex" and task["backend_history"][-1]["to"] == "codex"
    assert task["tokens"] >= prior_tokens + 120


@pytest.mark.skipif(not sandbox.available(), reason="bubblewrap is unavailable")
def test_codex_task_runs_inside_bubblewrap(ak, repo):
    _repo(repo, ak, SCRIPT, allow_unsandboxed=False)
    created = ak("task", "create", "fix add [CODEX-SANDBOX]", "--check", CHECK, "--backend", "codex")
    assert created["ok"], created
    tid = created["data"]["id"]
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["sandbox"] == "bwrap" and task["sandbox_network"] == "shared"
