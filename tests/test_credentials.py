# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Credential grant, binding, revocation and audit metadata."""
import json
import os

import pytest

from agentkit import cli, credentials, tasks


def _grant(root, **kwargs):
    return credentials.issue_grant(root, provider="openai", backend="builtin", auth_kind="api_key",
                                   ref_kind="env", ref="OPENAI_API_KEY", scopes=["model:call"],
                                   now=1.0, **kwargs)


def test_grant_binding_revocation_and_audit_roundtrip(tmp_path):
    grant = _grant(tmp_path)
    binding = credentials.create_binding(root=tmp_path, grant_id=grant["grant_id"], task_id="t123abc",
                                         run_id="r1", backend="builtin", session_id="builtin:t123abc", now=2.0)
    assert credentials.is_grant_active(tmp_path, grant["grant_id"], now=3.0)
    assert credentials.is_binding_active(tmp_path, binding["binding_id"], now=3.0)
    credentials.revoke(tmp_path, target_kind="grant", target_id=grant["grant_id"], reason_code="rotate",
                       reason="user requested rotation", now=4.0)
    assert not credentials.is_grant_active(tmp_path, grant["grant_id"], now=5.0)
    assert not credentials.is_binding_active(tmp_path, binding["binding_id"], now=5.0)
    rows = [json.loads(line) for line in (tmp_path / ".agentkit/state/credentials/audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["action"] for row in rows] == ["granted", "bound", "revoked"]
    assert "OPENAI_API_KEY" in json.dumps(grant)
    assert "sk-" not in json.dumps(rows)


def test_expiry_and_backend_mismatch_are_rejected(tmp_path):
    grant = _grant(tmp_path, expires_at=10.0)
    assert not credentials.is_grant_active(tmp_path, grant["grant_id"], now=10.0)
    with pytest.raises(ValueError, match="backend"):
        credentials.create_binding(root=tmp_path, grant_id=grant["grant_id"], task_id="t123abc",
                                   run_id="r1", backend="qoder")


def test_cli_can_show_and_revoke_task_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "selected-model-key")
    cfg = {"models": {"fake": {"base_url": "http://127.0.0.1:1/v1", "key_env": "FAKE_KEY"}}}
    task = {"id": "t123abc", "backend": "builtin", "model": "fake", "status": "queued"}
    task.update(tasks._bind_task_credential(task, cfg, "builtin", "fake", tmp_path))
    tasks.save(tmp_path, task)
    env, code, _ = cli.run_cli(["--root", str(tmp_path), "credential", "show", "t123abc"])
    assert code == 0 and env["data"]["binding_active"] is True
    assert "selected-model-key" not in json.dumps(env)
    binding_id = task["credential_binding_id"]
    env, code, _ = cli.run_cli(["--root", str(tmp_path), "credential", "revoke", binding_id])
    assert code == 0 and env["data"]["target_id"] == binding_id
    env, code, _ = cli.run_cli(["--root", str(tmp_path), "credential", "show", binding_id])
    assert code == 0 and env["data"]["active"] is False


def test_metadata_files_are_private_on_posix(tmp_path):
    grant = _grant(tmp_path)
    path = tmp_path / ".agentkit/state/credentials/grants" / f"{grant['grant_id']}.json"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="invalid"):
        credentials.load_grant(tmp_path, "../escape")
