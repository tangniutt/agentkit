# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Goal controller state transitions without starting POSIX workers."""
import json
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


def _routing_config():
    return {"routing": {"profiles": {
        "cheap": {"model_version": "v1", "status": "active", "capabilities": {"code_edit": 0.9},
                  "evidence": {"sample_count": 10, "expected_cost_per_verified_success": 0.02},
                  "evaluated_at": "2026-11-28T00:00:00Z", "eval_config_sha256": "a" * 64},
        "fallback": {"model_version": "v1", "status": "active", "capabilities": {"code_edit": 0.9},
                     "evidence": {"sample_count": 10, "expected_cost_per_verified_success": 0.04},
                     "evaluated_at": "2026-11-28T00:00:00Z", "eval_config_sha256": "a" * 64},
    }}}


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


def test_goal_create_routes_initial_model_from_risk(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(tasks, "create", lambda ctx, a: calls.append(a) or {"id": "t123abc", "status": "queued"})
    spec = repo / "goal.json"
    spec.write_text(json.dumps({"outcome": "fix add", "acceptance": {"checks": ["pytest -q"]},
                                "budget": {"tasks": 1},
                                "constraints": {"risk": {"complexity": 1, "blast_radius": 1,
                                                         "required_capabilities": ["code_edit"]}}}), encoding="utf-8")
    out = goals.create(Ctx(root=repo, cwd=repo, config=_routing_config()), _args(input=str(spec)))
    assert calls[0].model == "cheap"
    assert goals.load(repo, out["id"])["routing"]["model_id"] == "cheap"


@pytest.mark.parametrize("error_code,expected_model", [("AK-VERIFY-FAIL", "fallback"), ("AK-MODEL-429", None)])
def test_goal_escalates_only_capability_failures(repo, monkeypatch, error_code, expected_model):
    risk = {"complexity": 1, "blast_radius": 1, "required_capabilities": ["code_edit"]}
    spec = {"outcome": "fix add", "acceptance": {"checks": ["pytest -q"]},
            "constraints": {"max_iterations": 3, "failover": [], "risk": risk}}
    _goal(repo, status="running", spec=spec)
    states = iter([
        {"id": "t123abc", "status": "stopped", "model": "cheap", "error_code": error_code},
        {"id": "t123abc", "status": "done", "model": expected_model or "cheap"},
    ])
    reports = iter([
        ({"verdict": "fail", "checks": [{"cmd": "pytest", "result": "fail", "why": "still broken"}]}, False),
        ({"verdict": "pass", "receipt": {"id": "er_1"}}, False),
    ])
    resumed = {}

    def resume(ctx, a, handoff=False):
        resumed.update(model=a.model, handoff=handoff)

    monkeypatch.setattr(tasks, "schedule", lambda root: None)
    monkeypatch.setattr(tasks, "load", lambda root, tid: next(states))
    monkeypatch.setattr(tasks, "report_view", lambda ctx, tid: next(reports))
    monkeypatch.setattr(tasks, "update", lambda root, tid, **fields: resumed.update(fields))
    monkeypatch.setattr(tasks, "resume", resume)
    out = goals.run(Ctx(root=repo, cwd=repo, config=_routing_config()),
                    SimpleNamespace(id="g123abc", timeout=5, budget_tokens=None, budget_usd=None, budget_minutes=None))
    assert out["status"] == "completed"
    assert resumed["model"] == expected_model
    assert resumed["handoff"] is (expected_model is not None)


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
