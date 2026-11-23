# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-01 / baseline 2.3 `agentkit uninstall` and baseline 5.3 / C-03 log retention.

Everything AgentKit writes (baseline 5.3):
- the package itself (pip; M4: the binary)
- user config:  $XDG_CONFIG_HOME/agentkit   (default ~/.config/agentkit)
- user data:    $XDG_DATA_HOME/agentkit     (default ~/.local/share/agentkit): only `repos.txt`,
                the list of repositories that have a .agentkit/ directory, so uninstall can find them
- per repo:     <repo>/.agentkit/ (checkpoints, logs, memory, tasks, worktrees) + git worktrees
                registered under .agentkit/worktrees/

Uninstall removes all of it. It never touches user code or git history: task branches
(agentkit/<id>) are kept unless --delete-branches, and files under .agentkit/ that are tracked
by git (shared config / memory the team committed) are left in place and reported.
Without --yes it only prints the plan (an AI should show it to its human first).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .core import AKError, Ctx, Result

DEFAULT_RETAIN_DAYS = 7
PRUNE_EVERY_S = 6 * 3600


# ---------------------------------------------------------------- paths + repo registry


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")) / "agentkit"


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")) / "agentkit"


def _registrable(root: Path) -> bool:
    s = str(root)
    return "/.agentkit/worktrees/" not in s and "agentkit-taskverify-" not in s and "agentkit-verify" not in s


def register_repo(root: Path) -> None:
    """Remember a repo that got a .agentkit/ dir (never raises)."""
    try:
        root = Path(root).resolve()
        if not _registrable(root):
            return
        p = data_dir() / "repos.txt"
        if p.exists() and str(root) in p.read_text().splitlines():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(str(root) + "\n")
    except Exception:
        pass


def known_repos() -> list:
    p = data_dir() / "repos.txt"
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        ln = ln.strip()
        if ln and ln not in out:
            out.append(ln)
    return out


# ---------------------------------------------------------------- log retention (C-03, baseline 5.3)


def retain_days(cfg: dict) -> float:
    try:
        return float((cfg.get("logs") or {}).get("retain_days", DEFAULT_RETAIN_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_RETAIN_DAYS


def prune_logs(root: Path, cfg: dict, force: bool = False, now: float | None = None) -> dict:
    """Delete log files older than [logs] retain_days (default 7; 0 = keep forever) and drop
    expired lines from the shared events.jsonl. Runs at most every 6 h unless force."""
    d = Path(root) / ".agentkit" / "logs"
    out = {"removed": 0, "lines_dropped": 0}
    days = retain_days(cfg)
    if days <= 0 or not d.is_dir():
        return out
    now = time.time() if now is None else now
    stamp = d / ".pruned"
    try:
        if not force and stamp.exists() and now - stamp.stat().st_mtime < PRUNE_EVERY_S:
            return out
        stamp.touch()
    except OSError:
        return out
    cutoff = now - days * 86400
    for p in d.iterdir():
        if p.name.startswith(".") or not p.is_file():
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                out["removed"] += 1
            elif p.name == "events.jsonl":
                out["lines_dropped"] += _drop_old_lines(p, cutoff)
        except OSError:
            pass
    return out


def _drop_old_lines(p: Path, cutoff: float) -> int:
    with open(p, encoding="utf-8", errors="replace") as f:
        first = f.readline()
    try:
        if json.loads(first).get("ts", cutoff) >= cutoff:
            return 0  # lines are appended in time order: nothing expired
    except (ValueError, AttributeError):
        pass
    keep, dropped = [], 0
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                ts = json.loads(line).get("ts", cutoff)
            except (ValueError, AttributeError):
                ts = cutoff
            if ts >= cutoff:
                keep.append(line)
            else:
                dropped += 1
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text("".join(keep), encoding="utf-8")
    os.replace(tmp, p)
    return dropped


# ---------------------------------------------------------------- uninstall


def _git(root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120)


def _repo_plan(root: Path) -> dict:
    ak = root / ".agentkit"
    plan = {"repo": str(root), "remove": [], "worktrees": [], "branches": [], "kept_tracked": [], "active_tasks": []}
    if not ak.is_dir():
        return plan
    is_git = (root / ".git").exists() and shutil.which("git")
    if is_git:
        r = _git(root, "worktree", "list", "--porcelain")
        wt_root = str((ak / "worktrees").resolve())
        for ln in r.stdout.splitlines():
            if ln.startswith("worktree ") and ln[9:].startswith(wt_root):
                plan["worktrees"].append(ln[9:])
        b = _git(root, "branch", "--list", "agentkit/*", "--format=%(refname:short)")
        plan["branches"] = [x.strip() for x in b.stdout.splitlines() if x.strip()]
        t = _git(root, "ls-files", "--", ".agentkit")
        plan["kept_tracked"] = [x for x in t.stdout.splitlines() if x]
    try:
        from .tasks import ACTIVE, all_tasks, pid_alive

        for t in all_tasks(root):
            if t.get("status") in ACTIVE | {"starting"} and pid_alive(t.get("pid")):
                plan["active_tasks"].append(t["id"])
    except Exception:
        pass
    plan["remove"].append(str(ak) + ("/ (except git-tracked files)" if plan["kept_tracked"] else "/"))
    return plan


def _remove_repo_state(root: Path, plan: dict, delete_branches: bool) -> dict:
    ak = root / ".agentkit"
    done = {"repo": str(root), "worktrees_removed": 0, "branches_deleted": 0}
    for wt in plan["worktrees"]:
        _git(root, "worktree", "remove", "--force", wt)
        done["worktrees_removed"] += 1
    if plan["worktrees"] or (root / ".git").exists():
        _git(root, "worktree", "prune")
    if delete_branches:
        for b in plan["branches"]:
            if _git(root, "branch", "-D", b).returncode == 0:
                done["branches_deleted"] += 1
    tracked = {(root / p).resolve() for p in plan["kept_tracked"]}
    if not tracked:
        shutil.rmtree(ak, ignore_errors=True)
    else:
        for dirpath, dirnames, filenames in os.walk(ak, topdown=False):
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.resolve() not in tracked:
                    try:
                        p.unlink()
                    except OSError:
                        pass
            try:
                os.rmdir(dirpath)  # only succeeds when empty
            except OSError:
                pass
        done["kept_tracked"] = len(plan["kept_tracked"])
    return done


def _pip_installed() -> bool:
    r = subprocess.run([sys.executable, "-m", "pip", "show", "agentkit"], capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def cmd_uninstall(ctx: Ctx, a) -> Result:
    repos = []
    for r in [str(ctx.root)] + ([] if a.this_repo else known_repos()):
        p = Path(r)
        if r not in repos and (p / ".agentkit").is_dir():
            repos.append(r)
    plans = [_repo_plan(Path(r)) for r in repos]
    user_paths = [str(p) for p in (config_dir(), data_dir()) if p.exists()]
    keep_pkg = a.keep_package or a.this_repo
    package = None if keep_pkg else f"{sys.executable} -m pip uninstall -y agentkit"
    branches = sum(len(p["branches"]) for p in plans)
    summary = {
        "repos": [{k: v for k, v in p.items() if v and k != "kept_tracked"} | ({"kept_tracked": len(p["kept_tracked"])} if p["kept_tracked"] else {})
                  for p in plans][:10],
        "user_paths": user_paths,
        "package": package or "kept",
        "branches": f"{branches} agentkit/* branch(es) " + ("deleted" if a.delete_branches else "kept (your history; --delete-branches to drop)"),
        "never_touched": "your source files and git history",
    }
    if len(plans) > 10:
        summary["repos_more"] = len(plans) - 10
    active = [t for p in plans for t in p["active_tasks"]]
    if not a.yes:
        summary["dry_run"] = True
        nxt = ctx.next_cmd(*(["uninstall", "--yes"] + (["--this-repo"] if a.this_repo else [])
                             + (["--keep-package"] if a.keep_package else []) + (["--delete-branches"] if a.delete_branches else [])))
        summary["hint"] = "nothing deleted yet; show this plan to your human, then run next"
        return Result(summary, False, nxt)
    if active:
        raise AKError(f"{len(active)} task(s) still running: {' '.join(active[:5])}", "stop them before uninstalling",
                      "agentkit task cancel " + " ".join(active[:5]), code="AK-TASK-STATE")
    removed = [_remove_repo_state(Path(p["repo"]), p, a.delete_branches) for p in plans]
    for up in user_paths:
        shutil.rmtree(up, ignore_errors=True)
    pkg = "kept"
    if package:
        if _pip_installed():
            r = subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "agentkit"], capture_output=True, text=True, timeout=300)
            pkg = "removed" if r.returncode == 0 else f"pip uninstall failed: {r.stderr.strip()[-160:]}"
        else:
            pkg = "not installed via pip (remove the binary / checkout yourself)"
    return Result({"uninstalled": True, "repos": removed[:10], "user_paths_removed": user_paths, "package": pkg,
                   "never_touched": "your source files and git history"})
