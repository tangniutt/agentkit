# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Task diff, steering, questions and supervisor decisions."""
import json

from agentkit import tasks
from agentkit.config import load_config
from agentkit.core import Ctx
from agentkit.models import ModelSpec
from agentkit.worker import Worker
from conftest import git


def _task(repo, **kw):
    tid = "t123abc"
    wt = repo / ".agentkit" / "worktrees" / tid
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("A = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "worktree", "add", "-q", "-b", f"agentkit/{tid}", str(wt), "HEAD")
    d = repo / ".agentkit" / "tasks" / tid
    d.mkdir(parents=True)
    task = {"id": tid, "goal": "fix", "checks": ["true"], "allow": [], "model": "fake", "tier": "low",
            "budget": {"tokens": 1000, "usd": 0, "minutes": 5}, "hash": "h", "base": "HEAD",
            "branch": f"agentkit/{tid}", "worktree": f".agentkit/worktrees/{tid}", "status": "paused",
            "created": 1, "rounds": 1, "tokens": 10, "usd": 0.0, "verdict": "pass"}
    task.update(kw)
    (d / "task.json").write_text(json.dumps(task), encoding="utf-8")
    (repo / ".agentkit" / "config.toml").write_text(
        '[models.fake]\nbase_url = "http://127.0.0.1:1/v1"\nmodel = "fake"\n', encoding="utf-8")
    return tid, wt


def test_task_diff_includes_tracked_and_untracked(repo):
    tid, wt = _task(repo)
    (wt / "a.py").write_text("A = 2\n", encoding="utf-8")
    (wt / "new.py").write_text("NEW = 1\n", encoding="utf-8")
    r = tasks.task_diff(Ctx(root=repo, cwd=repo), tid)
    text = "\n".join(r.data["diff"])
    assert "A = 2" in text and "new.py" in text and "NEW = 1" in text


def test_steer_updates_paused_task(repo):
    tid, _ = _task(repo)
    out = tasks.steer(repo, tid, "keep public API")
    assert out["steering"] == ["keep public API"]
    assert "STEER: keep public API" in tasks.load(repo, tid)["goal"]


def test_answer_clears_question_and_resumes(repo, monkeypatch):
    tid, _ = _task(repo, question={"question": "compatible?"}, waiting=True)
    monkeypatch.setattr(tasks, "schedule", lambda root: None)
    ctx = Ctx(root=repo, cwd=repo, config=load_config(repo))
    out = tasks.answer(ctx, tid, "yes")
    assert out["resumes"] == 1
    t = tasks.load(repo, tid)
    assert "question" not in t and "ANSWER: yes" in t["goal"] and t["answers"][0]["answer"] == "yes"


def test_accept_requires_pass_and_reject_records_reason(repo):
    tid, _ = _task(repo)
    accepted = tasks.decide(repo, tid, True)
    assert accepted["accepted"] and not accepted["merged"]
    rejected = tasks.decide(repo, tid, False, "wrong approach")
    assert rejected["rejected"] and tasks.load(repo, tid)["rejection_reason"] == "wrong approach"


class _AskClient:
    def chat(self, messages, tools=None, timeout=None, max_tokens=None):
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "ask1", "type": "function", "function": {"name": "ask", "arguments": json.dumps(
                {"question": "Use the old API?", "options": ["yes", "no"], "recommendation": "yes", "blocking": True})}}
        ]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def test_worker_ask_pauses_with_structured_question(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    spec = ModelSpec(name="fake", base_url="http://127.0.0.1:1/v1", model_id="fake")
    task = {"id": "t123abc", "goal": "fix", "checks": ["true"], "allow": [], "model": "fake", "budget": {}}
    out = Worker(spec, wt, tmp_path, task, client=_AskClient(), cfg={"backoff_s": 0}).run("")
    assert out.status == "paused" and out.code == "AK-TASK-WAITING"
    assert out.question["question"] == "Use the old API?" and out.question["options"] == ["yes", "no"]
