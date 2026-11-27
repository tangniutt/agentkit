# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Deterministic brand-neutral routing over measured model profiles."""
from __future__ import annotations

from .protocol import ModelProfile, ProtocolError, RoutingDecision, TaskRisk


def role_topology(risk: TaskRisk) -> list[str]:
    risk.validate()
    if risk.blast_radius == 3:
        return ["planner", "implementer", "reviewer"]
    if risk.complexity >= 2 or risk.blast_radius == 2:
        return ["planner", "implementer"]
    return ["implementer"]


def _expected_cost(profile: ModelProfile) -> float | None:
    evidence = profile.evidence
    if "expected_cost_per_verified_success" in evidence:
        return float(evidence["expected_cost_per_verified_success"])
    average = evidence.get("average_task_cost")
    lower = evidence.get("wilson_lower_bound")
    if average is not None and lower:
        return float(average) / float(lower)
    return None


def profiles_from_config(cfg: dict) -> list[ModelProfile]:
    routing = cfg.get("routing") or {}
    if not isinstance(routing, dict) or not isinstance(routing.get("profiles") or {}, dict):
        raise ProtocolError("routing.profiles must be a TOML table")
    profiles = []
    for name, data in (routing.get("profiles") or {}).items():
        if not isinstance(name, str) or not name or not isinstance(data, dict):
            raise ProtocolError("routing profile names and values are invalid")
        profiles.append(ModelProfile.from_dict({**data, "model_id": name}))
    return profiles


def route(task_id: str, risk: TaskRisk, profiles: list[ModelProfile], *, min_samples: int = 5,
          capability_threshold: float = 0.6, exclude_models: set[str] | None = None) -> RoutingDecision:
    risk.validate()
    if min_samples < 1:
        raise ProtocolError("min_samples must be positive")
    if not 0 <= capability_threshold <= 1:
        raise ProtocolError("capability_threshold must be between 0 and 1")
    candidates = []
    excluded = exclude_models or set()
    for profile in profiles:
        profile.validate()
        if profile.model_id in excluded:
            continue
        evidence = profile.evidence
        if profile.status != "active" or int(evidence.get("sample_count", 0)) < min_samples:
            continue
        if any(float(profile.capabilities.get(name, 0)) < capability_threshold
               for name in risk.required_capabilities):
            continue
        cost = _expected_cost(profile)
        if cost is None:
            continue
        candidates.append((cost, profile))
    candidates.sort(key=lambda item: (item[0], item[1].model_id, item[1].model_version))
    if not candidates:
        raise ProtocolError("no active model profile satisfies the task risk and evidence threshold")
    cost, selected = candidates[0]
    fallbacks = [f"{profile.model_id}@{profile.model_version}" for _, profile in candidates[1:]]
    return RoutingDecision(task_id=task_id, model_id=selected.model_id, model_version=selected.model_version,
                           role="implementer", reasoning_level="low",
                           expected_cost_per_verified_success=cost, fallback_profiles=fallbacks,
                           review_required=risk.blast_radius >= 2,
                           reason="lowest expected cost per verified success among eligible measured profiles")
