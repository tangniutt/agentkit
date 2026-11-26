# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Qoder CLI stream-json adapter contract and process behavior."""
import json
import sys
from pathlib import Path

import pytest

from agentkit.backends import QoderBackend, qoder_available, qoder_config_dir, qoder_prices, qoder_settings


def _script(tmp_path: Path, mode: str = "ok") -> Path:
    path = tmp_path / "fake_qoder.py"
    path.write_text(f'''import json, os, sys
from pathlib import Path
mode = {mode!r}
args = sys.argv[1:]
prompt = sys.stdin.read()
capture = os.environ.get("AK_TEST_CAPTURE")
if capture:
    with open(capture, "a", encoding="utf-8") as f:
        f.write(json.dumps({{"args": args, "prompt": prompt,
            "config_dir": os.environ.get("QODER_CONFIG_DIR"),
            "settings": json.loads(next(args[i + 1] for i, x in enumerate(args[:-1]) if x == "--settings"))}}) + "\\n")
if mode != "noedit":
    p = Path("calc.py")
    p.write_text(p.read_text().replace("a - b", "a + b"))
tools = ["Read", "Edit", "Write", "Grep", "Glob"]
if mode == "policy":
    tools.append("Bash")
if mode != "noinit":
    print(json.dumps({{"type":"system","subtype":"init","uuid":"u1","session_id":"ses_qoder","qodercli_version":"1.1.98","protocol_version":"2026-07-28","apiKeySource":"user","cwd":os.getcwd(),"model":"fake","permissionMode":"dont_ask","tools":tools,"slash_commands":[],"output_style":"default","skills":[],"plugins":[],"mcp_servers":[]}}))
if mode in ("fail", "auth", "max"):
    subtype = "error_max_turns" if mode == "max" else "error_during_execution"
    code = 113 if mode == "fail" else 105 if mode == "auth" else 47902
    message = "quota exhausted" if mode == "fail" else "login expired" if mode == "auth" else "max turns"
    print(json.dumps({{"type":"result","subtype":subtype,"uuid":"u2","session_id":"ses_qoder","is_error":True,"num_turns":1,"error_code":code,"errors":[message],"usage":{{"input_tokens":0,"output_tokens":0}},"modelUsage":{{}},"permission_denials":[]}}))
    sys.exit(1)
print(json.dumps({{"type":"assistant","uuid":"u3","session_id":"ses_qoder","parent_tool_use_id":None,"message":{{"role":"assistant","content":[{{"type":"text","text":"fixed"}},{{"type":"tool_use","id":"tool1","name":"Edit","input":{{"file_path":"calc.py"}}}}],"usage":{{"input_tokens":100,"output_tokens":20,"cache_read_input_tokens":11,"cache_creation_input_tokens":3,"credits":1.5,"original_credits":2.0,"billable":True}}}}}}))
if mode == "denied":
    print(json.dumps({{"type":"system","subtype":"permission_denied","tool_name":"Bash","tool_use_id":"tool2","message":"denied","decision_reason":"policy","uuid":"u4","session_id":"ses_qoder"}}))
print(json.dumps({{"type":"user","uuid":"u5","session_id":"ses_qoder","parent_tool_use_id":None,"message":{{"role":"user","content":[{{"type":"tool_result","tool_use_id":"tool1","content":"ok","is_error":False}}]}}}}))
if mode == "noresult":
    sys.exit(0)
print(json.dumps({{"type":"result","subtype":"success","uuid":"u6","session_id":"ses_qoder","duration_ms":10,"duration_api_ms":8,"is_error":False,"num_turns":1,"result":"fixed","total_cost_usd":0.0,"usage":{{"input_tokens":100,"output_tokens":20}},"modelUsage":{{}},"permission_denials":[]}}))
''', encoding="utf-8")
    return path


def _run(tmp_path: Path, *, mode="ok", budget=None, max_rounds=40, prior=None, session="", prices=(0, 0),
         cost_known=False):
    work = tmp_path / "work"
    task_dir = tmp_path / "task"
    work.mkdir(exist_ok=True)
    task_dir.mkdir(exist_ok=True)
    (work / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    script = _script(tmp_path, mode)
    cfg = {"workers": {"qoder": {"command": [sys.executable, str(script)], "model": "fake-model",
                                  "price_in": prices[0], "price_out": prices[1], "cost_known": cost_known}}}
    task = {"id": "t123abc", "goal": "fix add", "checks": ["pytest -q"], "allow": [],
            "model": "fake-model", "budget": budget or {"tokens": 0, "usd": 0, "minutes": 5},
            "prior": prior or {}, "backend_session": session}
    backend = QoderBackend(cfg, task, work, task_dir, max_rounds=max_rounds)
    return backend, backend.run("memory", None), work, task_dir


def test_qoder_stream_json_success_usage_and_credits(tmp_path):
    backend, outcome, work, task_dir = _run(tmp_path)
    assert outcome.status == "submitted" and outcome.claim == "fixed"
    assert "a + b" in (work / "calc.py").read_text(encoding="utf-8")
    assert backend.session_id == "ses_qoder" and backend.qodercli_version == "1.1.98"
    assert backend.rounds == 1 and backend.tokens_in == 100 and backend.tokens_out == 20
    assert backend.credits == pytest.approx(1.5) and backend.credits_known is True
    assert backend.cost_known is False
    events = [json.loads(x) for x in (task_dir / "qoder-events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["type"] == "result"


def test_qoder_resume_uses_session_and_adds_prior_usage(tmp_path):
    backend, outcome, _work, _task_dir = _run(
        tmp_path, prior={"tokens_in": 1000, "tokens_out": 500, "usd": 0.01, "rounds": 3, "credits": 4.0},
        session="ses_old")
    assert outcome.status == "submitted"
    assert backend.tokens_in == 1100 and backend.tokens_out == 520
    assert backend.credits == pytest.approx(5.5) and backend.rounds == 4


def test_qoder_token_budget_stops_process(tmp_path):
    backend, outcome, _work, _task_dir = _run(tmp_path, budget={"tokens": 119, "usd": 0, "minutes": 5})
    assert outcome.status == "stopped" and outcome.stopped == "budget"
    assert backend.tokens_in + backend.tokens_out == 120


@pytest.mark.parametrize("mode,code,status", [
    ("fail", "AK-MODEL-429", "paused"),
    ("auth", "AK-MODEL-AUTH", "failed"),
    ("max", "AK-WORKER-ROUNDS", "stopped"),
])
def test_qoder_result_errors_map_to_agentkit_codes(tmp_path, mode, code, status):
    backend, outcome, _work, _task_dir = _run(tmp_path, mode=mode)
    assert outcome.status == status and outcome.code == code
    if mode == "auth":
        assert backend.credits_known is False


def test_qoder_policy_is_verified_from_init(tmp_path):
    backend, outcome, _work, _task_dir = _run(tmp_path, mode="policy")
    assert outcome.status == "failed" and outcome.code == "AK-CONFIG"
    assert "policy verification failed" in outcome.reason


def test_qoder_success_without_init_is_rejected(tmp_path):
    backend, outcome, _work, _task_dir = _run(tmp_path, mode="noinit")
    assert outcome.status == "failed" and outcome.code == "AK-CONFIG"
    assert "system/init" in outcome.reason


def test_qoder_missing_result_is_not_success(tmp_path):
    backend, outcome, _work, _task_dir = _run(tmp_path, mode="noresult")
    assert outcome.status == "failed" and outcome.code == "AK-MODEL-BAD-RESPONSE"


def test_qoder_permission_denials_are_recorded(tmp_path):
    backend, outcome, _work, _task_dir = _run(tmp_path, mode="denied")
    assert outcome.status == "submitted" and backend.denied == ["Bash"]


def test_qoder_price_fallback_and_declared_zero_cost(tmp_path):
    backend, outcome, _work, _task_dir = _run(tmp_path, prices=(2, 3))
    assert outcome.status == "submitted" and backend.cost_known is True
    assert backend.usd == pytest.approx((100 * 2 + 20 * 3) / 1_000_000)
    backend, outcome, _work, _task_dir = _run(tmp_path, cost_known=True)
    assert outcome.status == "submitted" and backend.cost_known is True


def test_qoder_invocation_is_noninteractive_and_private(tmp_path, monkeypatch):
    capture = tmp_path / "capture.jsonl"
    monkeypatch.setenv("AK_TEST_CAPTURE", str(capture))
    backend, outcome, _work, _task_dir = _run(tmp_path)
    assert outcome.status == "submitted"
    call = json.loads(capture.read_text(encoding="utf-8"))
    args = call["args"]
    assert "--print" in args and args[args.index("--output-format") + 1] == "stream-json"
    assert args[args.index("--permission-mode") + 1] == "dont_ask"
    assert args[args.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in args and "--resume" not in args
    assert args[args.index("--tools") + 1] == "Read,Edit,Write,Grep,Glob"
    settings = call["settings"]
    assert settings["privacy"]["usageStatisticsEnabled"] is False
    assert settings["aiCodeStatistics"]["enabled"] is False
    assert "Read(~/.qoder/**)" in settings["permissions"]["deny"]
    assert "Read(~/.config/agentkit/**)" in settings["permissions"]["deny"]
    assert "Edit(~/.config/agentkit/**)" in settings["permissions"]["deny"]
    assert "Edit(~/.qoder/**)" in settings["permissions"]["deny"]
    assert "Do not commit or push" in call["prompt"]


def test_qoder_defaults_to_dedicated_config_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("QODER_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert qoder_config_dir({}, tmp_path) == (tmp_path / "config" / "agentkit" / "qoder").resolve()
    monkeypatch.setenv("QODER_CONFIG_DIR", str(tmp_path / "explicit"))
    assert qoder_config_dir({}, tmp_path) == (tmp_path / "explicit").resolve()


def test_qoder_command_and_price_validation(tmp_path):
    assert qoder_available({"workers": {"qoder": {"command": [sys.executable]}}})
    assert not qoder_available({"workers": {"qoder": {"command": []}}})
    backend = QoderBackend({"workers": {"qoder": {"command": "   "}}}, {}, tmp_path, tmp_path)
    with pytest.raises(ValueError, match="argv"):
        backend.run("", None)
    with pytest.raises(ValueError, match="non-negative"):
        qoder_prices({"workers": {"qoder": {"price_in": "nope"}}})
    with pytest.raises(ValueError, match="non-negative"):
        qoder_prices({"workers": {"qoder": {"price_out": -1}}})


def test_qoder_settings_disable_shell_network_subagents_and_secret_reads():
    settings = qoder_settings()
    assert settings["tools"]["core"] == ["Read", "Edit", "Write", "Grep", "Glob"]
    assert settings["general"]["defaultPermissionMode"] == "dont_ask"
    assert settings["mcpServers"] == {}
    assert "Bash" in settings["permissions"]["deny"]
    assert "Read(~/.qoder/**)" in settings["permissions"]["deny"]
    assert "Read(~/.config/agentkit/**)" in settings["permissions"]["deny"]
    assert "Edit(~/.config/agentkit/**)" in settings["permissions"]["deny"]
    assert "Edit(~/.qoder/**)" in settings["permissions"]["deny"]
