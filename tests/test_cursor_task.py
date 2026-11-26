# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""End-to-end Cursor worker backend through task create/wait/verify."""
import json
import os
import sys

import pytest

from agentkit import sandbox
from conftest import git
from test_m2_tasks import CHECK, tjson, wait

pytestmark = pytest.mark.skipif(os.name != "posix", reason="dispatch mode requires Linux")


SCRIPT = '''import json, os, sys
from pathlib import Path
prompt = sys.argv[-1]
p = Path("calc.py")
p.write_text(p.read_text().replace("a - b", "a + b"))
print(json.dumps({"type":"system","subtype":"init","apiKeySource":"env","cwd":os.getcwd(),
    "session_id":"ses_cursor","model":"fake","permissionMode":"default","mcp_servers":[]}))
if "[CURSOR-SECRET]" in prompt:
    print(json.dumps({"type":"result","subtype":"error_during_execution","is_error":True,
        "result":os.environ["CURSOR_API_KEY"],"session_id":"ses_cursor"}))
    sys.exit(1)
print(json.dumps({"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"fixed"}]},
    "session_id":"ses_cursor"}))
print(json.dumps({"type":"result","subtype":"success","is_error":False,"duration_ms":10,
    "result":"fixed","session_id":"ses_cursor"}))
'''


def _repo(repo, ak, script=SCRIPT, *, allow_unsandboxed=True, sandbox_off=False):
    ak.env["CURSOR_API_KEY"] = "cursor-test-key-123"
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    fake = repo / "fake_cursor.py"
    fake.write_text(script, encoding="utf-8")
    cfg = repo / ".agentkit" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text(
        "[workers.cursor]\n"
        f"command = [{json.dumps(sys.executable)}, {json.dumps(str(fake))}]\n"
        "model = \"fake-model\"\n"
        "key_env = \"CURSOR_API_KEY\"\n"
        f"config_dir = {json.dumps(str(repo / 'cursor-config'))}\n"
        f"allow_unsandboxed = {str(allow_unsandboxed).lower()}\n"
        + ("[sandbox]\nmode = \"off\"\n" if sandbox_off else ""), encoding="utf-8")
    return cfg


def test_cursor_task_end_to_end_without_agentkit_model(ak, repo):
    _repo(repo, ak)
    created = ak("task", "create", "fix add [CURSOR]", "--check", CHECK, "--backend", "cursor")
    assert created["ok"] and created["data"]["backend"] == "cursor", created
    tid = created["data"]["id"]
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["backend"] == "cursor" and task["model"] == "fake-model"
    assert task["tokens"] == 0 and task["backend_session"] == "ses_cursor"
    receipt = json.loads((repo / ".agentkit" / "tasks" / tid / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["worker"]["backend"] == "cursor"


def test_cursor_rejects_token_and_usd_budgets(ak, repo):
    _repo(repo, ak)
    tokens = ak("task", "create", "fix add", "--check", CHECK, "--backend", "cursor", "--budget-tokens", "100")
    assert not tokens["ok"] and tokens["error"]["code"] == "AK-USAGE"
    usd = ak("task", "create", "fix add", "--check", CHECK, "--backend", "cursor", "--budget-usd", "1")
    assert not usd["ok"] and usd["error"]["code"] == "AK-USAGE"


def test_cursor_requires_api_key(ak, repo):
    _repo(repo, ak)
    ak.env.pop("CURSOR_API_KEY")
    result = ak("task", "create", "fix add", "--check", CHECK, "--backend", "cursor")
    assert not result["ok"] and result["error"]["code"] == "AK-MODEL-KEYENV"


def test_cursor_key_is_redacted_from_event_log(ak, repo):
    _repo(repo, ak)
    tid = ak("task", "create", "fix add [CURSOR-SECRET]", "--check", CHECK,
             "--backend", "cursor")["data"]["id"]
    assert wait(ak, tid)["status"] == "failed"
    log = (repo / ".agentkit" / "tasks" / tid / "cursor-events.jsonl").read_text(encoding="utf-8")
    assert ak.env["CURSOR_API_KEY"] not in log and "[REDACTED]" in log


def test_doctor_reports_dispatch_with_only_cursor_config(ak, repo):
    _repo(repo, ak)
    result = ak("doctor")
    assert result["ok"] and result["data"]["mode"] == "dispatch", result
    check = next(c for c in result["data"]["checks"] if c["name"] == "cursor_backend")
    assert check["ok"] is True


def test_cursor_requires_sandbox_unless_explicit_test_override(ak, repo):
    _repo(repo, ak, allow_unsandboxed=False, sandbox_off=True)
    result = ak("task", "create", "fix add", "--check", CHECK, "--backend", "cursor")
    assert not result["ok"] and result["error"]["code"] == "AK-TASK-STATE"


def test_cursor_sdk_task_end_to_end_with_usage(ak, repo):
    _repo(repo, ak)
    package = repo / "fake_sdk" / "cursor_sdk"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('''import json, os
from pathlib import Path
from types import SimpleNamespace as NS
class LocalAgentOptions:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
class SandboxOptions:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
class AgentOptions:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
class Run:
    id = "run_sdk"
    status = "finished"
    def messages(self):
        yield NS(type="assistant", message=NS(content=[NS(type="text", text="fixed")]))
        yield NS(type="usage", usage=NS(input_tokens=100, output_tokens=20))
    def wait(self): return NS(status="finished", result="fixed", usage=NS(input_tokens=100, output_tokens=20))
    def cancel(self): self.status = "cancelled"
class Agent:
    agent_id = "agent_sdk"
    @classmethod
    def create(cls, options): return cls()
    @classmethod
    def resume(cls, agent_id, options): return cls()
    def send(self, prompt):
        p=Path("calc.py"); p.write_text(p.read_text().replace("a - b", "a + b")); return Run()
    def get_usage(self, *, run_id): return NS(cost=NS(charged_cents=25))
    def close(self): pass
''', encoding="utf-8")
    git(repo, "add", "fake_sdk")
    git(repo, "commit", "-qm", "add fake cursor sdk")
    cfg = repo / ".agentkit" / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace('model = "fake-model"',
                                                         'model = "fake-model"\ntransport = "sdk"'), encoding="utf-8")
    ak.env["PYTHONPATH"] = str(repo / "fake_sdk")
    created = ak("task", "create", "fix add [CURSOR-SDK]", "--check", CHECK,
                 "--backend", "cursor", "--budget-tokens", "150")
    assert created["ok"], created
    tid = created["data"]["id"]
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["backend_session"] == "agent_sdk" and task["backend_transport"] == "sdk"
    assert task["tokens"] == 120 and task["usd"] == pytest.approx(0.25)


@pytest.mark.skipif(not sandbox.available(), reason="bubblewrap is unavailable")
def test_cursor_task_runs_inside_bubblewrap(ak, repo):
    _repo(repo, ak, allow_unsandboxed=False)
    created = ak("task", "create", "fix add [CURSOR-SANDBOX]", "--check", CHECK, "--backend", "cursor")
    assert created["ok"], created
    tid = created["data"]["id"]
    status = wait(ak, tid)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    assert tjson(repo, tid)["sandbox"] == "bwrap"
