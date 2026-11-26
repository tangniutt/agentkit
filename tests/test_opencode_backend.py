# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""OpenCode v1 run JSONL adapter contract and process behavior."""
import json
import sys
from pathlib import Path

import pytest

from agentkit.backends import (OpenCodeBackend, opencode_available, opencode_policy, opencode_prices,
                               opencode_state_paths)


def _script(tmp_path: Path, mode: str = "ok") -> Path:
    path = tmp_path / "fake_opencode.py"
    path.write_text(f'''import json, os, sys
from pathlib import Path
mode = {mode!r}
args = sys.argv[1:]
prompt = sys.stdin.read()
capture = os.environ.get("AK_TEST_CAPTURE")
v2_path = Path(os.environ["XDG_CONFIG_HOME"], "opencode", "opencode.json")
v2_config = json.loads(v2_path.read_text()) if v2_path.exists() else None
if capture:
    with open(capture, "a", encoding="utf-8") as f:
        f.write(json.dumps({{"args": args, "prompt": prompt,
            "disable_project": os.environ.get("OPENCODE_DISABLE_PROJECT_CONFIG"),
            "pure": os.environ.get("OPENCODE_PURE"),
            "permission": json.loads(os.environ.get("OPENCODE_PERMISSION") or "{{}}"),
            "config": json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{{}}"),
            "data_home": os.environ.get("XDG_DATA_HOME"), "db": os.environ.get("OPENCODE_DB"),
            "v2_config": v2_config}}) + "\\n")
p = Path("calc.py")
p.write_text(p.read_text().replace("a - b", "a + b"))
print(json.dumps({{"type":"tool_use","timestamp":1,"sessionID":"ses_1","part":{{"id":"prt_1","sessionID":"ses_1","messageID":"msg_1","tool":"edit","state":{{"status":"completed","input":{{"filePath":"calc.py","oldString":"a - b","newString":"a + b"}},"output":"ok","title":"edit calc.py","metadata":{{}},"time":{{"start":1,"end":2}}}}}}}}))
print(json.dumps({{"type":"step_start","timestamp":2,"sessionID":"ses_1","part":{{"id":"prt_2","sessionID":"ses_1","messageID":"msg_1","type":"step-start"}}}}))
if mode == "fail":
    print(json.dumps({{"type":"error","timestamp":3,"sessionID":"ses_1","error":{{"name":"APIError","data":{{"message":"429 rate limit"}}}}}}))
    sys.exit(1)
if mode == "free":
    print(json.dumps({{"type":"error","timestamp":3,"sessionID":"ses_1","error":{{"type":"provider.free-limit","message":"Free usage limit reached","status":429}}}}))
    sys.exit(1)
print(json.dumps({{"type":"text","timestamp":4,"sessionID":"ses_1","part":{{"id":"prt_3","sessionID":"ses_1","messageID":"msg_1","type":"text","text":"fixed","time":{{"start":2,"end":3}}}}}}))
print(json.dumps({{"type":"step_finish","timestamp":5,"sessionID":"ses_1","part":{{"id":"prt_4","sessionID":"ses_1","messageID":"msg_1","type":"step-finish","reason":"stop","cost":{0.25 if mode != "zero" else 0},"tokens":{{"input":100,"output":20,"reasoning":7,"cache":{{"read":11,"write":13}}}}}}}}))
if mode == "duplicate":
    print(json.dumps({{"type":"step_finish","timestamp":6,"sessionID":"ses_1","part":{{"id":"prt_4","sessionID":"ses_1","messageID":"msg_1","type":"step-finish","reason":"stop","cost":0.25,"tokens":{{"input":100,"output":20,"reasoning":7,"cache":{{"read":11,"write":13}}}}}}}}))
''', encoding="utf-8")
    return path


def _run(tmp_path: Path, *, mode="ok", budget=None, max_rounds=40, prior=None, session="", prices=(0, 0),
         cost_known=False, version="v1", allow_account_login=False):
    work = tmp_path / "work"
    task_dir = tmp_path / "task"
    work.mkdir(exist_ok=True)
    task_dir.mkdir(exist_ok=True)
    (work / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    script = _script(tmp_path, mode)
    cfg = {"workers": {"opencode": {"command": [sys.executable, str(script)], "model": "fake/model",
                                     "price_in": prices[0], "price_out": prices[1], "cost_known": cost_known,
                                     "version": version, "allow_account_login": allow_account_login}}}
    task = {"id": "t123abc", "goal": "fix add", "checks": ["pytest -q"], "allow": [],
            "model": "fake/model", "budget": budget or {"tokens": 0, "usd": 0, "minutes": 5},
            "prior": prior or {}, "backend_session": session}
    backend = OpenCodeBackend(cfg, task, work, task_dir, max_rounds=max_rounds)
    return backend, backend.run("memory", None), work, task_dir


def test_opencode_run_jsonl_success_and_usage(tmp_path):
    backend, outcome, work, task_dir = _run(tmp_path)
    assert outcome.status == "submitted" and outcome.claim == "fixed"
    assert "a + b" in (work / "calc.py").read_text(encoding="utf-8")
    assert backend.session_id == "ses_1" and backend.rounds == 1
    assert backend.tokens_in == 124 and backend.tokens_out == 27
    assert backend.usd == pytest.approx(0.25) and backend.cost_known is True
    events = [json.loads(x) for x in (task_dir / "opencode-events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["type"] == "step_finish"


def test_opencode_resume_uses_session_and_adds_prior_usage(tmp_path):
    backend, outcome, _work, _task_dir = _run(
        tmp_path, prior={"tokens_in": 1000, "tokens_out": 500, "usd": 0.01, "rounds": 3}, session="ses_old")
    assert outcome.status == "submitted"
    assert backend.tokens_in == 1124 and backend.tokens_out == 527
    assert backend.usd == pytest.approx(0.26) and backend.rounds == 4


def test_opencode_duplicate_step_finish_is_not_double_counted(tmp_path):
    backend, outcome, _work, _task_dir = _run(tmp_path, mode="duplicate")
    assert outcome.status == "submitted"
    assert backend.tokens_in == 124 and backend.tokens_out == 27 and backend.usd == pytest.approx(0.25)


def test_opencode_error_is_structured_and_resumable(tmp_path):
    backend, outcome, _work, _task_dir = _run(tmp_path, mode="fail")
    assert outcome.status == "paused" and outcome.code == "AK-MODEL-429"
    assert backend.completed is False


def test_opencode_token_budget_stops_process(tmp_path):
    backend, outcome, _work, _task_dir = _run(tmp_path, budget={"tokens": 150, "usd": 0, "minutes": 5})
    assert outcome.status == "stopped" and outcome.stopped == "budget"
    assert backend.tokens_in + backend.tokens_out == 151


def test_opencode_zero_cost_and_price_fallback(tmp_path):
    backend, outcome, _work, _task_dir = _run(tmp_path, mode="zero", prices=(2, 3))
    assert outcome.status == "submitted" and backend.cost_known is True
    assert backend.usd == pytest.approx((124 * 2 + 27 * 3) / 1_000_000)
    backend, outcome, _work, _task_dir = _run(tmp_path, mode="zero", cost_known=True)
    assert outcome.status == "submitted" and backend.cost_known is True


def test_opencode_policy_and_invocation_are_noninteractive(tmp_path, monkeypatch):
    capture = tmp_path / "capture.jsonl"
    monkeypatch.setenv("AK_TEST_CAPTURE", str(capture))
    backend, outcome, _work, _task_dir = _run(tmp_path)
    assert outcome.status == "submitted"
    call = json.loads(capture.read_text(encoding="utf-8"))
    assert call["args"][:2] == ["run", "--format"]
    assert "--auto" not in call["args"] and "--session" not in call["args"]
    assert call["disable_project"] == "1" and call["pure"] == "1"
    assert call["permission"]["bash"] == {"*": "deny"}
    assert call["permission"]["external_directory"] == "deny"
    assert call["permission"]["task"] == "deny"
    assert call["config"]["share"] == "disabled"
    assert "Do not commit or push" in call["prompt"]


def test_opencode_v2_invocation_policy_and_isolated_data(tmp_path, monkeypatch):
    capture = tmp_path / "capture.jsonl"
    monkeypatch.setenv("AK_TEST_CAPTURE", str(capture))
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "real.db"))
    backend, outcome, _work, task_dir = _run(tmp_path, version="v2")
    assert outcome.status == "submitted"
    call = json.loads(capture.read_text(encoding="utf-8"))
    args = call["args"]
    assert args[:4] == ["--standalone", "run", "--format", "json"]
    assert "--dir" not in args and "--auto" not in args
    assert call["db"] is None and call["data_home"] == str(task_dir / "opencode-data")
    config = call["v2_config"]
    assert "permission" not in config and "permissions" in config and "policies" in config
    denied = {(item["action"], item["resource"]) for item in config["policies"] if item["effect"] == "deny"}
    assert {("tool.use", "shell:*"), ("mcp.use", "*"), ("plugin.use", "*")} <= denied


def test_opencode_free_limit_is_resumable_rate_limit(tmp_path):
    _backend, outcome, _work, _task_dir = _run(tmp_path, mode="free", version="v2")
    assert outcome.status == "paused" and outcome.code == "AK-MODEL-429"


def test_opencode_account_login_requires_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert opencode_state_paths({}) == []
    paths = opencode_state_paths({"workers": {"opencode": {"allow_account_login": True}},
                                  "_user_security": {"allow_saved_credentials": True}})
    assert len(paths) == 3 and paths[0] == (tmp_path / "data" / "opencode").resolve()


def test_opencode_command_model_and_price_validation(tmp_path):
    assert opencode_available({"workers": {"opencode": {"command": [sys.executable]}}})
    assert not opencode_available({"workers": {"opencode": {"command": []}}})
    backend = OpenCodeBackend({"workers": {"opencode": {"command": "   "}}}, {}, tmp_path, tmp_path)
    with pytest.raises(ValueError, match="argv"):
        backend.run("", None)
    with pytest.raises(ValueError, match="non-negative"):
        opencode_prices({"workers": {"opencode": {"price_in": "nope"}}})
    with pytest.raises(ValueError, match="non-negative"):
        opencode_prices({"workers": {"opencode": {"price_out": -1}}})
    task = {"goal": "x", "model": "missing-slash"}
    backend = OpenCodeBackend({"workers": {"opencode": {"command": [sys.executable]}}}, task, tmp_path, tmp_path)
    with pytest.raises(ValueError, match="provider/model"):
        backend.run("", None)


def test_opencode_backend_ignores_project_login_opt_in_without_user_approval(tmp_path):
    cfg = {"workers": {"opencode": {"command": [sys.executable], "allow_account_login": True}}}
    backend = OpenCodeBackend(cfg, {}, tmp_path, tmp_path)
    assert backend.allow_account_login is False


def test_opencode_default_policy_denies_shell_secrets_and_writes_to_git():
    policy = opencode_policy()
    assert policy["bash"] == {"*": "deny"}
    assert policy["read"]["*.env"] == "deny"
    assert policy["read"]["*/.ssh/*"] == "deny"
    assert policy["edit"]["*/.git/*"] == "deny"
    assert policy["edit"]["*/.agentkit/*"] == "deny"
    assert opencode_policy(True)["bash"]["*"] == "allow"
