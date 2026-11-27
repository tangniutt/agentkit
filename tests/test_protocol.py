# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Versioned protocol validation and canonical serialization."""
import pytest

from agentkit.protocol import (
    Event,
    EvidenceReceipt,
    GoalSpec,
    HandoffPacket,
    ProtocolError,
    Retrospective,
    TaskSpec,
    ToolResult,
    sha256,
)


def _task():
    return TaskSpec.from_dict({
        "goal": "fix add",
        "context": ["calc.py"],
        "allowed_paths": ["src/"],
        "forbidden_paths": ["tests/"],
        "acceptance": {"checks": ["pytest -q"], "baseline_must_fail": True, "frozen": True},
        "budget": {"tokens": 1000, "minutes": 10},
        "worker": {"backend": "builtin", "model": "cheap"},
    })


def test_task_spec_defaults_and_roundtrip():
    task = _task()
    data = task.to_dict()
    assert data["budget"]["rounds"] == 40 and data["budget"]["usd"] == 2.0
    assert TaskSpec.from_dict(data).to_dict() == data
    assert sha256(data) == sha256(TaskSpec.from_dict(data).to_dict())


@pytest.mark.parametrize("data", [
    {"goal": "", "acceptance": {"checks": ["pytest"]}},
    {"goal": "x", "acceptance": {"checks": []}},
    {"goal": "x", "acceptance": {"checks": ["pytest"], "frozen": False}},
    {"goal": "x", "acceptance": {"checks": ["pytest"]}, "budget": {"tokens": -1}},
    {"goal": "x", "acceptance": {"checks": ["pytest"]}, "typo": True},
])
def test_task_spec_rejects_invalid(data):
    with pytest.raises(ProtocolError):
        TaskSpec.from_dict(data)


def test_goal_spec_is_bounded():
    goal = GoalSpec.from_dict({"outcome": "coverage above 80%", "acceptance": {"checks": ["pytest --cov"]}})
    assert goal.budget["tasks"] == 8 and goal.planning["mode"] == "supervisor"
    with pytest.raises(ProtocolError, match="tasks"):
        GoalSpec.from_dict({"outcome": "x", "acceptance": {"checks": ["pytest"]}, "budget": {"tasks": 0}})


def test_handoff_packet_is_portable_and_has_no_auth_fields():
    packet = HandoffPacket.from_dict({
        "task_id": "t123abc", "goal_version": 2, "from_backend": "antigravity", "to_backend": "codex",
        "reason": "quota_exhausted", "base_commit": "abc", "acceptance_sha256": "a" * 64,
        "worktree": ".agentkit/worktrees/t123abc", "diff_sha256": "d" * 64,
        "changed_files": ["calc.py"], "checks": [{"cmd": "pytest", "result": "fail"}],
        "memory_brief_sha256": "m" * 64, "decisions": ["keep API stable"], "unknowns": ["cache behavior"],
        "remaining_budget": {"tokens": 1000, "usd": 0.2, "minutes": 5},
    })
    data = packet.to_dict()
    assert HandoffPacket.from_dict(data).to_dict() == data
    assert set(data["remaining_budget"]) == {"tokens", "usd", "minutes"}
    with pytest.raises(ProtocolError, match="unknown fields"):
        HandoffPacket.from_dict({**data, "authorization": "Bearer secret"})
    with pytest.raises(ProtocolError, match="different backends"):
        HandoffPacket.from_dict({**data, "to_backend": "antigravity"})
    with pytest.raises(ProtocolError, match="reason"):
        HandoffPacket.from_dict({**data, "reason": "account_rotation"})
    with pytest.raises(ProtocolError, match="non-negative"):
        HandoffPacket.from_dict({**data, "remaining_budget": {"tokens": -1}})


def test_tool_result_requires_next_when_truncated():
    with pytest.raises(ProtocolError, match="next"):
        ToolResult(ok=True, data="x", truncated=True).validate()
    result = ToolResult.from_dict({"ok": False, "error": {"code": "AK-PATH"}, "truncated": False})
    assert result.error["code"] == "AK-PATH"


def test_evidence_retrospective_and_event_roundtrip():
    receipt = EvidenceReceipt(
        receipt_id="er_1", subject={"kind": "task", "id": "t123abc"}, base_commit="abc",
        diff_sha256="d" * 64, acceptance_sha256="a" * 64, environment_sha256="e" * 64,
        checks=[{"cmd": "pytest", "result": "pass", "exit": 0}], verdict="pass",
        verifier_version="0.3.0", worker={"backend": "builtin", "model": "cheap"},
        cost={"tokens": 10, "usd": 0.1, "seconds": 1}, created_at="2026-11-25T00:00:00Z")
    assert EvidenceReceipt.from_dict(receipt.to_dict()).to_dict() == receipt.to_dict()
    retro = Retrospective(id="rp_1", subject={"kind": "task", "id": "t123abc"}, outcome="fixed",
                          changes=[{"path": "calc.py"}], evidence=[{"receipt": "er_1"}], failures=[],
                          cost={"tokens": 10}, next_actions=["merge"], created_at="2026-11-25T00:00:00Z")
    assert Retrospective.from_dict(retro.to_dict()).next_actions == ["merge"]
    event = Event(event_id="ev_1", ts=1.0, type="task.completed", task_id="t123abc", run_id="r1",
                  session_id="ses_1", turn_id="turn_1", item_id="item_1", to_state="done")
    assert Event.from_dict(event.to_dict()).to_dict() == event.to_dict()
    with pytest.raises(ProtocolError, match="turn_id"):
        Event.from_dict({**event.to_dict(), "turn_id": ""})
    with pytest.raises(ProtocolError, match="verdict"):
        EvidenceReceipt.from_dict({**receipt.to_dict(), "verdict": "maybe"})
