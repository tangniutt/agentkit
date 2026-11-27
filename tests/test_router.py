# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Brand-neutral task risk, model profile and deterministic routing."""
import pytest

from agentkit.protocol import ModelProfile, ProtocolError, RoutingDecision, TaskRisk
from agentkit.router import role_topology, route


def _profile(model_id, cost, *, status="active", samples=10, capability=0.9):
    return ModelProfile(model_id=model_id, model_version="v1", status=status,
                        capabilities={"code_edit": capability},
                        evidence={"sample_count": samples, "verified_pass_rate": 0.9,
                                  "wilson_lower_bound": 0.8, "average_task_cost": cost,
                                  "expected_cost_per_verified_success": cost},
                        evaluated_at="2026-11-28T00:00:00Z", eval_config_sha256="a" * 64)


def test_task_risk_and_profile_roundtrip():
    risk = TaskRisk(complexity=2, blast_radius=1, required_capabilities=["code_edit"])
    assert TaskRisk.from_dict(risk.to_dict()).to_dict() == risk.to_dict()
    profile = _profile("provider/model-a", 0.02)
    assert ModelProfile.from_dict(profile.to_dict()).to_dict() == profile.to_dict()
    with pytest.raises(ProtocolError, match="blast_radius"):
        TaskRisk.from_dict({"complexity": 0, "blast_radius": 3, "verification_strength": "basic"})
    with pytest.raises(ProtocolError, match="between 0 and 1"):
        ModelProfile.from_dict({**profile.to_dict(), "capabilities": {"code_edit": 1.1}})
    with pytest.raises(ProtocolError, match="unknown fields"):
        ModelProfile.from_dict({**profile.to_dict(), "brand_rank": 1})


def test_role_topology_uses_risk_not_brand():
    assert role_topology(TaskRisk(complexity=1, blast_radius=1)) == ["implementer"]
    assert role_topology(TaskRisk(complexity=2, blast_radius=1)) == ["planner", "implementer"]
    assert role_topology(TaskRisk(complexity=3, blast_radius=3, verification_strength="strong")) == [
        "planner", "implementer", "reviewer"]


def test_route_selects_lowest_expected_cost_among_measured_profiles():
    risk = TaskRisk(complexity=1, blast_radius=1, required_capabilities=["code_edit"])
    profiles = [_profile("provider/expensive", 0.08), _profile("provider/cheap", 0.02),
                _profile("provider/provisional", 0.001, status="provisional"),
                _profile("provider/thin-evidence", 0.001, samples=1),
                _profile("provider/weak", 0.001, capability=0.2)]
    decision = route("task-1", risk, profiles)
    assert decision.model_id == "provider/cheap"
    assert decision.role == "implementer" and decision.reasoning_level == "low"
    assert decision.fallback_profiles == ["provider/expensive@v1"]
    assert RoutingDecision.from_dict(decision.to_dict()).to_dict() == decision.to_dict()


def test_route_rejects_missing_evidence_or_capability():
    risk = TaskRisk(complexity=2, blast_radius=2, required_capabilities=["security"])
    with pytest.raises(ProtocolError, match="no active model profile"):
        route("task-1", risk, [_profile("provider/model-a", 0.02)])
    profile = _profile("provider/model-b", 0.02)
    profile.evidence.pop("expected_cost_per_verified_success")
    profile.evidence.pop("average_task_cost")
    profile.evidence.pop("wilson_lower_bound")
    with pytest.raises(ProtocolError, match="no active model profile"):
        route("task-1", TaskRisk(complexity=1, blast_radius=1), [profile])
