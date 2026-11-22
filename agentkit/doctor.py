# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-18 doctor and M-11 direct-mode `task` stub."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

from . import __version__
from .config import model_configured, user_config_path
from .core import AKError, Ctx, Result


def _check(name, ok, value, fix=None):
    d = {"name": name, "ok": bool(ok), "value": value}
    if not ok and fix:
        d["fix"] = fix
    return d


def cmd_doctor(ctx: Ctx, a) -> Result:
    checks = []
    checks.append(_check("version", True, f"agentkit {__version__}, python {sys.version.split()[0]}"))
    py_ok = sys.version_info >= (3, 10)
    checks.append(_check("python>=3.10", py_ok, sys.version.split()[0], "install Python 3.10+"))
    git = shutil.which("git")
    gv = ""
    if git:
        try:
            gv = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            git = None
    checks.append(_check("git", bool(git), gv or "not found",
                         "install git (apt install git / brew install git); without it verify uses a temp copy and checkpoints still work"))
    checks.append(_check("git_repo", ctx.is_git, str(ctx.root),
                         "optional: `git init` gives verify a clean worktree; direct mode works without it"))
    try:
        d = ctx.state("state")
        with tempfile.NamedTemporaryFile(dir=d, delete=True):
            pass
        w_ok, w_val = True, f"{ctx.root}/.agentkit writable"
    except OSError as e:
        w_ok, w_val = False, str(e)[:120]
    checks.append(_check("write_permission", w_ok, w_val, "run in a directory you own, or pass --root DIR"))
    du = shutil.disk_usage(str(ctx.root))
    free_mb = du.free // (1024 * 1024)
    checks.append(_check("disk_space", free_mb >= 500, f"{free_mb} MB free", "free at least 500 MB (logs/checkpoints live in .agentkit/)"))
    from .symbols import treesitter_available

    ts = treesitter_available()
    checks.append(_check("tree_sitter", ts, "available" if ts else "unavailable (regex/plain-text fallback)",
                         "pip install tree-sitter-language-pack (needs network once to fetch grammars)"))
    rg = shutil.which("rg")
    checks.append(_check("ripgrep", bool(rg), rg or "not found (built-in Python search used)",
                         "optional: install ripgrep for fast search on big repos"))
    model = model_configured(ctx.config)
    checks.append(_check("model_config", model, "configured" if model else "none -> direct mode",
                         f"optional: no model needed for direct mode; worker models arrive in M2 ({user_config_path()})"))
    if ctx.config.get("_error"):
        checks.append(_check("config_parse", False, ctx.config["_error"][:160], "fix the TOML syntax in .agentkit/config.toml"))
    required = {"python>=3.10", "write_permission", "disk_space", "config_parse"}
    ok = all(c["ok"] for c in checks if c["name"] in required)
    return Result({"healthy": ok, "mode": "dispatch" if model else "direct", "checks": checks})


def cmd_task(ctx: Ctx, a) -> Result:
    model = model_configured(ctx.config)
    if not model:
        raise AKError(
            "direct mode: no worker model configured",
            "task dispatch needs a worker model (arrives in M2); every tool works without one",
            "do it yourself: agentkit repo_map -> search/read -> edit -> run -> verify --check '<test cmd>'",
            data={"mode": "direct", "task_created": False},
        )
    raise AKError(
        "task system not implemented yet (M2)",
        "the M1 prototype ships direct-mode tools only",
        "use the tools directly: agentkit repo_map / search / read / edit / run / verify",
        data={"mode": "direct", "task_created": False},
    )


_ = os
