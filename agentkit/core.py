# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Shared context, the fixed JSON envelope and the output budget (M-02 / M-03)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

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

def _norm_path(path: Path) -> Path:
    return Path(os.path.normcase(str(path)))


def _within(path: Path, base: Path) -> bool:
    try:
        _norm_path(path).relative_to(_norm_path(base))
        return True
    except ValueError:
        return False


def _is_reparse_point(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # fail closed when the path cannot be inspected
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _path_has_reparse_point(path: Path, base: Path) -> bool:
    """Check every existing component below base, including Windows junctions."""
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        return True
    cur = base
    for part in parts:
        cur = cur / part
        if _is_reparse_point(cur):
            return True
        if not cur.exists():
            break
    return False


def atomic_write(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    """Atomically replace path using an exclusive, unpredictable temporary file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".agentkit-", suffix=".tmp", dir=str(path.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Cross-process read-modify-write lock (flock on POSIX, msvcrt on Windows)."""
    path = Path(path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "r+b") as f:
        if os.name == "posix":
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        else:
            import msvcrt

            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(b"\0")
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if os.name == "posix":
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            else:
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


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

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        self.cwd = Path(self.cwd).expanduser().resolve()

    @property
    def budget_chars(self) -> int:
        return self.budget * CHARS_PER_TOKEN

    @property
    def is_git(self) -> bool:
        return (self.root / ".git").exists()

    def state_path(self, *parts: str, create: bool = False) -> Path:
        """Safe path inside .agentkit; internal callers may use state, user paths may not."""
        base = self.root / STATE_DIR
        for part in parts:
            if not isinstance(part, str) or Path(part).is_absolute() or ".." in Path(part).parts:
                raise AKError("invalid agentkit state path", "state paths must stay inside .agentkit", None, code="AK-PATH")
        p = base.joinpath(*parts)
        try:
            base_real = base.resolve(strict=False)
            real = p.resolve(strict=False)
        except (OSError, RuntimeError) as e:
            raise AKError(f"cannot resolve agentkit state path: {p}", str(e), None, code="AK-PATH")
        if not _within(base_real, self.root) or not _within(real, base_real):
            raise AKError("agentkit state path escapes repo", f"{p} is not inside {base}", None, code="AK-PATH")
        if _path_has_reparse_point(p, self.root):
            raise AKError("refused: agentkit state path uses a link or junction", str(p), None, code="AK-PATH")
        if base.exists() and not base.is_dir():
            raise AKError("agentkit state path is not a directory", str(base), None, code="AK-PATH")
        if create:
            new_base = not base.exists()
            base.mkdir(parents=True, exist_ok=True)
            if new_base:
                (base / ".gitignore").write_text(_AGENTKIT_GITIGNORE)
            from .lifecycle import register_repo

            register_repo(self.root)  # so `agentkit uninstall` can find every repo it wrote to
            if p.exists() and not p.is_dir():
                raise AKError("agentkit state path is not a directory", str(p), None, code="AK-PATH")
            p.mkdir(parents=True, exist_ok=True)
            real = p.resolve(strict=False)
            if not _within(real, base_real) or _path_has_reparse_point(p, self.root):
                raise AKError("agentkit state path changed while creating it", str(p), None, code="AK-PATH")
        return real

    def state(self, *parts: str, create: bool = True) -> Path:
        return self.state_path(*parts, create=create)

    def resolve(self, path: str | os.PathLike, *, allow_secret: bool = False, allow_artifact: bool = False) -> Path:
        p = Path(os.path.expanduser(os.fspath(path)))
        if not p.is_absolute():
            p = self.cwd / p
        p = Path(os.path.abspath(str(p)))
        try:
            real = p.resolve(strict=False)
        except (OSError, RuntimeError) as e:
            raise AKError(f"cannot resolve path: {path}", str(e), "agentkit ls", code="AK-PATH")
        if not _within(p, self.root) or not _within(real, self.root):
            raise AKError(f"path outside repo root: {path}", f"paths are limited to {self.root}", "agentkit --root DIR ...", code="AK-PATH")
        if _path_has_reparse_point(p, self.root):
            raise AKError(f"refused: {path} uses a symbolic link or junction", "linked paths are not allowed across the repo boundary", None, code="AK-PATH")
        rel_parts = real.relative_to(self.root).parts
        folded = [part.casefold() if os.name == "nt" else part for part in rel_parts]
        if ".git" in folded:
            raise AKError(f"refused: {path}", ".git is protected", None, code="AK-PATH")
        if ".agentkit" in folded:
            artifact = len(folded) >= 2 and folded[0] == ".agentkit" and folded[1] in ("logs", "verify")
            if not (allow_artifact and artifact):
                raise AKError(f"refused: {path}", ".agentkit state is protected", None, code="AK-PATH")
        if not allow_secret:
            from .fsutil import is_secret_path

            if is_secret_path(real, root=self.root):
                raise AKError(f"refused: {path} looks like a secret file", "secret paths are blocked by default", None, code="AK-PATH")
        return real

    def rel(self, p: Path) -> str:
        try:
            return Path(os.path.normpath(str(p))).relative_to(self.root).as_posix()
        except ValueError:
            return str(p)

    def inside_root(self, p: str | os.PathLike, *, allow_secret: bool = False, allow_artifact: bool = False) -> bool:
        try:
            self.resolve(p, allow_secret=allow_secret, allow_artifact=allow_artifact)
            return True
        except AKError:
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
        r = Path(root).expanduser().resolve()
    else:
        r = find_git_root(cwd) or cwd
    ctx = Ctx(root=r, cwd=cwd, budget=DEFAULT_BUDGET, human=human, argv=argv)
    ctx.state_path()  # reject a pre-planted .agentkit link before reading config
    from .config import load_config

    cfg = load_config(r)
    b = budget if budget is not None else int(cfg.get("output", {}).get("budget", DEFAULT_BUDGET))
    ctx.budget = max(50, b)
    ctx.config = cfg
    return ctx


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
    if len(dumps(env)) > budget_chars:
        original_error = env.get("error") if isinstance(env.get("error"), dict) else {}
        ok = bool(env.get("ok"))
        err = None if ok else {"what": "output truncated", "why": "", "next": None,
                               "code": str(original_error.get("code") or "AK-USAGE")[:24],
                               "run": str(original_error.get("run") or ctx.run_id or "")[:16]}
        env = envelope(ok, None, err, True, "agentkit manual")
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
