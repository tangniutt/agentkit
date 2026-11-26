# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Codex exec JSONL adapter contract and process behavior."""
import json
import sys
from pathlib import Path

import pytest

from agentkit.backends import CodexBackend, codex_available, codex_prices


def _script(tmp_path: Path, mode: str = "ok") -> Path:
    path = tmp_path / "fake_codex.py"
    path.write_text(f'''import json, sys
from pathlib import Path
mode = {mode!r}
prompt = sys.stdin.read()
if mode == "ok":
    p = Path("calc.py")
    p.write_text(p.read_text().replace("a - b", "a + b"))
print(json.dumps({{"type":"thread.started","thread_id":"thread-1"}}))
print(json.dumps({{"type":"turn.started"}}))
if mode == "fail":
    print(json.dumps({{"type":"turn.failed","error":{{"message":"429 rate limit"}}}}))
    sys.exit(1)
print(json.dumps({{"type":"item.completed","item":{{"id":"item-1","type":"command_execution","command":"edit calc.py","aggregated_output":"","exit_code":0,"status":"completed"}}}}))
print(json.dumps({{"type":"item.completed","item":{{"id":"item-2","type":"agent_message","text":"fixed"}}}}))
print(json.dumps({{"type":"turn.completed","usage":{{"input_tokens":100,"cached_input_tokens":10,"cache_write_input_tokens":5,"output_tokens":20,"reasoning_output_tokens":4}}}}))
''', encoding="utf-8")
    return path


def _run(tmp_path: Path, *, mode="ok", budget=None, max_rounds=40, prior=None):
    work = tmp_path / "work"
    task_dir = tmp_path / "task"
    work.mkdir()
    task_dir.mkdir()
    (work / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    script = _script(tmp_path, mode)
    cfg = {"workers": {"codex": {"command": [sys.executable, str(script)], "model": "fake-model",
                                   "price_in": 2, "price_out": 3}}}
    task = {"id": "t123abc", "goal": "fix add", "checks": ["pytest -q"], "allow": [],
            "model": "fake-model", "budget": budget or {"tokens": 0, "usd": 0, "minutes": 5},
            "prior": prior or {}}
    backend = CodexBackend(cfg, task, work, task_dir, max_rounds=max_rounds)
    return backend, backend.run("memory", None), work, task_dir


def test_codex_exec_jsonl_success_and_usage(tmp_path):
    backend, outcome, work, task_dir = _run(tmp_path)
    assert outcome.status == "submitted" and outcome.claim == "fixed"
    assert "a + b" in (work / "calc.py").read_text(encoding="utf-8")
    assert backend.thread_id == "thread-1" and backend.rounds == 1
    assert backend.tokens_in == 100 and backend.tokens_out == 20
    assert backend.usd == pytest.approx(0.00026) and backend.cost_known is True
    events = [json.loads(x) for x in (task_dir / "codex-events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["type"] == "turn.completed"


def test_codex_resume_usage_is_added_to_prior(tmp_path):
    backend, outcome, _work, _task_dir = _run(
        tmp_path, prior={"tokens_in": 1000, "tokens_out": 500, "usd": 0.01, "rounds": 3})
    assert outcome.status == "submitted"
    assert backend.tokens_in == 1100 and backend.tokens_out == 520
    assert backend.usd == pytest.approx(0.01026)


def test_codex_turn_failure_is_structured_and_resumable(tmp_path):
    backend, outcome, _work, _task_dir = _run(tmp_path, mode="fail")
    assert outcome.status == "paused" and outcome.code == "AK-MODEL-429"
    assert backend.completed is False


def test_codex_token_budget_stops_process(tmp_path):
    backend, outcome, _work, _task_dir = _run(tmp_path, budget={"tokens": 50, "usd": 0, "minutes": 5})
    assert outcome.status == "stopped" and outcome.stopped == "budget"
    assert backend.tokens_in + backend.tokens_out >= 50


def test_codex_command_validation(tmp_path):
    assert codex_available({"workers": {"codex": {"command": [sys.executable]}}})
    assert not codex_available({"workers": {"codex": {"command": []}}})
    backend = CodexBackend({"workers": {"codex": {"command": "   "}}}, {}, tmp_path, tmp_path)
    with pytest.raises(ValueError, match="argv"):
        backend.run("", None)
    with pytest.raises(ValueError, match="non-negative"):
        codex_prices({"workers": {"codex": {"price_in": "nope"}}})
    with pytest.raises(ValueError, match="non-negative"):
        codex_prices({"workers": {"codex": {"price_out": -1}}})
