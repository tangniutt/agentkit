# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""File walking that respects .gitignore, binary detection, secret-path detection."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from .core import AKError, Ctx

ALWAYS_SKIP_DIRS = {".git", ".hg", ".svn", ".agentkit"}
DEFAULT_SKIP_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".idea", ".gradle",
}
MAX_TEXT_BYTES_SNIFF = 8192

_SECRET_PATH = re.compile(
    r"(^|/)\.ssh(/|$)|(^|/)\.env(\.(?!example$|sample$|template$|dist$)[\w.-]+)?$|(^|/)id_(rsa|ed25519|ecdsa|dsa)$",
    re.IGNORECASE,
)
_SECRET_DIRS = {".ssh", "secret", "secrets"}


def _secret_name(name: str) -> bool:
    folded = name.casefold()
    return folded in _SECRET_DIRS or bool(_SECRET_PATH.fullmatch(folded))


def _secret_candidates(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        skipped = {name.casefold() for name in ALWAYS_SKIP_DIRS}
        dirnames[:] = [name for name in dirnames if name.casefold() not in skipped]
        for name in filenames:
            if _secret_name(name):
                yield Path(dirpath) / name


def is_secret_path(p: Path, root: Optional[Path] = None) -> bool:
    if any(_secret_name(part) for part in p.parts):
        return True
    # A hard link has no target path to resolve. Compare inode identity with
    # secret-named files in the repo so an innocuous alias cannot bypass policy.
    if root is None:
        return False
    try:
        st = p.stat()
        if not p.is_file() or getattr(st, "st_nlink", 1) <= 1:
            return False
        for candidate in _secret_candidates(Path(root)):
            try:
                if os.path.samestat(st, candidate.stat()):
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def is_binary_bytes(head: bytes) -> bool:
    if b"\x00" in head:
        return True
    try:
        head.decode("utf-8")
        return False
    except UnicodeDecodeError as e:
        # tolerate a multibyte char cut at the sniff boundary
        return e.start < len(head) - 4


def is_binary_file(p: Path) -> bool:
    with open(p, "rb") as f:
        return is_binary_bytes(f.read(MAX_TEXT_BYTES_SNIFF))


def read_text(p: Path) -> str:
    data = p.read_bytes()
    if is_binary_bytes(data[:MAX_TEXT_BYTES_SNIFF]):
        raise AKError(f"binary file: {p.name}", "refusing to read/edit binary content", None)
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- gitignore (non-git fallback)


class _Rule:
    def __init__(self, base: str, pat: str):
        self.neg = pat.startswith("!")
        if self.neg:
            pat = pat[1:]
        self.dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        self.anchored = "/" in pat
        pat = pat.lstrip("/")
        self.base = base
        self.pat = pat

    def match(self, rel: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        if self.base:
            if not rel.startswith(self.base + "/"):
                return False
            rel = rel[len(self.base) + 1:]
        if self.anchored:
            return _glob_match(rel, self.pat)
        return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], self.pat)


def _glob_match(rel: str, pat: str) -> bool:
    rx = _glob_to_re(pat)
    return re.fullmatch(rx, rel) is not None


def _glob_to_re(pat: str) -> str:
    i, out = 0, []
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("/**", i) and i + 3 == len(pat):
            out.append("/.*")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pat.find("]", i)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                out.append(pat[i:j + 1].replace("[!", "[^"))
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _load_rules(dirpath: Path, base: str) -> list:
    rules = []
    gi = dirpath / ".gitignore"
    if gi.is_file():
        for line in gi.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rules.append(_Rule(base, line))
    return rules


def _ignored(rules: list, rel: str, is_dir: bool) -> bool:
    ign = False
    for r in rules:
        if r.match(rel, is_dir):
            ign = not r.neg
    return ign


def _walk_python(root: Path, start: Path, no_ignore: bool) -> Iterable[str]:
    base_rel = "" if start == root else start.relative_to(root).as_posix()
    rules: list = [] if no_ignore else _load_rules(root, "")
    # rules from intermediate dirs between root and start
    if base_rel and not no_ignore:
        acc = ""
        for part in base_rel.split("/")[:-1]:
            acc = f"{acc}/{part}" if acc else part
            rules += _load_rules(root / acc, acc)
    stack = [(start, base_rel, rules)]
    while stack:
        d, drel, rules = stack.pop()
        if drel and not no_ignore and d != root:
            rules = rules + _load_rules(d, drel)
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        subdirs = []
        for e in entries:
            rel = f"{drel}/{e.name}" if drel else e.name
            is_dir = e.is_dir(follow_symlinks=False)
            if is_dir:
                if e.name in ALWAYS_SKIP_DIRS or (not no_ignore and e.name in DEFAULT_SKIP_DIRS):
                    continue
                if not no_ignore and _ignored(rules, rel, True):
                    continue
                subdirs.append((Path(e.path), rel, rules))
            else:
                if not no_ignore and _ignored(rules, rel, False):
                    continue
                yield rel
        stack.extend(reversed(subdirs))


def _git_files(root: Path, start: Path) -> Optional[list]:
    try:
        rel = "." if start == root else start.relative_to(root).as_posix()
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z", "--", rel],
            capture_output=True, timeout=60,
        )
        if out.returncode != 0:
            return None
        files = [f for f in out.stdout.decode("utf-8", "replace").split("\0") if f]
        files = [f for f in files if not f.startswith(".agentkit/") and (root / f).exists()]
        return sorted(set(files))
    except (OSError, subprocess.SubprocessError):
        return None


def list_files(ctx: Ctx, start: Optional[Path] = None, no_ignore: bool = False) -> list:
    """Repo-relative posix paths of safe files under start, respecting .gitignore."""
    start = ctx.resolve(start or ".")
    if not start.exists():
        raise AKError(f"not found: {start}", "search path does not exist", ctx.next_cmd("ls"))
    if start.is_file():
        candidates = [ctx.rel(start)]
    elif not start.is_dir():
        raise AKError(f"not a file or directory: {start}", "unsupported path type", None, code="AK-PATH")
    elif not no_ignore and ctx.is_git:
        candidates = _git_files(ctx.root, start)
        if candidates is None:
            candidates = list(_walk_python(ctx.root, start, no_ignore))
    else:
        candidates = list(_walk_python(ctx.root, start, no_ignore))
    safe = []
    for rel in candidates:
        try:
            p = ctx.resolve(rel)
        except AKError:
            continue  # never follow a planted link or expose a protected/secret alias
        if p.is_file():
            safe.append(ctx.rel(p))
    return sorted(set(safe))
