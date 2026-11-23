# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Shared context, the fixed JSON envelope and the output budget (M-02 / M-03)."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .redact import redact

CHARS_PER_TOKEN = 4
DEFAULT_BUDGET = 2000
STATE_DIR = ".agentkit"

_AGENTKIT_GITIGNORE = """# written by agentkit: keep runtime state out of git, keep config + memory shareable
checkpoints/
objects/
logs/
state/
verify/
memory/index.sqlite*
tasks/
worktrees/
"""


class AKError(Exception):
    """A structured, expected failure: {what, why, next}."""

    def __init__(self, what: str, why: str = "", next: Optional[str] = None, data: Any = None, code: Optional[str] = None):
        super().__init__(what)
        self.code = code  # stable error code (C-03, docs/errors.md); classified from `what` when None
        self.what = what
        self.why = why
        self.next = next
        self.data = data


@dataclass
class Result:
    data: Any
    truncated: bool = False
    next: Optional[str] = None
    budget_chars: Optional[int] = None  # stricter cap for this command (verify 300 tok, memory brief 500 tok)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> Optional[str]:
    try:
        return sha256_bytes(p.read_bytes())
    except (FileNotFoundError, IsADirectoryError):
        return None


def find_git_root(start: Path) -> Optional[Path]:
    cur = start.resolve()
    for d in [cur, *cur.parents]:
        if (d / ".git").exists():
            return d
    return None


def git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass
class Ctx:
    root: Path
    cwd: Path
    budget: int = DEFAULT_BUDGET
    human: bool = False
    argv: list = field(default_factory=list)
    config: dict = field(default_factory=dict)
    run_id: str = ""

    @property
    def budget_chars(self) -> int:
        return self.budget * CHARS_PER_TOKEN

    @property
    def is_git(self) -> bool:
        return (self.root / ".git").exists()

    def state(self, *parts: str, create: bool = True) -> Path:
        base = self.root / STATE_DIR
        if create and not base.exists():
            base.mkdir(parents=True, exist_ok=True)
            (base / ".gitignore").write_text(_AGENTKIT_GITIGNORE)
            from .lifecycle import register_repo

            register_repo(self.root)  # so `agentkit uninstall` can find every repo it wrote to
        p = base.joinpath(*parts)
        if create:
            p.mkdir(parents=True, exist_ok=True)
        return p

    def resolve(self, path: str) -> Path:
        p = Path(os.path.expanduser(path))
        if not p.is_absolute():
            p = self.cwd / p
        return Path(os.path.normpath(str(p)))

    def rel(self, p: Path) -> str:
        try:
            return Path(os.path.normpath(str(p))).relative_to(self.root).as_posix()
        except ValueError:
            return str(p)

    def inside_root(self, p: Path) -> bool:
        try:
            Path(os.path.normpath(str(p))).relative_to(self.root)
            return True
        except ValueError:
            return False

    def next_cmd(self, *args: Any) -> str:
        return "agentkit " + shlex.join(str(a) for a in args)

    def with_args(self, replace: dict) -> str:
        """Rebuild the current command line with some --options replaced/added."""
        argv = list(self.argv)
        for opt, val in replace.items():
            if opt in argv:
                i = argv.index(opt)
                argv[i + 1] = str(val)
            else:
                argv += [opt, str(val)]
        return "agentkit " + shlex.join(argv)


def make_ctx(root: Optional[str], budget: Optional[int], human: bool, argv: list) -> Ctx:
    cwd = Path(os.getcwd()).resolve()
    if root:
        r = Path(root).resolve()
    else:
        r = find_git_root(cwd) or cwd
    from .config import load_config

    cfg = load_config(r)
    b = budget if budget is not None else int(cfg.get("output", {}).get("budget", DEFAULT_BUDGET))
    return Ctx(root=r, cwd=cwd, budget=max(50, b), human=human, argv=argv, config=cfg)


# ---------------------------------------------------------------- envelope + budget


def envelope(ok: bool, data: Any = None, error: Any = None, truncated: bool = False, next: Any = None) -> dict:
    return {"ok": ok, "data": data, "error": error, "truncated": truncated, "next": next}


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _measure(obj: Any, path=()):
    """Return (serialized_size, (best_size, best_path)) where best is the largest str/list slot."""
    if isinstance(obj, str):
        n = len(dumps(obj))
        return n, ((n, path) if path else (0, path))
    if isinstance(obj, list):
        total, best = 2 + max(0, len(obj) - 1), (0, path)
        for i, v in enumerate(obj):
            n, b = _measure(v, path + (i,))
            total += n
            if b[0] > best[0]:
                best = b
        if path and total > best[0]:
            best = (total, path)
        return total, best
    if isinstance(obj, dict):
        total, best = 2 + max(0, len(obj) - 1), (0, path)
        for k, v in obj.items():
            n, b = _measure(v, path + (k,))
            total += n + len(dumps(str(k))) + 1
            if b[0] > best[0]:
                best = b
        return total, best
    return len(dumps(obj)), (0, path)


def _largest_slot(obj: Any):
    return _measure(obj)[1]


def _get(obj, path):
    for k in path:
        obj = obj[k]
    return obj


def _set(obj, path, val):
    for k in path[:-1]:
        obj = obj[k]
    obj[path[-1]] = val


def fit(env: dict, budget_chars: int) -> dict:
    """Shrink the largest strings / lists in env until dumps(env) fits. Last-resort safety net."""
    env = json.loads(dumps(env))
    for _ in range(200):
        size = len(dumps(env))
        if size <= budget_chars:
            return env
        env["truncated"] = True
        over = size - budget_chars
        n, path = _largest_slot({"data": env.get("data"), "error": env.get("error")})
        if not path or n <= 2:
            break
        target = {"data": env.get("data"), "error": env.get("error")}
        val = _get(target, path)
        if isinstance(val, str):
            keep = max(0, len(val) - over - 20)
            new = val[:keep] + "…" if keep else ""
            if new == val:
                new = val[: max(0, len(val) // 2)]
        else:
            keep = max(0, len(val) - max(1, (len(val) * over) // max(n, 1) + 1))
            new = val[:keep]
        _set(target, path, new)
        env["data"], env["error"] = target["data"], target["error"]
    # Could not shrink structurally: replace data with a minimal stub.
    if len(dumps(env)) > budget_chars:
        env["data"] = None
        env["truncated"] = True
        if env.get("error"):
            old = env["error"]
            env["error"] = {"what": str(old.get("what", ""))[:80], "why": "", "next": None}
            for k in ("code", "run"):
                if k in old:
                    env["error"][k] = old[k]
    if len(dumps(env)) > budget_chars:
        env["next"] = (str(env.get("next") or ""))[: max(0, budget_chars - 80)] or None
    return env


def finalize(ctx: Ctx, env: dict, budget_chars: Optional[int] = None) -> dict:
    budget_chars = budget_chars if budget_chars is not None else ctx.budget_chars
    env = redact(env)
    if len(dumps(env)) > budget_chars:
        env = fit(env, budget_chars)
        if env.get("truncated") and not env.get("next"):
            env["next"] = ctx.with_args({"--budget": ctx.budget * 2})
            if len(dumps(env)) > budget_chars:
                env = fit(env, budget_chars)
    if env.get("truncated") and not env.get("next"):
        env["next"] = ctx.with_args({"--budget": ctx.budget * 2})
    return env


# ---------------------------------------------------------------- text-fill helper


def fill_lines(lines: list[str], room: int) -> tuple[list[str], bool]:
    """Take as many items as fit in `room` serialized chars. Returns (taken, truncated)."""
    out: list[str] = []
    used = 2
    for ln in lines:
        cost = len(dumps(ln)) + 1
        if used + cost > room:
            return out, True
        out.append(ln)
        used += cost
    return out, False


def room_for(ctx: Ctx, skeleton: dict, budget_chars: Optional[int] = None, reserve: int = 220) -> int:
    """Chars available for a variable payload once the skeleton envelope is accounted for."""
    budget_chars = budget_chars if budget_chars is not None else ctx.budget_chars
    return max(0, budget_chars - len(dumps(envelope(True, skeleton))) - reserve)


Handler = Callable[[Ctx, Any], Result]
