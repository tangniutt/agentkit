# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Bounded Goal controller built on tasks, worktrees and independent verification."""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from types import SimpleNamespace

from . import tasks
from .core import AKError, Ctx, Result, atomic_write
from .protocol import GoalSpec, ProtocolError

TERMINAL = {"completed", "needs_review", "failed", "cancelled"}
_ID_RE = re.compile(r"g[0-9a-f]{6,12}\Z")


def _dir(root: Path) -> Path:
    p = root / ".agentkit" / "goals"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(root: Path, gid: str) -> Path:
    if not isinstance(gid, str) or not _ID_RE.fullmatch(gid):
        raise AKError(f"invalid goal id '{str(gid)[:40]}'", "ids look like g1a2b3c", "agentkit goal status", code="AK-TASK-UNKNOWN")
    return _dir(root) / gid / "goal.json"


def load(root: Path, gid: str) -> dict:
    try:
        return json.loads(_path(root, gid).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise AKError(f"unknown goal '{gid}'", "no such goal in this repo", "agentkit goal status", code="AK-TASK-UNKNOWN") from e


def save(root: Path, g: dict) -> None:
    g["updated"] = time.time()
    p = _path(root, g["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(p, json.dumps(g, ensure_ascii=False, indent=1).encode("utf-8"))


def _spec_from_args(a) -> GoalSpec:
    if getattr(a, "input", None):
        try:
            raw = Path(a.input).read_text(encoding="utf-8")
            return GoalSpec.from_dict(json.loads(raw))
        except (OSError, ValueError, ProtocolError) as e:
            raise AKError("invalid goal spec", str(e), "pass a GoalSpec JSON file with outcome and acceptance.checks", code="AK-USAGE")
    checks = list(a.check or [])
    if not checks:
        raise AKError("goal needs acceptance checks", "Goal requires at least one --check", "agentkit goal create 'outcome' --check 'pytest -q'", code="AK-VERIFY-NOCHECK")
    constraints = {"max_tasks": 1, "max_handoffs": getattr(a, "max_handoffs", 2),
                   "max_iterations": getattr(a, "max_iterations", 3), "allowed_paths": list(a.allow or []),
                   "failover": list(a.failover or [])}
    return GoalSpec.from_dict({
        "outcome": a.outcome,
        "non_goals": list(a.non_goal or []),
        "acceptance": {"checks": checks, "baseline_must_fail": not bool(a.allow_baseline_pass), "frozen": True},
        "budget": {"tokens": a.budget_tokens or 200000, "usd": a.budget_usd or 2.0,
                   "minutes": a.budget_minutes or 30, "rounds": a.max_rounds or 40, "tasks": 1},
        "constraints": constraints,
        "planning": {"mode": "supervisor", "approval_required": False},
    })


def create(ctx: Ctx, a) -> dict:
    spec = _spec_from_args(a).to_dict()
    constraints = spec.get("constraints") or {}
    try:
        max_iterations = int(constraints.get("max_iterations", 3))
        max_tasks = int(constraints.get("max_tasks", 1))
    except (TypeError, ValueError) as e:
        raise AKError("invalid Goal constraints", str(e), None, code="AK-USAGE")
    if not 1 <= max_iterations <= 20:
        raise AKError("invalid max_iterations", "Goal allows 1–20 verification iterations", None, code="AK-USAGE")
    if max_tasks != 1:
        raise AKError("Goal v1 supports one underlying task", "set constraints.max_tasks=1; task DAGs are planned for a later version", None, code="AK-USAGE")
    budget = spec.get("budget") or {}
    if int(budget.get("tasks", 1)) != 1:
        raise AKError("Goal v1 supports one underlying task", "set budget.tasks=1", None, code="AK-USAGE")
    task_args = SimpleNamespace(
        goal=spec["outcome"], check=spec["acceptance"]["checks"], allow=constraints.get("allowed_paths", []),
        allow_baseline_pass=not spec["acceptance"].get("baseline_must_fail", True),
        budget_tokens=int(budget.get("tokens", 200000)), budget_usd=float(budget.get("usd", 2.0)),
        budget_minutes=float(budget.get("minutes", 30)), tier=getattr(a, "tier", None), model=getattr(a, "model", None),
    )
    task = tasks.create(ctx, task_args)
    gid = "g" + secrets.token_hex(3)
    g = {
        "id": gid, "spec": spec, "task_id": task["id"], "status": "planned", "iterations": 0,
        "max_iterations": max_iterations, "created": time.time(), "history": [],
        "verdict": None, "receipt": None,
    }
    save(ctx.root, g)
    return {"id": gid, "task_id": task["id"], "status": g["status"], "max_iterations": g["max_iterations"]}


def _wait_task(ctx: Ctx, tid: str, deadline: float) -> dict:
    while True:
        tasks.schedule(ctx.root)
        t = tasks.load(ctx.root, tid)
        if t["status"] in tasks.TERMINAL or time.monotonic() >= deadline:
            return t
        time.sleep(0.2)


def _feedback(rep: dict) -> str:
    failed = [c for c in rep.get("checks") or [] if c.get("result") != "pass"]
    if failed:
        return "; ".join(f"{c.get('cmd')}: {c.get('why') or c.get('result')}" for c in failed[:3])
    return str(rep.get("why") or rep.get("verdict") or "verification failed")[:300]


def run(ctx: Ctx, a) -> dict:
    g = load(ctx.root, a.id)
    if g["status"] in TERMINAL:
        return {"id": g["id"], "status": g["status"], "verdict": g.get("verdict")}
    timeout = float(getattr(a, "timeout", 600))
    deadline = time.monotonic() + timeout
    g["status"] = "running"
    save(ctx.root, g)
    while time.monotonic() < deadline:
        t = _wait_task(ctx, g["task_id"], deadline)
        if t["status"] not in tasks.TERMINAL:
            save(ctx.root, g)
            return {"id": g["id"], "status": "running", "task_id": g["task_id"], "iteration": g["iterations"]}
        rep, _trunc = tasks.report_view(ctx, g["task_id"])
        g["history"].append({"iteration": g["iterations"], "task_status": t["status"], "verdict": rep.get("verdict"),
                             "cost": rep.get("cost"), "at": time.time()})
        if rep.get("verdict") == "pass":
            g.update(status="completed", verdict="pass", receipt=rep.get("receipt"), ended=time.time())
            save(ctx.root, g)
            return {"id": g["id"], "status": "completed", "verdict": "pass", "task_id": g["task_id"],
                    "iterations": g["iterations"], "receipt": rep.get("receipt")}
        if t.get("stopped") == "budget":
            g.update(status="needs_review", verdict="needs_review", reason="task budget exhausted", ended=time.time())
            save(ctx.root, g)
            return {"id": g["id"], "status": "needs_review", "reason": g["reason"], "task_id": g["task_id"]}
        if t["status"] == "cancelled":
            g.update(status="cancelled", verdict="cancelled", reason="task cancelled", ended=time.time())
            save(ctx.root, g)
            return {"id": g["id"], "status": "cancelled", "task_id": g["task_id"]}
        if t.get("question"):
            g.update(status="needs_review", verdict="needs_review", reason="worker is waiting for a supervisor answer",
                     question=t["question"], ended=time.time())
            save(ctx.root, g)
            return {"id": g["id"], "status": "needs_review", "reason": g["reason"],
                    "question": t["question"], "task_id": g["task_id"]}
        if g["iterations"] >= g["max_iterations"]:
            g.update(status="needs_review", verdict=rep.get("verdict") or "needs_review",
                     reason=f"max_iterations={g['max_iterations']} reached", ended=time.time())
            save(ctx.root, g)
            return {"id": g["id"], "status": "needs_review", "reason": g["reason"], "task_id": g["task_id"]}
        if t["status"] not in tasks.RESUMABLE:
            g.update(status="needs_review", verdict=rep.get("verdict") or "needs_review",
                     reason="underlying task is not resumable; create a new goal", ended=time.time())
            save(ctx.root, g)
            return {"id": g["id"], "status": "needs_review", "reason": g["reason"], "task_id": g["task_id"]}
        feedback = _feedback(rep)
        tasks.update(ctx.root, g["task_id"], reason=feedback)
        constraints = g["spec"].get("constraints") or {}
        failover = list(constraints.get("failover") or [])
        next_model = None
        if int(t.get("handoffs") or 0) < int(constraints.get("max_handoffs", 2)):
            if failover and t.get("model") in failover:
                next_model = failover[(failover.index(t["model"]) + 1) % len(failover)]
            elif failover and t.get("model") not in failover:
                next_model = failover[0]
        resume_args = SimpleNamespace(ids=[g["task_id"]], model=next_model,
                                      budget_tokens=getattr(a, "budget_tokens", None),
                                      budget_usd=getattr(a, "budget_usd", None),
                                      budget_minutes=getattr(a, "budget_minutes", None))
        try:
            tasks.resume(ctx, resume_args, handoff=bool(next_model and next_model != t.get("model")))
        except AKError as e:
            g.update(status="needs_review", verdict=rep.get("verdict") or "needs_review",
                     reason=str(e)[:200], ended=time.time())
            save(ctx.root, g)
            return {"id": g["id"], "status": "needs_review", "reason": g["reason"], "task_id": g["task_id"]}
        g["iterations"] += 1
        save(ctx.root, g)
    save(ctx.root, g)
    return {"id": g["id"], "status": "running", "task_id": g["task_id"], "iteration": g["iterations"]}


def list_goals(root: Path, limit: int = 8) -> list:
    out = []
    for p in sorted(_dir(root).glob("g*/goal.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        try:
            g = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": g["id"], "status": g["status"], "task_id": g["task_id"],
                        "iteration": g.get("iterations", 0), "verdict": g.get("verdict")})
        except (OSError, ValueError, KeyError):
            continue
    return out


def status(ctx: Ctx, gid: str | None) -> dict:
    if not gid:
        return {"goals": list_goals(ctx.root)}
    g = load(ctx.root, gid)
    t = tasks.load(ctx.root, g["task_id"])
    out = {"id": g["id"], "status": g["status"], "task_id": g["task_id"], "task_status": t["status"],
           "iteration": g["iterations"], "max_iterations": g["max_iterations"]}
    if g.get("verdict"):
        out["verdict"] = g["verdict"]
    if g.get("reason"):
        out["reason"] = g["reason"][:120]
    if g.get("question"):
        out["question"] = g["question"]
    return out


def wait(ctx: Ctx, a) -> dict:
    g = load(ctx.root, a.id)
    deadline = time.monotonic() + float(a.timeout)
    while g["status"] not in TERMINAL and time.monotonic() < deadline:
        tasks.schedule(ctx.root)
        time.sleep(0.2)
        g = load(ctx.root, a.id)
    return status(ctx, a.id)


def report(ctx: Ctx, gid: str) -> dict:
    g = load(ctx.root, gid)
    rep, _trunc = tasks.report_view(ctx, g["task_id"])
    return {"id": g["id"], "status": g["status"], "iteration": g["iterations"],
            "max_iterations": g["max_iterations"], "task": rep, "history": g["history"][-3:],
            "receipt": g.get("receipt") or rep.get("receipt")}


def steer(ctx: Ctx, a) -> dict:
    g = load(ctx.root, gid := a.id)
    if g["status"] in TERMINAL:
        raise AKError(f"goal {gid} is {g['status']}", "finished goals cannot be steered", None, code="AK-TASK-STATE")
    text = a.text.strip()
    if not text:
        raise AKError("steer needs text", "", f"agentkit goal steer {gid} 'constraint'", code="AK-USAGE")
    g["spec"].setdefault("constraints", {}).setdefault("steering", []).append(text)
    t = tasks.load(ctx.root, g["task_id"])
    tasks.update(ctx.root, g["task_id"], goal=t["goal"] + "\nSTEER: " + text)
    save(ctx.root, g)
    return {"id": gid, "status": g["status"], "steering": g["spec"]["constraints"]["steering"][-3:]}


def cancel(ctx: Ctx, gid: str) -> dict:
    g = load(ctx.root, gid)
    if g["status"] in TERMINAL:
        return status(ctx, gid)
    tasks.cancel(ctx.root, g["task_id"])
    g.update(status="cancelled", verdict="cancelled", reason="goal cancelled", ended=time.time())
    save(ctx.root, g)
    return status(ctx, gid)


def answer(ctx: Ctx, a) -> dict:
    g = load(ctx.root, a.id)
    out = tasks.answer(ctx, g["task_id"], a.text)
    g.update(status="running", verdict=None, reason=None, ended=None)
    save(ctx.root, g)
    return {"id": g["id"], "status": "running", "task_id": g["task_id"], "task_resumes": out["resumes"]}


def resume(ctx: Ctx, a) -> dict:
    g = load(ctx.root, a.id)
    if g["status"] not in ("needs_review", "failed", "paused", "running"):
        raise AKError(f"goal {a.id} is {g['status']}", "only unfinished goals can resume", None, code="AK-TASK-STATE")
    t = tasks.load(ctx.root, g["task_id"])
    if t["status"] not in tasks.RESUMABLE:
        raise AKError(f"task {g['task_id']} is {t['status']}", "underlying task is not resumable; create a new goal", None, code="AK-TASK-STATE")
    resume_args = SimpleNamespace(ids=[g["task_id"]], model=getattr(a, "model", None),
                                  budget_tokens=getattr(a, "budget_tokens", None),
                                  budget_usd=getattr(a, "budget_usd", None),
                                  budget_minutes=getattr(a, "budget_minutes", None))
    tasks.resume(ctx, resume_args, handoff=bool(getattr(a, "model", None) and a.model != t.get("model")))
    g.update(status="running", verdict=None, reason=None, ended=None)
    save(ctx.root, g)
    return {"id": g["id"], "status": "running", "task_id": g["task_id"]}


def cmd_goal(ctx: Ctx, a) -> Result:
    sub = a.goal_cmd
    if sub == "create":
        out = create(ctx, a)
        return Result(out, next=ctx.next_cmd("goal", "run", out["id"]))
    if sub == "status":
        return Result(status(ctx, getattr(a, "id", None)))
    if sub == "run":
        return Result(run(ctx, a))
    if sub == "wait":
        return Result(wait(ctx, a))
    if sub == "report":
        return Result(report(ctx, a.id))
    if sub == "steer":
        return Result(steer(ctx, a))
    if sub == "cancel":
        return Result(cancel(ctx, a.id))
    if sub == "resume":
        return Result(resume(ctx, a))
    if sub == "answer":
        return Result(answer(ctx, a), next=ctx.next_cmd("goal", "run", a.id))
    raise AKError("goal needs a subcommand", "use create|run|status|wait|report|steer|answer|cancel|resume", "agentkit goal --help", code="AK-USAGE")
