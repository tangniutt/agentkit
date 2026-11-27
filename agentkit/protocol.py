# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Versioned JSON contracts for tasks, handoffs, goals, evidence and events."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"


class ProtocolError(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _keys(data: dict, allowed: set[str], required: set[str] = frozenset()) -> None:
    if not isinstance(data, dict):
        raise ProtocolError("expected object")
    missing = required - set(data)
    unknown = set(data) - allowed
    if missing:
        raise ProtocolError(f"missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ProtocolError(f"unknown fields: {', '.join(sorted(unknown))}")


def _strings(value: Any, name: str, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ProtocolError(f"{name} must be a string array")
    if nonempty and not value:
        raise ProtocolError(f"{name} must not be empty")
    return value


def _number(value: Any, name: str, default: float = 0) -> float:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ProtocolError(f"{name} must be a non-negative number")
    return float(value)


def default_budget() -> dict:
    return {"tokens": 200000, "usd": 2.0, "minutes": 20, "rounds": 40, "tasks": 8}


def validate_acceptance(data: Any) -> dict:
    _keys(data, {"checks", "allow", "forbidden_paths", "baseline_must_fail", "frozen", "manual_evidence"},
          {"checks"})
    checks = _strings(data.get("checks"), "acceptance.checks", nonempty=True)
    allow = _strings(data.get("allow", []), "acceptance.allow")
    forbidden = _strings(data.get("forbidden_paths", []), "acceptance.forbidden_paths")
    manual = _strings(data.get("manual_evidence", []), "acceptance.manual_evidence")
    baseline = data.get("baseline_must_fail", True)
    frozen = data.get("frozen", True)
    if not isinstance(baseline, bool) or not isinstance(frozen, bool):
        raise ProtocolError("acceptance flags must be boolean")
    if not frozen:
        raise ProtocolError("acceptance.frozen must be true")
    return {"checks": checks, "allow": allow, "forbidden_paths": forbidden,
            "baseline_must_fail": baseline, "frozen": True, "manual_evidence": manual}


def validate_budget(data: Any, *, goal: bool = False) -> dict:
    allowed = {"tokens", "usd", "minutes", "rounds", "tasks"} if goal else {"tokens", "usd", "minutes", "rounds"}
    _keys(data or {}, allowed)
    defaults = default_budget()
    out = {k: _number((data or {}).get(k), f"budget.{k}", defaults[k]) for k in allowed}
    if out["rounds"] <= 0 or out["minutes"] <= 0:
        raise ProtocolError("budget.rounds and budget.minutes must be positive")
    if goal and out["tasks"] <= 0:
        raise ProtocolError("budget.tasks must be positive")
    return out


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: dict | None = None
    truncated: bool = False
    next: str | None = None
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> "ToolResult":
        if not isinstance(self.ok, bool) or not isinstance(self.truncated, bool):
            raise ProtocolError("ok/truncated must be boolean")
        if self.error is not None and not isinstance(self.error, dict):
            raise ProtocolError("error must be object or null")
        if self.next is not None and not isinstance(self.next, str):
            raise ProtocolError("next must be string or null")
        if self.truncated and not self.next:
            raise ProtocolError("truncated results require next")
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ToolResult":
        _keys(data, {"schema_version", "ok", "data", "error", "truncated", "next"}, {"ok"})
        return cls(ok=data["ok"], data=data.get("data"), error=data.get("error"),
                   truncated=data.get("truncated", False), next=data.get("next"),
                   schema_version=data.get("schema_version", SCHEMA_VERSION)).validate()


@dataclass
class BackendCapabilities:
    backend: str
    event_protocol: str
    headless: bool = False
    streaming: bool = False
    session_resume: bool = False
    session_fork: bool = False
    approval_events: bool = False
    diff_events: bool = False
    native_mcp: bool = False
    oauth: bool = False
    api_key: bool = False
    usage_reporting: bool = False
    cost_reporting: bool = False
    credits_reporting: bool = False
    cancel: bool = False
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> "BackendCapabilities":
        if self.schema_version != SCHEMA_VERSION:
            raise ProtocolError(f"unsupported schema_version: {self.schema_version}")
        if not isinstance(self.backend, str) or not self.backend.strip():
            raise ProtocolError("backend must be a non-empty string")
        if self.event_protocol not in ("internal", "jsonl", "sdk"):
            raise ProtocolError("event_protocol must be internal, jsonl or sdk")
        for name in ("headless", "streaming", "session_resume", "session_fork", "approval_events",
                     "diff_events", "native_mcp", "oauth", "api_key", "usage_reporting",
                     "cost_reporting", "credits_reporting", "cancel"):
            if not isinstance(getattr(self, name), bool):
                raise ProtocolError(f"{name} must be boolean")
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BackendCapabilities":
        allowed = {"backend", "event_protocol", "headless", "streaming", "session_resume", "session_fork",
                   "approval_events", "diff_events", "native_mcp", "oauth", "api_key", "usage_reporting",
                   "cost_reporting", "credits_reporting", "cancel", "schema_version"}
        _keys(data, allowed, {"backend", "event_protocol"})
        return cls(**{k: data[k] for k in allowed if k in data}).validate()


@dataclass
class TaskSpec:
    goal: str
    acceptance: dict
    budget: dict = field(default_factory=default_budget)
    context: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    worker: dict = field(default_factory=lambda: {"backend": "builtin", "failover": []})
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> "TaskSpec":
        if self.schema_version != SCHEMA_VERSION:
            raise ProtocolError(f"unsupported schema_version: {self.schema_version}")
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ProtocolError("goal must be a non-empty string")
        self.context = _strings(self.context, "context")
        self.allowed_paths = _strings(self.allowed_paths, "allowed_paths")
        self.forbidden_paths = _strings(self.forbidden_paths, "forbidden_paths")
        self.acceptance = validate_acceptance(self.acceptance)
        self.budget = validate_budget(self.budget)
        if not isinstance(self.worker, dict):
            raise ProtocolError("worker must be object")
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskSpec":
        _keys(data, {"schema_version", "goal", "acceptance", "budget", "context", "allowed_paths",
                     "forbidden_paths", "worker"}, {"goal", "acceptance"})
        return cls(goal=data["goal"], acceptance=data["acceptance"], budget=data.get("budget") or default_budget(),
                   context=data.get("context", []), allowed_paths=data.get("allowed_paths", []),
                   forbidden_paths=data.get("forbidden_paths", []),
                   worker=data.get("worker") or {"backend": "builtin", "failover": []},
                   schema_version=data.get("schema_version", SCHEMA_VERSION)).validate()


@dataclass
class GoalSpec:
    outcome: str
    acceptance: dict
    budget: dict = field(default_factory=default_budget)
    non_goals: list[str] = field(default_factory=list)
    constraints: dict = field(default_factory=lambda: {"max_tasks": 8, "max_handoffs": 2, "allowed_paths": []})
    planning: dict = field(default_factory=lambda: {"mode": "supervisor", "approval_required": False})
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> "GoalSpec":
        if self.schema_version != SCHEMA_VERSION:
            raise ProtocolError(f"unsupported schema_version: {self.schema_version}")
        if not isinstance(self.outcome, str) or not self.outcome.strip():
            raise ProtocolError("outcome must be a non-empty string")
        self.non_goals = _strings(self.non_goals, "non_goals")
        self.acceptance = validate_acceptance(self.acceptance)
        self.budget = validate_budget(self.budget, goal=True)
        if not isinstance(self.constraints, dict) or not isinstance(self.planning, dict):
            raise ProtocolError("constraints/planning must be objects")
        if self.planning.get("mode", "supervisor") not in ("supervisor", "model"):
            raise ProtocolError("planning.mode must be supervisor or model")
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GoalSpec":
        _keys(data, {"schema_version", "outcome", "acceptance", "budget", "non_goals", "constraints", "planning"},
              {"outcome", "acceptance"})
        return cls(outcome=data["outcome"], acceptance=data["acceptance"], budget=data.get("budget") or default_budget(),
                   non_goals=data.get("non_goals", []),
                   constraints=data.get("constraints") or {"max_tasks": 8, "max_handoffs": 2, "allowed_paths": []},
                   planning=data.get("planning") or {"mode": "supervisor", "approval_required": False},
                   schema_version=data.get("schema_version", SCHEMA_VERSION)).validate()


@dataclass
class EvidenceReceipt:
    receipt_id: str
    subject: dict
    base_commit: str
    diff_sha256: str
    acceptance_sha256: str
    environment_sha256: str
    checks: list[dict]
    verdict: str
    verifier_version: str
    worker: dict
    cost: dict
    created_at: str
    suspicious: list[dict] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> "EvidenceReceipt":
        if self.schema_version != SCHEMA_VERSION:
            raise ProtocolError(f"unsupported schema_version: {self.schema_version}")
        if not isinstance(self.receipt_id, str) or not self.receipt_id:
            raise ProtocolError("receipt_id is required")
        if not isinstance(self.subject, dict) or not self.subject.get("kind") or not self.subject.get("id"):
            raise ProtocolError("subject requires kind and id")
        for name in ("base_commit", "diff_sha256", "acceptance_sha256", "environment_sha256", "verifier_version", "created_at"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ProtocolError(f"{name} is required")
        if self.verdict not in ("pass", "fail", "invalid", "needs_review"):
            raise ProtocolError("invalid verdict")
        if not isinstance(self.checks, list) or any(not isinstance(x, dict) for x in self.checks):
            raise ProtocolError("checks must be objects")
        if not isinstance(self.suspicious, list) or any(not isinstance(x, dict) for x in self.suspicious):
            raise ProtocolError("suspicious must be objects")
        if not isinstance(self.worker, dict) or not isinstance(self.cost, dict):
            raise ProtocolError("worker/cost must be objects")
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceReceipt":
        allowed = {"schema_version", "receipt_id", "subject", "base_commit", "diff_sha256", "acceptance_sha256",
                   "environment_sha256", "checks", "verdict", "suspicious", "verifier_version", "worker", "cost", "created_at"}
        _keys(data, allowed, allowed - {"schema_version", "suspicious"})
        return cls(**{k: data[k] for k in allowed if k in data}).validate()


@dataclass
class Retrospective:
    id: str
    subject: dict
    outcome: str
    changes: list[dict]
    evidence: list[dict]
    failures: list[dict]
    cost: dict
    next_actions: list[str]
    created_at: str
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> "Retrospective":
        if not isinstance(self.id, str) or not self.id or not isinstance(self.subject, dict):
            raise ProtocolError("id/subject required")
        if not isinstance(self.outcome, str) or not self.outcome:
            raise ProtocolError("outcome required")
        for name in ("changes", "evidence", "failures"):
            value = getattr(self, name)
            if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
                raise ProtocolError(f"{name} must be objects")
        if not isinstance(self.cost, dict):
            raise ProtocolError("cost must be object")
        self.next_actions = _strings(self.next_actions, "next_actions")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise ProtocolError("created_at required")
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Retrospective":
        allowed = {"schema_version", "id", "subject", "outcome", "changes", "evidence", "failures", "cost",
                   "next_actions", "created_at"}
        _keys(data, allowed, allowed - {"schema_version"})
        return cls(**{k: data[k] for k in allowed if k in data}).validate()


@dataclass
class HandoffPacket:
    task_id: str
    goal_version: int
    from_backend: str
    to_backend: str
    reason: str
    base_commit: str
    acceptance_sha256: str
    worktree: str
    diff_sha256: str
    changed_files: list[str]
    checks: list[dict]
    memory_brief_sha256: str
    decisions: list[str]
    unknowns: list[str]
    remaining_budget: dict
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> "HandoffPacket":
        if self.schema_version != SCHEMA_VERSION:
            raise ProtocolError(f"unsupported schema_version: {self.schema_version}")
        for name in ("task_id", "from_backend", "to_backend", "base_commit", "acceptance_sha256",
                     "worktree", "diff_sha256", "memory_brief_sha256"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ProtocolError(f"{name} is required")
        if isinstance(self.goal_version, bool) or not isinstance(self.goal_version, int) or self.goal_version < 1:
            raise ProtocolError("goal_version must be a positive integer")
        if self.from_backend == self.to_backend:
            raise ProtocolError("handoff requires different backends")
        if self.reason not in ("quota_exhausted", "context_limit", "timeout", "tool_failure", "supervisor_switch"):
            raise ProtocolError("invalid handoff reason")
        self.changed_files = _strings(self.changed_files, "changed_files")
        self.decisions = _strings(self.decisions, "decisions")
        self.unknowns = _strings(self.unknowns, "unknowns")
        if not isinstance(self.checks, list) or any(not isinstance(x, dict) for x in self.checks):
            raise ProtocolError("checks must be objects")
        _keys(self.remaining_budget, {"tokens", "usd", "minutes"})
        self.remaining_budget = {k: _number(self.remaining_budget.get(k), f"remaining_budget.{k}")
                                 for k in ("tokens", "usd", "minutes")}
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "HandoffPacket":
        allowed = {"schema_version", "task_id", "goal_version", "from_backend", "to_backend", "reason",
                   "base_commit", "acceptance_sha256", "worktree", "diff_sha256", "changed_files", "checks",
                   "memory_brief_sha256", "decisions", "unknowns", "remaining_budget"}
        optional = {"changed_files", "checks", "decisions", "unknowns"}
        _keys(data, allowed, allowed - {"schema_version"} - optional)
        values = {k: data[k] for k in allowed - optional if k in data}
        return cls(**values, changed_files=data.get("changed_files", []), checks=data.get("checks", []),
                   decisions=data.get("decisions", []), unknowns=data.get("unknowns", [])).validate()


@dataclass
class Event:
    event_id: str
    ts: float
    type: str
    from_state: str | None = None
    to_state: str | None = None
    goal_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    component: str | None = None
    step: int | None = None
    duration: float | None = None
    error_code: str | None = None
    retryable: bool | None = None
    artifact: str | None = None
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> "Event":
        if not isinstance(self.event_id, str) or not self.event_id or not isinstance(self.type, str) or not self.type:
            raise ProtocolError("event_id/type required")
        if isinstance(self.ts, bool) or not isinstance(self.ts, (int, float)) or self.ts < 0:
            raise ProtocolError("ts must be non-negative number")
        if self.retryable is not None and not isinstance(self.retryable, bool):
            raise ProtocolError("retryable must be boolean or null")
        for name in ("session_id", "turn_id", "item_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ProtocolError(f"{name} must be a non-empty string or null")
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        allowed = {"schema_version", "event_id", "ts", "type", "from_state", "to_state", "goal_id", "task_id",
                   "run_id", "session_id", "turn_id", "item_id", "component", "step", "duration",
                   "error_code", "retryable", "artifact"}
        _keys(data, allowed, {"event_id", "ts", "type"})
        return cls(**{k: data[k] for k in allowed if k in data}).validate()
