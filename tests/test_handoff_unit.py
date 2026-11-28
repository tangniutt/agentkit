# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Model handoff state transitions without starting a POSIX worker process."""
import json
from types import SimpleNamespace

import pytest

from agentkit import tasks
from agentkit.config import load_config
from agentkit.core import AKError, Ctx


def _paused(repo, monkeypatch):
    (repo / ".agentkit" / "tasks" / "t123abc").mkdir(parents=True)
    wt = repo / ".agentkit" / "worktrees" / "t123abc"
    wt.mkdir(parents=True)
    (repo / ".agentkit" / "config.toml").write_text(
        '[models.fake]\nbase_url = "http://127.0.0.1:1/v1"\nmodel = "fake"\nkey_env = "FAKE_KEY"\n'
        '[models.fake-b]\nbase_url = "http://127.0.0.1:1/v1"\nmodel = "fake-b"\nkey_env = "FAKE_KEY"\n'
        '[tasks]\nmax_handoffs = 1\n', encoding="utf-8")
    task = {
        "id": "t123abc", "goal": "fix", "checks": ["pytest -q"], "allow": [], "model": "fake", "tier": "low",
        "budget": {"tokens": 1000, "usd": 1.0, "minutes": 10, "rounds": 20}, "hash": "h", "base": "abc",
        "branch": "agentkit/t123abc", "worktree": ".agentkit/worktrees/t123abc", "status": "paused",
        "created": 1, "rounds": 3, "tokens": 400, "tokens_in": 350, "tokens_out": 50, "usd": 0.2,
        "reason": "rate limited", "error_code": "AK-MODEL-429", "acceptance": {"checks": ["pytest -q"], "baseline_must_fail": True},
        "acceptance_sha256": "a" * 64,
    }
    (repo / ".agentkit" / "tasks" / "t123abc" / "task.json").write_text(json.dumps(task), encoding="utf-8")
    monkeypatch.setattr(tasks, "schedule", lambda root: None)
    monkeypatch.setenv("FAKE_KEY", "fake-key-value")
    return Ctx(root=repo, cwd=repo, config=load_config(repo)), task, wt


def _args(model="fake-b"):
    return SimpleNamespace(ids=["t123abc"], model=model, budget_tokens=None, budget_usd=None, budget_minutes=None)


def test_handoff_preserves_state_and_records_history(repo, monkeypatch):
    ctx, before, wt = _paused(repo, monkeypatch)
    out = tasks.resume(ctx, _args(), handoff=True)
    assert out["handoff"] and out["from_model"] == "fake" and out["to_model"] == "fake-b"
    after = tasks.load(repo, "t123abc")
    assert after["status"] == "queued" and after["model"] == "fake-b" and after["handoffs"] == 1
    assert after["worktree"] == before["worktree"] and after["budget"] == before["budget"]
    assert after["prior"]["tokens_in"] == 350 and after["prior"]["rounds"] == 3
    assert after["model_history"][-1]["from"] == "fake"
    assert wt.exists()


def test_handoff_rejects_nonvision_target_for_attachment_task(repo, monkeypatch):
    ctx, task, _ = _paused(repo, monkeypatch)
    task["attachments"] = [{"path": ".agentkit/tasks/t123abc/attachments/ref.png",
                            "sha256": "d" * 64, "mime": "image/png", "bytes": 68}]
    tasks.save(repo, task)
    with pytest.raises(AKError, match="does not support image attachments"):
        tasks.resume(ctx, _args("fake-b"), handoff=True)


def test_handoff_limit_and_same_model(repo, monkeypatch):
    ctx, task, _ = _paused(repo, monkeypatch)
    with pytest.raises(AKError, match="already uses model"):
        tasks.resume(ctx, _args("fake"), handoff=True)
    task["handoffs"] = 1
    tasks.save(repo, task)
    with pytest.raises(AKError, match="max_handoffs"):
        tasks.resume(ctx, _args("fake-b"), handoff=True)
    assert tasks.max_handoffs({"tasks": {"max_handoffs": "bad"}}) == 2
