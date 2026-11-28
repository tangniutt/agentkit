# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Versioned protocol validation and canonical serialization."""
import pytest

from agentkit.protocol import (
    CredentialAudit,
    CredentialBinding,
    CredentialGrant,
    CredentialRevocation,
    Event,
    EvidenceReceipt,
    GoalSpec,
    HandoffPacket,
    ProtocolError,
    Retrospective,
    SandboxCapabilities,
    SandboxStatus,
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
        "attachments": [{"path": ".agentkit/tasks/t123abc/attachments/ref.png", "sha256": "d" * 64,
                         "mime": "image/png", "bytes": 68}],
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


def test_credential_metadata_roundtrip_never_contains_secret_values():
    grant = CredentialGrant(grant_id="cg_1", credential_id="cred_1", provider="openai", backend="builtin",
                            auth_kind="api_key", ref_kind="env", ref="OPENAI_API_KEY", owner="user",
                            scopes=["model:call"], issued_at=1.0, policy_sha256="a" * 64)
    binding = CredentialBinding(binding_id="cb_1", grant_id="cg_1", task_id="t123abc", run_id="r1",
                                backend="builtin", session_id="builtin:t123abc", created_at=1.0)
    revocation = CredentialRevocation(revocation_id="cr_1", target_kind="grant", target_id="cg_1",
                                      reason_code="user_revoked", reason="rotate key", actor="user", revoked_at=2.0)
    audit = CredentialAudit(audit_id="ca_1", ts=2.0, action="revoked", decision="allow", actor="user",
                            credential_id="cred_1", grant_id="cg_1", reason_code="user_revoked")
    for value in (grant, binding, revocation, audit):
        data = value.to_dict()
        assert type(value).from_dict(data).to_dict() == data
        assert not ({"secret", "token", "api_key", "value"} & set(data))
    with pytest.raises(ProtocolError, match="unknown fields"):
        CredentialGrant.from_dict({**grant.to_dict(), "secret": "sk-test"})
    with pytest.raises(ProtocolError, match="auth_kind"):
        CredentialGrant.from_dict({**grant.to_dict(), "auth_kind": "cookie"})
    with pytest.raises(ProtocolError, match="status"):
        CredentialBinding.from_dict({**binding.to_dict(), "status": "sleeping"})


def test_sandbox_capabilities_and_status_are_conservative():
    caps = SandboxCapabilities(provider="bubblewrap", snapshot_kinds=["git", "files"],
                               network_modes=["shared", "isolated", "allowlist"])
    assert caps.pause is False and caps.resume is False and caps.fork is False
    assert SandboxCapabilities.from_dict(caps.to_dict()).to_dict() == caps.to_dict()
    status = SandboxStatus(sandbox_id="sandbox-t123abc", task_id="t123abc", provider="bubblewrap",
                           state="completed", capabilities=caps.to_dict())
    assert SandboxStatus.from_dict(status.to_dict()).to_dict() == status.to_dict()
    with pytest.raises(ProtocolError, match="snapshot kind"):
        SandboxCapabilities.from_dict({**caps.to_dict(), "snapshot_kinds": ["swap"]})
    with pytest.raises(ProtocolError, match="state"):
        SandboxStatus.from_dict({**status.to_dict(), "state": "sleeping"})


def test_evidence_retrospective_and_event_roundtrip():
    receipt = EvidenceReceipt(
        receipt_id="er_1", subject={"kind": "task", "id": "t123abc"}, base_commit="abc",
        diff_sha256="d" * 64, acceptance_sha256="a" * 64, environment_sha256="e" * 64,
        checks=[{"cmd": "pytest", "result": "pass", "exit": 0}], verdict="pass",
        verifier_version="0.3.0", worker={"backend": "builtin", "model": "cheap"},
        cost={"tokens": 10, "usd": 0.1, "seconds": 1}, created_at="2026-11-25T00:00:00Z",
        artifacts=[{"path": "artifacts/proof.png", "sha256": "f" * 64, "mime": "image/png", "bytes": 68}])
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
