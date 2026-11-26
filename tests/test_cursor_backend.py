# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Cursor CLI stream-json adapter contract and process behavior."""
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from agentkit.backends import CursorBackend, cursor_available, cursor_settings


def _script(tmp_path: Path, mode: str = "ok") -> Path:
    path = tmp_path / "fake_cursor.py"
    path.write_text(f'''import json, os, sys
from pathlib import Path
mode = {mode!r}
args = sys.argv[1:]
prompt = args[-1]
capture = os.environ.get("AK_TEST_CAPTURE")
if capture:
    with open(capture, "a", encoding="utf-8") as f:
        f.write(json.dumps({{"args":args,"prompt":prompt,"home":os.environ.get("HOME"),
            "config":json.loads(Path(os.environ["HOME"], ".cursor", "cli-config.json").read_text())}})+"\\n")
if mode != "noedit":
    p=Path("calc.py"); p.write_text(p.read_text().replace("a - b", "a + b"))
source = "login" if mode == "policy" else "env"
if mode != "noinit":
    print(json.dumps({{"type":"system","subtype":"init","apiKeySource":source,"cwd":os.getcwd(),
        "session_id":"ses_cursor","model":"fake","permissionMode":"default","mcp_servers":[]}}))
if mode in ("fail", "auth"):
    message="quota exhausted" if mode=="fail" else "login expired"
    print(json.dumps({{"type":"result","subtype":"error_during_execution","is_error":True,
        "result":message,"session_id":"ses_cursor"}}))
    sys.exit(1)
print(json.dumps({{"type":"assistant","message":{{"role":"assistant","content":[{{"type":"text","text":"fixed"}}]}},
    "session_id":"ses_cursor"}}))
print(json.dumps({{"type":"tool_call","subtype":"completed","call_id":"c1",
    "tool_call":{{"writeToolCall":{{"args":{{"path":"calc.py"}},"result":{{"success":{{}}}}}}}},"session_id":"ses_cursor"}}))
if mode == "noresult":
    sys.exit(0)
print(json.dumps({{"type":"result","subtype":"success","is_error":False,"duration_ms":10,
    "result":"fixed","session_id":"ses_cursor"}}))
''', encoding="utf-8")
    return path


def _run(tmp_path: Path, *, mode="ok", budget=None, max_rounds=40, prior=None, session=""):
    work = tmp_path / "work"
    task_dir = tmp_path / "task"
    config = tmp_path / "cursor-config"
    work.mkdir(exist_ok=True)
    task_dir.mkdir(exist_ok=True)
    (work / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    cfg = {"workers": {"cursor": {"command": [sys.executable, str(_script(tmp_path, mode))],
                                  "model": "fake-model", "config_dir": str(config)}}}
    task = {"id": "t123abc", "goal": "fix add", "checks": ["pytest -q"], "allow": [],
            "model": "fake-model", "budget": budget or {"tokens": 0, "usd": 0, "minutes": 5},
            "prior": prior or {}, "backend_session": session}
    backend = CursorBackend(cfg, task, work, task_dir, max_rounds=max_rounds)
    return backend, backend.run("memory", None), work, task_dir, config


def test_cursor_stream_json_success_and_session(tmp_path):
    backend, outcome, work, task_dir, _config = _run(tmp_path)
    assert outcome.status == "submitted" and outcome.claim == "fixed"
    assert "a + b" in (work / "calc.py").read_text(encoding="utf-8")
    assert backend.session_id == "ses_cursor" and backend.rounds == 1
    assert backend.tokens_in == backend.tokens_out == backend.usd == 0
    assert backend.stats()["cost_known"] is False
    events = [json.loads(x) for x in (task_dir / "cursor-events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["type"] == "result"


def test_cursor_resume_preserves_prior_counters(tmp_path):
    backend, outcome, _work, _task_dir, _config = _run(
        tmp_path, prior={"tokens_in": 1000, "tokens_out": 500, "usd": 0.01, "rounds": 3}, session="ses_old")
    assert outcome.status == "submitted"
    assert (backend.tokens_in, backend.tokens_out, backend.usd, backend.rounds) == (1000, 500, 0.01, 4)


@pytest.mark.parametrize(("mode", "code", "status"), [
    ("fail", "AK-MODEL-429", "paused"),
    ("auth", "AK-MODEL-AUTH", "failed"),
])
def test_cursor_result_errors_map_to_agentkit_codes(tmp_path, mode, code, status):
    _backend, outcome, _work, _task_dir, _config = _run(tmp_path, mode=mode)
    assert outcome.status == status and outcome.code == code


def test_cursor_policy_is_verified_from_init(tmp_path):
    _backend, outcome, _work, _task_dir, _config = _run(tmp_path, mode="policy")
    assert outcome.status == "failed" and outcome.code == "AK-CONFIG"
    assert "policy verification failed" in outcome.reason


def test_cursor_success_without_init_is_rejected(tmp_path):
    _backend, outcome, _work, _task_dir, _config = _run(tmp_path, mode="noinit")
    assert outcome.status == "failed" and outcome.code == "AK-CONFIG"


def test_cursor_missing_result_is_not_success(tmp_path):
    _backend, outcome, _work, _task_dir, _config = _run(tmp_path, mode="noresult")
    assert outcome.status == "failed" and outcome.code == "AK-MODEL-BAD-RESPONSE"


def test_cursor_time_budget_stops_process(tmp_path):
    _backend, outcome, _work, _task_dir, _config = _run(tmp_path, budget={"tokens": 0, "usd": 0, "minutes": 0.00001})
    assert outcome.status == "stopped" and outcome.stopped == "budget"


def test_cursor_invocation_is_headless_private_and_resumable(tmp_path, monkeypatch):
    capture = tmp_path / "capture.jsonl"
    monkeypatch.setenv("AK_TEST_CAPTURE", str(capture))
    _backend, outcome, _work, _task_dir, config = _run(tmp_path, session="ses_old")
    assert outcome.status == "submitted"
    call = json.loads(capture.read_text(encoding="utf-8"))
    args = call["args"]
    assert "--print" in args and args[args.index("--output-format") + 1] == "stream-json"
    assert args[args.index("--workspace") + 1] == str(tmp_path / "work")
    assert args[args.index("--resume") + 1] == "ses_old"
    assert "--force" not in args and "--yolo" not in args
    assert call["home"] == str(config.resolve())
    assert "Do not commit or push" in call["prompt"]
    deny = call["config"]["permissions"]["deny"]
    assert {"Shell(*)", "WebFetch(*)", "Mcp(*:*)"} <= set(deny)


def test_cursor_command_and_policy_validation(tmp_path):
    assert cursor_available({"workers": {"cursor": {"command": [sys.executable]}}})
    assert not cursor_available({"workers": {"cursor": {"command": []}}})
    backend = CursorBackend({"workers": {"cursor": {"command": "   "}}}, {}, tmp_path, tmp_path)
    with pytest.raises(ValueError, match="argv"):
        backend.run("", None)
    assert "Read(**/.ssh/**)" in cursor_settings()["permissions"]["deny"]


def _fake_sdk(monkeypatch, *, usage=True, cost=True, close_error=False):
    module = ModuleType("cursor_sdk")
    calls = []

    class LocalAgentOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class SandboxOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class AgentOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Run:
        def __init__(self):
            self.id = "run_sdk"
            self.status = "finished"
            self.cancelled = False

        def messages(self):
            block = SimpleNamespace(type="text", text="fixed")
            yield SimpleNamespace(type="assistant", message=SimpleNamespace(content=[block]))
            yield SimpleNamespace(type="tool_call", name="edit", status="completed")
            if usage:
                yield SimpleNamespace(type="usage", usage=SimpleNamespace(input_tokens=100, output_tokens=20))

        def wait(self):
            return SimpleNamespace(status="finished", result="fixed",
                                   usage=SimpleNamespace(input_tokens=100, output_tokens=20) if usage else None)

        def cancel(self):
            self.cancelled = True
            self.status = "cancelled"

    class Agent:
        def __init__(self, agent_id):
            self.agent_id = agent_id

        @classmethod
        def create(cls, options):
            calls.append(("create", options, os.environ.get("HOME"), os.environ.get("CURSOR_API_KEY")))
            return cls("agent_sdk")

        @classmethod
        def resume(cls, agent_id, options):
            calls.append(("resume", agent_id, options, os.environ.get("HOME")))
            return cls(agent_id)

        def send(self, prompt):
            calls.append(("send", prompt))
            return Run()

        def get_usage(self, *, run_id):
            calls.append(("usage", run_id))
            if not cost:
                raise RuntimeError("usage access disabled")
            return SimpleNamespace(cost=SimpleNamespace(charged_cents=1234))

        def close(self):
            calls.append(("close",))
            if close_error:
                raise RuntimeError("close failed")

    module.Agent = Agent
    module.AgentOptions = AgentOptions
    module.LocalAgentOptions = LocalAgentOptions
    module.SandboxOptions = SandboxOptions
    monkeypatch.setitem(sys.modules, "cursor_sdk", module)
    return calls


def _run_sdk(tmp_path, monkeypatch, *, budget=None, prior=None, session="", usage=True, cost=True, close_error=False):
    calls = _fake_sdk(monkeypatch, usage=usage, cost=cost, close_error=close_error)
    work = tmp_path / "work"
    task_dir = tmp_path / "task"
    config = tmp_path / "cursor-config"
    work.mkdir(exist_ok=True)
    task_dir.mkdir(exist_ok=True)
    (work / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    monkeypatch.setenv("CURSOR_API_KEY", "sdk-test-key")
    cfg = {"workers": {"cursor": {"transport": "sdk", "model": "fake-model", "config_dir": str(config)}}}
    task = {"id": "t123abc", "goal": "fix add", "checks": ["pytest -q"], "allow": [],
            "model": "fake-model", "budget": budget or {"tokens": 0, "usd": 0, "minutes": 5},
            "prior": prior or {}, "backend_session": session, "backend_transport": "sdk"}
    backend = CursorBackend(cfg, task, work, task_dir)
    return backend, backend.run("memory", None), calls, config


def test_cursor_sdk_reports_usage_cost_and_restricted_tools(tmp_path, monkeypatch):
    backend, outcome, calls, config = _run_sdk(
        tmp_path, monkeypatch, budget={"tokens": 2000, "usd": 0, "minutes": 5},
        prior={"tokens_in": 1000, "tokens_out": 500, "usd": 0.01, "rounds": 3})
    assert outcome.status == "submitted", outcome
    assert backend.session_id == "agent_sdk" and backend.rounds == 4
    assert (backend.tokens_in, backend.tokens_out) == (1100, 520)
    assert backend.usd == pytest.approx(12.35) and backend.cost_known is True
    create = calls[0]
    assert create[0] == "create" and create[2] == str(config.resolve()) and create[3] == "sdk-test-key"
    options = create[1]
    assert options.tools == ["read", "edit", "write", "grep", "glob", "ls"]
    assert options.local.setting_sources == ["user"] and options.local.sandbox_options.enabled is False
    assert options.mcp_servers == {} and calls[-1] == ("close",)


def test_cursor_sdk_token_budget_stops_after_usage(tmp_path, monkeypatch):
    backend, outcome, _calls, _config = _run_sdk(
        tmp_path, monkeypatch, budget={"tokens": 119, "usd": 0, "minutes": 5})
    assert outcome.status == "stopped" and outcome.stopped == "budget"
    assert backend.tokens_in + backend.tokens_out == 120


def test_cursor_sdk_missing_usage_fails_token_budget(tmp_path, monkeypatch):
    _backend, outcome, _calls, _config = _run_sdk(
        tmp_path, monkeypatch, budget={"tokens": 100, "usd": 0, "minutes": 5}, usage=False)
    assert outcome.status == "failed" and outcome.code == "AK-CONFIG"


def test_cursor_sdk_resume_and_optional_cost_failure(tmp_path, monkeypatch):
    backend, outcome, calls, _config = _run_sdk(
        tmp_path, monkeypatch, session="agent_old", cost=False,
        prior={"tokens_in": 10, "tokens_out": 5, "usd": 0.5, "rounds": 1})
    assert outcome.status == "submitted"
    assert calls[0][0:2] == ("resume", "agent_old")
    assert backend.session_id == "agent_old" and backend.cost_known is False
    assert (backend.tokens_in, backend.tokens_out) == (110, 25)


def test_cursor_sdk_close_error_does_not_override_success(tmp_path, monkeypatch):
    _backend, outcome, _calls, _config = _run_sdk(tmp_path, monkeypatch, close_error=True)
    assert outcome.status == "submitted"


def test_cursor_sdk_requires_explicit_model(tmp_path):
    backend = CursorBackend({"workers": {"cursor": {"transport": "sdk"}}},
                            {"goal": "x", "checks": [], "budget": {"minutes": 5}}, tmp_path, tmp_path)
    outcome = backend.run("", None)
    assert outcome.status == "failed" and outcome.code == "AK-CONFIG"


def test_cursor_sdk_transport_validation(tmp_path):
    with pytest.raises(ValueError, match="transport"):
        CursorBackend({"workers": {"cursor": {"transport": "invalid"}}}, {}, tmp_path, tmp_path)
