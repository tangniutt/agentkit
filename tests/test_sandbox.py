# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Bubblewrap command construction, network policy and real isolation."""
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agentkit import sandbox, tasks


def test_sandbox_command_binds_only_worker_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: True)
    wt = tmp_path / "repo" / ".agentkit" / "worktrees" / "t123abc"
    task = tmp_path / "repo" / ".agentkit" / "tasks" / "t123abc"
    logs = tmp_path / "repo" / ".agentkit" / "logs"
    cmd = sandbox.wrap(["python", "-m", "agentkit", "_task-run", "t123abc"], wt, task, logs)
    assert cmd[:3] == ["bwrap", "--new-session", "--unshare-all"]
    assert "--die-with-parent" not in cmd
    assert "--share-net" in cmd
    assert cmd[-5:] == ["python", "-m", "agentkit", "_task-run", "t123abc"]
    for path in (wt, task, logs):
        i = cmd.index(str(path.resolve()))
        assert cmd[i - 1] == "--bind"
    assert "--ro-bind" in cmd and "/" in cmd


def test_sandbox_masks_home_and_recreates_bound_parents(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: True)
    home = tmp_path / "home"
    wt = home / "repo" / ".agentkit" / "worktrees" / "t123abc"
    task = home / "repo" / ".agentkit" / "tasks" / "t123abc"
    logs = home / "repo" / ".agentkit" / "logs"
    for path in (wt, task, logs):
        path.mkdir(parents=True)
    cmd = sandbox.wrap(["true"], wt, task, logs, masked_paths=[home])
    assert cmd[cmd.index(str(home.resolve())) - 1] == "--tmpfs"
    assert cmd.index(str(wt.resolve())) > cmd.index(str(home.resolve()))
    assert "--dir" in cmd
    assert str((home / "repo").resolve()) in cmd
    nested = sandbox.wrap(["true"], wt, task, logs, masked_paths=[home, home / ".config"])
    assert nested.count(str(home.resolve())) == 1
    repo = home / "repo"
    xdg = repo / ".config"
    xdg.mkdir(parents=True)
    remasked = sandbox.wrap(["true"], wt, task, logs, ro_paths=[repo], masked_paths=[home, xdg])
    assert remasked.count(str(xdg.resolve())) == 1
    assert remasked.index(str(xdg.resolve())) > len(remasked) - 1 - remasked[::-1].index(str(repo.resolve()))


@pytest.mark.skipif(not sandbox.available(), reason="bubblewrap is unavailable")
def test_sandbox_hides_host_home_credentials(tmp_path):
    home = tmp_path / "home"
    wt = tmp_path / "wt"
    task = tmp_path / "task"
    logs = tmp_path / "logs"
    for path in (home, wt, task, logs):
        path.mkdir()
    secret = home / ".ssh" / "id_ed25519"
    secret.parent.mkdir()
    secret.write_text("must-not-be-visible", encoding="utf-8")
    marker = wt / "visible.txt"
    marker.write_text("worktree-visible", encoding="utf-8")
    code = ("from pathlib import Path; "
            f"assert not Path({str(secret)!r}).exists(); "
            f"assert Path({str(marker)!r}).read_text() == 'worktree-visible'; "
            "print('masked')")
    result = subprocess.run(sandbox.wrap([sys.executable, "-c", code], wt, task, logs,
                                         masked_paths=[home]),
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    assert result.returncode == 0 and "masked" in result.stdout, result.stderr


def test_sandbox_network_policy_and_command(tmp_path, monkeypatch):
    assert sandbox.network_enabled({}) is True
    assert sandbox.network_enabled({"sandbox": {"network": False}}) is False
    assert sandbox.network_enabled({"sandbox": {"network": "false"}}) is False
    assert sandbox.network_enabled({"sandbox": {"network": ["api.example.com"]}}) is False
    assert sandbox.network_domains({"sandbox": {"network": ["API.Example.com.", "127.0.0.1"]}}) == [
        "api.example.com", "127.0.0.1"]
    assert sandbox.network_label({"sandbox": {"network": ["api.example.com"]}}) == "allowlist:api.example.com"
    assert sandbox.restricted_network({"sandbox": {"network": False}}) is True
    assert sandbox.restricted_network({"sandbox": {"network": ["api.example.com"]}}) is True
    assert sandbox.restricted_network({}) is False
    with pytest.raises(ValueError):
        sandbox.network_domains({"sandbox": {"network": ["https://example.com"]}})
    with pytest.raises(ValueError):
        sandbox.network_domains({"sandbox": {"network": "sometimes"}})
    monkeypatch.setattr(sandbox, "available", lambda: True)
    cmd = sandbox.wrap(["true"], tmp_path, tmp_path, tmp_path, network=False)
    assert "--unshare-all" in cmd and "--share-net" not in cmd
    source = tmp_path / "masked-source"
    state = tmp_path / "state"
    cmd = sandbox.wrap(["true"], tmp_path, tmp_path, tmp_path, ro_paths=[source], writable_paths=[state])
    assert cmd.index(str(source.resolve())) > cmd.index("--tmpfs")
    assert cmd.index(str(state.resolve())) > cmd.index(str(source.resolve()))
    assert cmd.index(str(tmp_path.resolve())) > cmd.index(str(state.resolve()))
    assert cmd[cmd.index(str(source.resolve())) - 1] == "--ro-bind"
    assert cmd[cmd.index(str(state.resolve())) - 1] == "--bind"


def test_sandbox_policy_and_unavailable(tmp_path, monkeypatch):
    assert sandbox.mode({}) == "auto"
    assert sandbox.mode({"sandbox": {"mode": "required"}}) == "required"
    assert sandbox.mode({"sandbox": {"mode": "off"}}) == "off"
    monkeypatch.setattr(sandbox, "available", lambda: False)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.wrap(["true"], tmp_path, tmp_path, tmp_path)


def test_restricted_network_never_falls_back_to_host(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    task_dir = root / ".agentkit" / "tasks" / "t123abc"
    worktree = root / ".agentkit" / "worktrees" / "t123abc"
    task_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (root / ".agentkit" / "config.toml").write_text('[sandbox]\nnetwork = false\n', encoding="utf-8")
    task = {"id": "t123abc", "goal": "fix", "checks": [], "allow": [], "model": "fake", "backend": "builtin",
            "status": "queued", "worktree": ".agentkit/worktrees/t123abc", "base": "HEAD", "branch": "agentkit/t123abc",
            "budget": {"tokens": 0, "usd": 0, "minutes": 5}, "hash": "h"}
    (task_dir / "task.json").write_text(json.dumps(task), encoding="utf-8")
    monkeypatch.setattr(sandbox, "available", lambda: False)
    tasks.schedule(root)
    result = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed" and result["error_code"] == "AK-TASK-STATE"
    assert "restricted" in result["reason"]


@pytest.mark.skipif(not sandbox.available(), reason="bubblewrap is unavailable")
def test_isolated_task_pauses_without_crashing(ak, repo, fake):
    from test_m2_tasks import create, project, tjson, wait

    project(repo, ak, fake)
    cfg = repo / ".agentkit" / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\n[sandbox]\nnetwork = false\n", encoding="utf-8")
    fake.add("[SANDBOX-OFFLINE]", [{"call": "finish", "args": {"summary": "done"}}])
    tid = create(ak, "inspect add [SANDBOX-OFFLINE]")
    status = wait(ak, tid, timeout=30)
    task = tjson(repo, tid)
    assert status["status"] == "paused" and "network" in status.get("why", "")
    assert task["sandbox"] == "bwrap" and task["sandbox_network"] == "isolated"
    assert task["error_code"] == "AK-MODEL-NETWORK"


@pytest.mark.skipif(not sandbox.available(), reason="bubblewrap is unavailable")
def test_allowlisted_task_uses_isolated_proxy(ak, repo, fake):
    from test_m2_tasks import FIX, create, project, tjson, wait

    project(repo, ak, fake, cfg='[sandbox]\nnetwork = ["127.0.0.1"]')
    fake.add("[SANDBOX-ALLOWLIST]", FIX)
    tid = create(ak, "fix add [SANDBOX-ALLOWLIST]")
    status = wait(ak, tid, timeout=60)
    assert status["status"] == "done" and status["verdict"] == "pass", status
    task = tjson(repo, tid)
    assert task["sandbox"] == "bwrap" and task["sandbox_network"] == "allowlist:127.0.0.1"
    assert task["sandbox_domains"] == ["127.0.0.1"]
    receipt = json.loads((repo / ".agentkit" / "tasks" / tid / "receipt.json").read_text())
    assert receipt["worker"]["sandbox_domains"] == ["127.0.0.1"]
    assert fake.requests_for("[SANDBOX-ALLOWLIST]")


@pytest.mark.skipif(not sandbox.available(), reason="bubblewrap is unavailable")
def test_non_allowlisted_model_endpoint_is_blocked(ak, repo, fake):
    from test_m2_tasks import create, project, tjson, wait

    project(repo, ak, fake, cfg='[sandbox]\nnetwork = ["example.invalid"]')
    fake.add("[SANDBOX-DENYLIST]", [{"call": "finish", "args": {"summary": "done"}}])
    tid = create(ak, "inspect add [SANDBOX-DENYLIST]")
    status = wait(ak, tid, timeout=30)
    task = tjson(repo, tid)
    assert status["status"] == "paused", status
    assert task["sandbox_network"] == "allowlist:example.invalid"
    assert task["error_code"] == "AK-MODEL-NETWORK"


@pytest.mark.skipif(not sandbox.available(), reason="bubblewrap is unavailable")
@pytest.mark.parametrize("network,connected", [(True, True), (False, False)])
def test_sandbox_network_namespace_controls_host_loopback(tmp_path, network, connected):
    wt, task, logs = (tmp_path / name for name in ("wt", "task", "logs"))
    for path in (wt, task, logs):
        path.mkdir()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(10)
    port = listener.getsockname()[1]
    code = (f"import socket; s=socket.create_connection(('127.0.0.1',{port}),2); "
            "print('connected'); s.close()")
    try:
        result = subprocess.run(sandbox.wrap([sys.executable, "-c", code], wt, task, logs, network=network),
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    finally:
        listener.close()
    assert (result.returncode == 0 and "connected" in result.stdout) is connected, result.stderr
