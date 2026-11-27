# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Machine-readable worker backend capability contracts."""
import pytest

from agentkit.backends import backend_capabilities
from agentkit.protocol import BackendCapabilities, ProtocolError


def test_capability_defaults_are_conservative_and_roundtrip():
    caps = BackendCapabilities(backend="custom", event_protocol="jsonl")
    assert caps.headless is False and caps.session_resume is False and caps.cost_reporting is False
    assert BackendCapabilities.from_dict(caps.to_dict()).to_dict() == caps.to_dict()
    with pytest.raises(ProtocolError, match="boolean"):
        BackendCapabilities.from_dict({**caps.to_dict(), "streaming": "yes"})
    with pytest.raises(ProtocolError, match="event_protocol"):
        BackendCapabilities.from_dict({**caps.to_dict(), "event_protocol": "stdio"})
    with pytest.raises(ProtocolError, match="unknown fields"):
        BackendCapabilities.from_dict({**caps.to_dict(), "oauth_token": "secret"})


def test_builtin_and_external_backends_declare_distinct_capabilities():
    builtin = backend_capabilities("builtin")
    codex = backend_capabilities("codex")
    opencode = backend_capabilities("opencode")
    qoder = backend_capabilities("qoder")
    cursor = backend_capabilities("cursor")
    assert builtin["event_protocol"] == "internal" and builtin["session_resume"] is False
    assert codex["session_resume"] is False and codex["usage_reporting"] is True
    assert codex["approval_events"] is False and codex["cost_reporting"] is False
    assert opencode["oauth"] is True and opencode["cost_reporting"] is True
    assert qoder["oauth"] is True and qoder["credits_reporting"] is True
    assert qoder["cost_reporting"] is False
    assert cursor["event_protocol"] == "jsonl" and cursor["usage_reporting"] is False


def test_cursor_sdk_declares_usage_and_cost_reporting():
    caps = backend_capabilities("cursor", {"workers": {"cursor": {"transport": "sdk"}}})
    assert caps["event_protocol"] == "sdk"
    assert caps["usage_reporting"] is True and caps["cost_reporting"] is True


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="unknown worker backend"):
        backend_capabilities("unknown")
