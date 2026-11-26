# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Goal controller state transitions without starting POSIX workers."""
from types import SimpleNamespace

import pytest

from agentkit import goals, tasks
from agentkit.core import AKError, Ctx


def _args(**kw):
    base = dict(outcome="fix add", check=["pytest -q"], non_goal=[], allow=[], allow_baseline_pass=False,
                budget_tokens=1000, budget_usd=1.0, budget_minutes=10, max_rounds=20,
                max_iterations=3, max_handoffs=2, failover=[], model=None, tier=None, input=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _goal(repo, **kw):
    g = {"id": "g123abc", "spec": {"outcome": "fix add", "acceptance": {"checks": ["pytest -q"]},
                                    "constraints": {"max_iterations": 3, "failover": []}},
         "task_id": "t123abc", "status": "planned", "iterations": 0, "max_iterations": 3,
         "created": 1, "history": [], "verdict": None, "receipt": None}
    g.update(kw)
    goals.save(repo, g)
    return g


def test_goal_create_registers_one_underlying_task(repo, monkeypatch):
    calls = []

    def create(ctx, a):
        calls.append(a)
        return {"id": "t123abc", "status": "queued"}

    monkeypatch.setattr(tasks, "create", create)
    out = goals.create(Ctx(root=repo, cwd=repo), _args())
    assert out["task_id"] == "t123abc" and len(calls) == 1
    g = goals.load(repo, out["id"])
    assert g["status"] == "planned" and g["spec"]["acceptance"]["baseline_must_fail"] is True


def test_goal_rejects_multi_task_spec(repo):
    spec = repo / "goal.json"
    spec.write_text('{"outcome":"x","acceptance":{"checks":["true"]},"constraints":{"max_tasks":2}}', encoding="utf-8")
    with pytest.raises(AKError, match="one underlying task"):
        goals.create(Ctx(root=repo, cwd=repo), _args(input=str(spec)))


def test_goal_completes_only_on_verified_pass(repo, monkeypatch):
    _goal(repo)
    monkeypatch.setattr(tasks, "schedule", lambda root: None)
    monkeypatch.setattr(tasks, "load", lambda root, tid: {"id": tid, "status": "done", "model": "fake"})
    monkeypatch.setattr(tasks, "report_view", lambda ctx, tid: ({"verdict": "pass", "receipt": {"id": "er_1"}}, False))
    out = goals.run(Ctx(root=repo, cwd=repo), SimpleNamespace(id="g123abc", timeout=5, budget_tokens=None,
                                                              budget_usd=None, budget_minutes=None))
    assert out["status"] == "completed" and out["verdict"] == "pass"
    assert goals.load(repo, "g123abc")["receipt"] == {"id": "er_1"}


def test_goal_retries_with_verifier_feedback(repo, monkeypatch):
    _goal(repo, status="running")
    states = iter([
        {"id": "t123abc", "status": "stopped", "stopped": "rounds", "model": "fake"},
        {"id": "t123abc", "status": "done", "model": "fake"},
    ])
    reports = iter([
        ({"verdict": "fail", "checks": [{"cmd": "pytest", "result": "fail", "why": "assert 1 != 2"}]}, False),
        ({"verdict": "pass", "receipt": {"id": "er_2"}}, False),
    ])
    resumed = {}

    def resume(ctx, a, handoff=False):
        resumed.update({"id": a.ids[0], "handoff": handoff})

    monkeypatch.setattr(tasks, "schedule", lambda root: None)
    monkeypatch.setattr(tasks, "load", lambda root, tid: next(states))
    monkeypatch.setattr(tasks, "report_view", lambda ctx, tid: next(reports))
    monkeypatch.setattr(tasks, "update", lambda root, tid, **fields: resumed.update(fields))
    monkeypatch.setattr(tasks, "resume", resume)
    out = goals.run(Ctx(root=repo, cwd=repo), SimpleNamespace(id="g123abc", timeout=5, budget_tokens=None,
                                                              budget_usd=None, budget_minutes=None))
    assert out["status"] == "completed" and out["iterations"] == 1
    assert resumed["reason"].startswith("pytest") and resumed["id"] == "t123abc"


def test_goal_does_not_resume_finished_failed_task(repo, monkeypatch):
    _goal(repo, status="running")
    monkeypatch.setattr(tasks, "schedule", lambda root: None)
    monkeypatch.setattr(tasks, "load", lambda root, tid: {"id": tid, "status": "done", "model": "fake"})
    monkeypatch.setattr(tasks, "report_view", lambda ctx, tid: ({"verdict": "fail", "why": "still broken"}, False))
    out = goals.run(Ctx(root=repo, cwd=repo), SimpleNamespace(id="g123abc", timeout=5, budget_tokens=None,
                                                              budget_usd=None, budget_minutes=None))
    assert out["status"] == "needs_review" and "not resumable" in out["reason"]
