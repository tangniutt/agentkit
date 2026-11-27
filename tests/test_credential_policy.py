# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""User-controlled opt-in for external CLI saved credentials."""
import json
import sys
from types import SimpleNamespace

import pytest

from conftest import git
from agentkit import credentials, tasks
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


def test_sandbox_environment_hides_host_state(tmp_path):
    home = tmp_path / "host-home"
    xdg = {name: home / name.lower() for name in
           ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")}
    task_dir = tmp_path / "task"
    env = {"HOME": str(home), "QODER_CONFIG_DIR": str(home / "qoder"), "CODEX_HOME": str(home / "codex"),
           "OPENCODE_DB": str(home / "opencode.db"), "SSH_AUTH_SOCK": str(home / "agent.sock"),
           "GPG_AGENT_INFO": str(home / "gpg"), "DBUS_SESSION_BUS_ADDRESS": str(home / "dbus"),
           "AWS_SHARED_CREDENTIALS_FILE": str(home / "aws"), "GOOGLE_APPLICATION_CREDENTIALS": str(home / "gcp"),
           "GIT_CONFIG_GLOBAL": str(home / "gitconfig"), "GIT_CREDENTIAL_HELPER": str(home / "helper")}
    tasks.sandbox_environment(env, task_dir, "codex", [], home, xdg)
    assert env["HOME"] == str(task_dir / "home")
    for name in xdg:
        assert env[name] == str(task_dir / name.lower())
    for name in ("QODER_CONFIG_DIR", "CODEX_HOME", "OPENCODE_DB", "SSH_AUTH_SOCK", "SSH_ASKPASS",
                 "GPG_AGENT_INFO", "DBUS_SESSION_BUS_ADDRESS", "GIT_ASKPASS", "GIT_CREDENTIAL_HELPER",
                 "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE",
                 "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_CONFIG_DIR", "KUBECONFIG", "DOCKER_CONFIG",
                 "NPM_CONFIG_USERCONFIG", "PIP_CONFIG_FILE", "CLOUDSDK_CONFIG"):
        assert name not in env


def test_sandbox_environment_preserves_user_config_without_host_home(tmp_path, monkeypatch):
    user = tmp_path / "user-config"
    (user / "agentkit").mkdir(parents=True)
    (user / "agentkit" / "config.toml").write_text(
        '[security]\nallow_saved_credentials = true\n[models.fake]\nbase_url = "http://127.0.0.1:1/v1"\n'
        'model = "fake"\nkey_env = "FAKE_KEY"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user))
    root = tmp_path / "repo"
    (root / ".agentkit").mkdir(parents=True)
    home = tmp_path / "host-home"
    xdg = {name: home / name.lower() for name in
           ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")}
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="utf-8")
    (task_dir / "user-config.json").symlink_to(victim)
    env = {}
    tasks.sandbox_environment(env, task_dir, "builtin", [], home, xdg)
    assert victim.read_text(encoding="utf-8") == "unchanged"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.setenv("AGENTKIT_USER_CONFIG_SNAPSHOT", env["AGENTKIT_USER_CONFIG_SNAPSHOT"])
    cfg = load_config(root)
    assert saved_credentials_allowed(cfg) is True
    assert cfg["models"]["fake"]["key_env"] == "FAKE_KEY"


def test_sandbox_environment_restores_only_authorized_backend_paths(tmp_path):
    home = tmp_path / "host-home"
    xdg = {name: home / name.lower() for name in
           ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")}
    task_dir = tmp_path / "task"
    qoder_path = xdg["XDG_CONFIG_HOME"] / "agentkit" / "qoder"
    env = {"QODER_CONFIG_DIR": str(home / "other")}
    tasks.sandbox_environment(env, task_dir, "qoder", [qoder_path], home, xdg)
    assert env["QODER_CONFIG_DIR"] == str(qoder_path)
    opencode_paths = [xdg["XDG_DATA_HOME"] / "opencode", xdg["XDG_CACHE_HOME"] / "opencode",
                      xdg["XDG_STATE_HOME"] / "opencode"]
    tasks.sandbox_environment(env, task_dir, "opencode", opencode_paths, home, xdg)
    assert env["XDG_DATA_HOME"] == str(xdg["XDG_DATA_HOME"])
    assert env["XDG_CACHE_HOME"] == str(xdg["XDG_CACHE_HOME"])
    assert env["XDG_STATE_HOME"] == str(xdg["XDG_STATE_HOME"])
    assert "QODER_CONFIG_DIR" not in env


def test_cursor_external_config_dir_requires_opt_in(tmp_path, monkeypatch):
    root, cfg = _config(tmp_path, monkeypatch)
    external = tmp_path / "cursor-outside"
    with pytest.raises(AKError) as error:
        tasks._check_credential_policy(cfg, "cursor", {"config_dir": str(external)}, root)
    assert error.value.code == "AK-CONFIG"
    tasks._check_credential_policy(cfg, "cursor", {"config_dir": "cursor-config"}, root)
    _root, cfg = _config(tmp_path, monkeypatch, allow=True)
    tasks._check_credential_policy(cfg, "cursor", {"config_dir": str(external)}, root)


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


def test_worker_environment_allowlist_keeps_only_selected_key(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/safe/bin")
    monkeypatch.setenv("FAKE_KEY", "selected-model-key")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "other-provider-secret")
    cfg = {"models": {"fake": {"base_url": "http://127.0.0.1:1/v1", "key_env": "FAKE_KEY"}}}
    env = tasks.selected_worker_env(cfg, "builtin", "fake")
    assert env["PATH"] == "/safe/bin" and env["FAKE_KEY"] == "selected-model-key"
    assert not {"GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY"} & env.keys()

    monkeypatch.setenv("QODER_PERSONAL_ACCESS_TOKEN", "qoder-pat")
    monkeypatch.setenv("QODER_MISSING_KEY", "")
    env = tasks.selected_worker_env({"workers": {"qoder": {"key_env": "QODER_MISSING_KEY"}}}, "qoder", "")
    assert env["QODER_PERSONAL_ACCESS_TOKEN"] == "qoder-pat"
    assert "QODER_MISSING_KEY" not in env and "GITHUB_TOKEN" not in env


def test_backend_config_dir_rejects_repository_and_reserved_roots(tmp_path, monkeypatch):
    root, cfg = _config(tmp_path, monkeypatch, allow=True)
    for backend in ("qoder", "cursor"):
        for raw in (".", ".git/state", ".agentkit/state", tmp_path.anchor):
            with pytest.raises(AKError) as error:
                tasks._check_credential_policy(cfg, backend, {"config_dir": str(raw)}, root)
            assert error.value.code == "AK-CONFIG"


def test_backend_config_dir_rejects_tracked_source_directory(tmp_path, monkeypatch):
    root, cfg = _config(tmp_path, monkeypatch, allow=True)
    (root / "src").mkdir()
    (root / "src" / "calc.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "src")
    with pytest.raises(AKError) as error:
        tasks._check_credential_policy(cfg, "qoder", {"config_dir": "src"}, root)
    assert error.value.code == "AK-CONFIG" and "tracked" in error.value.what


def test_task_credential_revocation_blocks_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "selected-model-key")
    cfg = {"models": {"fake": {"base_url": "http://127.0.0.1:1/v1", "key_env": "FAKE_KEY"}}}
    task = {"id": "t123abc", "backend": "builtin", "model": "fake"}
    task.update(tasks._bind_task_credential(task, cfg, "builtin", "fake", tmp_path))
    binding_id = task["credential_binding_id"]
    assert credentials.is_binding_active(tmp_path, binding_id)
    credentials.revoke(tmp_path, target_kind="grant", target_id=task["credential_grant_id"],
                       reason_code="user_revoked", reason="revoke", now=10.0)
    error = tasks._ensure_task_credential(tmp_path, cfg, task)
    assert error and "revoked" in error
    rows = [json.loads(line) for line in (tmp_path / ".agentkit/state/credentials/audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["action"] == "denied" and rows[-1]["decision"] == "deny"


def test_schedule_rejects_revoked_credential_before_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "selected-model-key")
    cfg = {"models": {"fake": {"base_url": "http://127.0.0.1:1/v1", "key_env": "FAKE_KEY"}},
           "sandbox": {"mode": "off"}}
    (tmp_path / ".agentkit").mkdir()
    (tmp_path / ".agentkit/config.toml").write_text(
        '[sandbox]\nmode = "off"\n[models.fake]\nbase_url = "http://127.0.0.1:1/v1"\nkey_env = "FAKE_KEY"\n',
        encoding="utf-8")
    task = {"id": "t123abc", "backend": "builtin", "model": "fake", "status": "queued",
            "worktree": ".agentkit/worktrees/t123abc", "base": "HEAD", "branch": "agentkit/t123abc",
            "budget": {"tokens": 0, "usd": 0, "minutes": 5}, "hash": "h"}
    task.update(tasks._bind_task_credential(task, cfg, "builtin", "fake", tmp_path))
    tasks.save(tmp_path, task)
    credentials.revoke(tmp_path, target_kind="binding", target_id=task["credential_binding_id"],
                       reason_code="user_revoked", reason="revoke", now=10.0)
    monkeypatch.setattr(tasks, "selected_worker_env", lambda *_args: (_ for _ in ()).throw(AssertionError("injected")))
    tasks.schedule(tmp_path)
    failed = tasks.load(tmp_path, "t123abc")
    assert failed["status"] == "failed" and "revoked" in failed["reason"]


def test_task_credential_rebind_closes_previous_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "selected-model-key")
    monkeypatch.setenv("OTHER_KEY", "other-model-key")
    cfg = {"models": {"fake": {"base_url": "http://127.0.0.1:1/v1", "key_env": "FAKE_KEY"}}}
    task = {"id": "t123abc", "backend": "builtin", "model": "fake"}
    task.update(tasks._bind_task_credential(task, cfg, "builtin", "fake", tmp_path))
    first = task["credential_binding_id"]
    cfg["models"]["fake"]["key_env"] = "OTHER_KEY"
    task.update(tasks._bind_task_credential(task, cfg, "builtin", "fake", tmp_path))
    assert task["credential_binding_id"] != first
    assert credentials.load_binding(tmp_path, first)["status"] == "closed"
    assert credentials.is_binding_active(tmp_path, task["credential_binding_id"])
