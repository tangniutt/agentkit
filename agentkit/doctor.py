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
from .config import model_configured
from .core import Ctx, Result


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
                         "optional: direct mode needs no model; for dispatch: agentkit model add NAME --base-url URL --key-env VAR"))
    from . import sandbox

    smode = sandbox.mode(ctx.config)
    savail = sandbox.available() if os.name == "posix" and smode != "off" else False
    try:
        network_label = sandbox.network_label(ctx.config)
        network_ok = True
    except ValueError as e:
        network_label, network_ok = str(e), False
    checks.append(_check("sandbox", smode == "off" or savail,
                         f"mode={smode}, bubblewrap={'available' if savail else 'unavailable'}, network={network_label}",
                         "install bubblewrap or set [sandbox] mode=required to refuse unsandboxed dispatch"))
    checks.append(_check("sandbox_network_policy", network_ok, network_label,
                         "use true, false, or an array of bare hostnames/IP addresses"))
    from . import codeintel
    from .backends import (codex_available, codex_config, cursor_available, cursor_config,
                           opencode_available, opencode_config, qoder_available, qoder_config, qoder_config_dir)

    if codex_config(ctx.config):
        cavail = codex_available(ctx.config)
        checks.append(_check("codex_backend", cavail, "configured" if cavail else "not found",
                             "set workers.codex.command or install Codex CLI"))
    if opencode_config(ctx.config):
        oavail = opencode_available(ctx.config)
        checks.append(_check("opencode_backend", oavail, "configured" if oavail else "not found",
                             "set workers.opencode.command or install OpenCode v1 CLI"))
    if qoder_config(ctx.config):
        qavail = qoder_available(ctx.config)
        qdir = qoder_config_dir(ctx.config, ctx.root)
        checks.append(_check("qoder_backend", qavail, "configured; dedicated login directory" if qavail else "not found",
                             f"set QODER_CONFIG_DIR={qdir} then run qodercli login"))
    if cursor_config(ctx.config):
        cavail = cursor_available(ctx.config)
        checks.append(_check("cursor_backend", cavail, "configured; CURSOR_API_KEY required" if cavail else "not found",
                             "set workers.cursor.command, install Cursor CLI, then set CURSOR_API_KEY"))
    ccfg = ctx.config.get("codeintel") or {}
    cmode = codeintel._backend(ccfg)
    checks.append(_check("codeintel", cmode != "xerj" or codeintel.xerj_available(ccfg),
                         f"backend={cmode}, xerj={'available' if codeintel.xerj_available(ccfg) else 'not found'}",
                         "optional: install xerj or set codeintel.xerj_path; builtin search remains available"))
    for language in sorted((ccfg.get("lsp") or {}).keys()):
        available = codeintel.lsp_available(ccfg, language)
        checks.append(_check(f"lsp:{language}", available, "configured" if available else "not found",
                             f"install the server or fix [codeintel.lsp.{language}] command"))
    from .models import ChatClient, ModelError, all_models

    for m in all_models(ctx.config).values():
        key_ok = (not m.key_env) or bool(os.environ.get(m.key_env))
        val = f"{m.model_id} @ {m.base_url} ({m.tier})" + ("" if key_ok else f"; ${m.key_env} not set")
        fix = f"export {m.key_env}=...  (the key is read from this variable; never put it in config)"
        if key_ok and getattr(a, "online", False):
            try:
                ChatClient(m, retries=0, capacity_retries=0).chat([{"role": "user", "content": "Reply: ok"}], timeout=30, max_tokens=8)
                val += "; reachable"
            except ModelError as e:
                key_ok, fix = False, f"agentkit model test {m.name}  ({e.kind})"
                val += f"; {e.kind}"
        elif key_ok:
            val += "; connectivity: run `agentkit doctor --online` or `agentkit model test`"
        checks.append(_check(f"model:{m.name}", key_ok, val, fix))
    if ctx.config.get("_error"):
        checks.append(_check("config_parse", False, ctx.config["_error"][:160], "fix the TOML syntax in .agentkit/config.toml"))
    required = {"python>=3.10", "write_permission", "disk_space", "config_parse", "sandbox_network_policy"}
    ok = all(c["ok"] for c in checks if c["name"] in required)
    external = bool(codex_config(ctx.config) or opencode_config(ctx.config) or qoder_config(ctx.config)
                    or cursor_config(ctx.config))
    dispatch = (bool(model) or external) and os.name == "posix"
    return Result({"healthy": ok, "mode": "dispatch" if dispatch else "direct", "checks": checks})


_ = os
