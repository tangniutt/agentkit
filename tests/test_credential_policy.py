# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""User-controlled opt-in for external CLI saved credentials."""
import json
import sys
from types import SimpleNamespace

import pytest

from agentkit import tasks
from agentkit.backends import opencode_state_paths
from agentkit.config import load_config, saved_credentials_allowed
from agentkit.core import AKError, Ctx
from agentkit.doctor import cmd_doctor


def _config(tmp_path, monkeypatch, *, allow=False):
    user = tmp_path / "user-config"
    user.mkdir(exist_ok=True)
    (user / "agentkit").mkdir(exist_ok=True)
    (user / "agentkit" / "config.toml").write_text(
        f"[security]\nallow_saved_credentials = {str(allow).lower()}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user))
    root = tmp_path / "repo"
    (root / ".agentkit").mkdir(parents=True, exist_ok=True)
    (root / ".agentkit" / "config.toml").write_text(
        "[security]\nallow_saved_credentials = true\n[_user_security]\nallow_saved_credentials = true\n"
        "[workers.qoder]\n"
        f"command = [{json.dumps(sys.executable)}]\n",
        encoding="utf-8")
    return root, load_config(root)


def test_saved_credentials_require_user_level_opt_in(tmp_path, monkeypatch):
    _root, cfg = _config(tmp_path, monkeypatch)
    assert saved_credentials_allowed(cfg) is False
    _root, cfg = _config(tmp_path, monkeypatch, allow=True)
    assert saved_credentials_allowed(cfg) is True


def test_qoder_saved_login_requires_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("QODER_PERSONAL_ACCESS_TOKEN", raising=False)
    root, cfg = _config(tmp_path, monkeypatch)
    with pytest.raises(AKError) as error:
        tasks._check_credential_policy(cfg, "qoder", {}, root)
    assert error.value.code == "AK-CONFIG"
    monkeypatch.setenv("QODER_PERSONAL_ACCESS_TOKEN", "test-pat")
    tasks._check_credential_policy(cfg, "qoder", {}, root)


def test_qoder_dedicated_key_env_does_not_fall_back_to_saved_login(tmp_path, monkeypatch):
    monkeypatch.delenv("QODER_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("QODER_TEST_PAT", raising=False)
    root, cfg = _config(tmp_path, monkeypatch, allow=True)
    with pytest.raises(AKError) as error:
        tasks._check_credential_policy(cfg, "qoder", {"key_env": "QODER_TEST_PAT"}, root)
    assert error.value.code == "AK-MODEL-KEYENV"
    monkeypatch.setenv("QODER_PERSONAL_ACCESS_TOKEN", "test-pat")
    tasks._check_credential_policy(cfg, "qoder", {"key_env": "QODER_TEST_PAT"}, root)


def test_qoder_external_config_dir_requires_opt_in(tmp_path, monkeypatch):
    root, cfg = _config(tmp_path, monkeypatch, allow=True)
    external = tmp_path / "outside"
    with pytest.raises(AKError) as error:
        tasks._check_credential_policy({**cfg, "_user_security": {}}, "qoder", {"config_dir": str(external)}, root)
    assert error.value.code == "AK-CONFIG"
    tasks._check_credential_policy(cfg, "qoder", {"config_dir": str(external)}, root)
    monkeypatch.setenv("QODER_PERSONAL_ACCESS_TOKEN", "test-pat")
    tasks._check_credential_policy({**cfg, "_user_security": {}}, "qoder", {"config_dir": "local-config"}, root)


def test_doctor_reports_saved_credential_policy(tmp_path, monkeypatch):
    monkeypatch.delenv("QODER_PERSONAL_ACCESS_TOKEN", raising=False)
    root, cfg = _config(tmp_path, monkeypatch)
    result = cmd_doctor(Ctx(root=root, cwd=root, config=cfg), SimpleNamespace(online=False))
    checks = {check["name"]: check for check in result.data["checks"]}
    assert checks["saved_credentials"]["ok"] is True
    assert "disabled" in checks["saved_credentials"]["value"]
    assert "saved login disabled" in checks["qoder_backend"]["value"]
    _root, cfg = _config(tmp_path, monkeypatch, allow=True)
    result = cmd_doctor(Ctx(root=root, cwd=root, config=cfg), SimpleNamespace(online=False))
    checks = {check["name"]: check for check in result.data["checks"]}
    assert "user opt-in enabled" in checks["saved_credentials"]["value"]
    assert "saved login approved" in checks["qoder_backend"]["value"]
    cfg["workers"]["qoder"]["key_env"] = "QODER_TEST_PAT"
    monkeypatch.delenv("QODER_TEST_PAT", raising=False)
    result = cmd_doctor(Ctx(root=root, cwd=root, config=cfg), SimpleNamespace(online=False))
    check = next(c for c in result.data["checks"] if c["name"] == "qoder_backend")
    assert check["ok"] is False and "dedicated PAT missing" in check["value"]


def test_opencode_saved_login_requires_opt_in(tmp_path, monkeypatch):
    root, cfg = _config(tmp_path, monkeypatch)
    with pytest.raises(AKError) as error:
        tasks._check_credential_policy(cfg, "opencode", {"allow_account_login": True}, root)
    assert error.value.code == "AK-CONFIG"
    assert opencode_state_paths({"workers": {"opencode": {"allow_account_login": True}}}) == []
    _root, cfg = _config(tmp_path, monkeypatch, allow=True)
    tasks._check_credential_policy(cfg, "opencode", {"allow_account_login": True}, root)
    assert len(opencode_state_paths({**cfg, "workers": {"opencode": {"allow_account_login": True}}})) == 3
