# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-01 / baseline 2.3 uninstall, baseline 5.3 + C-03 seven-day log retention."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from conftest import git


def _home(ak, tmp):
    cfg, data = tmp / "xdg-config", tmp / "xdg-data"
    ak.env["XDG_CONFIG_HOME"], ak.env["XDG_DATA_HOME"] = str(cfg), str(data)
    return cfg, data


def _git_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "app.py").write_text("x = 1\n")
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return path


def test_uninstall_dry_run_then_removes_everything_but_code_and_history(ak, repo, tmp_path):
    cfg, data = _home(ak, tmp_path)
    _git_repo(repo)
    other = _git_repo(tmp_path / "other")
    (cfg / "agentkit").mkdir(parents=True)
    (cfg / "agentkit" / "config.toml").write_text("[models.x]\nbase_url='http://127.0.0.1:1'\n")
    # agentkit writes state in two repos (edit -> checkpoint, memory) and leaves a task-like worktree + branch
    assert ak("edit", "app.py", "--old", "x = 1", "--new", "x = 2")["ok"]
    assert ak("memory", "add", "t", "b", cwd=other)["ok"]
    wt = repo / ".agentkit" / "worktrees" / "tabc123"
    git(repo, "worktree", "add", "-q", "-b", "agentkit/tabc123", str(wt), "HEAD")
    assert (data / "agentkit" / "repos.txt").read_text().count("\n") == 2

    plan = ak("uninstall")
    assert plan["ok"] and plan["data"]["dry_run"] and plan["next"].startswith("agentkit uninstall --yes")
    assert len(plan["data"]["repos"]) == 2 and (repo / ".agentkit").exists() and wt.exists()
    assert str(cfg / "agentkit") in plan["data"]["user_paths"]

    r = ak("uninstall", "--yes", "--keep-package")
    assert r["ok"] and r["data"]["uninstalled"], r
    assert not (repo / ".agentkit").exists() and not (other / ".agentkit").exists()
    assert not (cfg / "agentkit").exists() and not (data / "agentkit").exists()
    wl = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert "tabc123" not in wl
    # user code (with the edit) and git history are untouched; the task branch is kept by default
    assert (repo / "app.py").read_text() == "x = 2\n"
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "agentkit/tabc123"], capture_output=True).returncode == 0
    assert subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout.count("\n") == 1


def test_uninstall_keeps_tracked_files_and_can_delete_branches(ak, repo, tmp_path):
    _home(ak, tmp_path)
    _git_repo(repo)
    assert ak("memory", "add", "shared", "team fact", "--id", "shared")["ok"]
    git(repo, "add", "-f", ".agentkit/memory")
    git(repo, "commit", "-qm", "share memory")
    git(repo, "branch", "agentkit/t000aaa")
    r = ak("uninstall", "--yes", "--this-repo", "--delete-branches")
    assert r["ok"], r
    assert r["data"]["package"] == "kept"
    assert (repo / ".agentkit" / "memory" / "shared.md").exists(), "git-tracked files are user content"
    assert not (repo / ".agentkit" / "logs").exists() and not (repo / ".agentkit" / "checkpoints").exists()
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "agentkit/t000aaa"], capture_output=True).returncode != 0
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout == ""


def test_uninstall_refuses_while_tasks_run(ak, repo, tmp_path):
    _home(ak, tmp_path)
    _git_repo(repo)
    d = repo / ".agentkit" / "tasks" / "t1a2b3c"
    d.mkdir(parents=True)
    # stands in for a live worker: pid_alive() also checks the cmdline for the _task-run marker
    q = subprocess.Popen(["python3", "-c", "import time; time.sleep(30)", "_task-run"])
    try:
        d.joinpath("task.json").write_text(json.dumps({"id": "t1a2b3c", "status": "running", "pid": q.pid, "created": time.time()}))
        r = ak("uninstall", "--yes", "--keep-package")
        assert not r["ok"] and r["error"]["code"] == "AK-TASK-STATE" and "task cancel" in r["error"]["next"]
        assert (repo / ".agentkit").exists()
    finally:
        q.kill()


def test_logs_older_than_retention_are_pruned(ak, repo):
    _git_repo(repo)
    assert ak("run", "echo hi")["ok"]
    logs = repo / ".agentkit" / "logs"
    old = time.time() - 8 * 86400
    stale = logs / "crash-r0101000000abcd.log"
    stale.write_text("old")
    os.utime(stale, (old, old))
    fresh = logs / "crash-r0101000001abcd.log"
    fresh.write_text("new")
    ev = logs / "events.jsonl"
    lines = ev.read_text().splitlines()
    ev.write_text(json.dumps({"ts": old, "run_id": "rold", "event": "command"}) + "\n" + "\n".join(lines) + "\n")
    (logs / ".pruned").unlink(missing_ok=True)
    assert ak("ls")["ok"]  # any command triggers the (rate-limited) prune
    assert not stale.exists() and fresh.exists()
    assert "rold" not in ev.read_text() and len(ev.read_text().splitlines()) >= len(lines)
    # rate limited: a second stale file survives until the next window
    stale.write_text("old")
    os.utime(stale, (old, old))
    assert ak("ls")["ok"] and stale.exists()


def test_log_retention_configurable_and_zero_keeps_forever(ak, repo):
    _git_repo(repo)
    from agentkit.lifecycle import prune_logs

    logs = repo / ".agentkit" / "logs"
    logs.mkdir(parents=True)
    f = logs / "x.stderr.log"
    f.write_text("x")
    t = time.time() - 3 * 86400
    os.utime(f, (t, t))
    assert prune_logs(repo, {"logs": {"retain_days": 0}}, force=True)["removed"] == 0 and f.exists()
    assert prune_logs(repo, {}, force=True)["removed"] == 0 and f.exists()
    assert prune_logs(repo, {"logs": {"retain_days": 2}}, force=True)["removed"] == 1 and not f.exists()


def test_pip_probe_uses_isolated_python(monkeypatch):
    from agentkit import lifecycle

    captured = []

    def run(args, **kwargs):
        captured.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    assert lifecycle._pip_installed() is True
    assert captured == [[sys.executable, "-I", "-m", "pip", "show", "agentkit"]]
