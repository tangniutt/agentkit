# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-05 search / ls / glob. Respects .gitignore; results are `path:line:text`; budgeted with --offset paging.

Engine: ripgrep when on PATH (fast path for large repos), else a built-in Python scanner.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .core import AKError, Ctx, Result, dumps, fill_lines, room_for
from .fsutil import ALWAYS_SKIP_DIRS, DEFAULT_SKIP_DIRS, MAX_TEXT_BYTES_SNIFF, _glob_to_re, is_binary_bytes, is_secret_path, list_files

MAX_TEXT = 200
MAX_COLLECT = 20000


def _clip(s: str) -> str:
    s = s.rstrip("\n").strip()
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + "…"


def _engine() -> str:
    forced = os.environ.get("AGENTKIT_SEARCH_ENGINE")
    if forced in ("python", "rg"):
        return forced
    return "rg" if shutil.which("rg") else "python"


def _rg(ctx: Ctx, rx: str, base: Path, fixed: bool, icase: bool, glob: str | None, no_ignore: bool, limit: int):
    cmd = ["rg", "--json", "--sort", "path", "--no-config", "--no-require-git", "-g", "!.agentkit", "-g", "!.git"]
    if fixed:
        cmd.append("-F")
    if icase:
        cmd.append("-i")
    if no_ignore:
        cmd += ["--no-ignore", "--hidden"]
    else:
        cmd += ["--hidden"]
        for d in DEFAULT_SKIP_DIRS:
            cmd += ["-g", f"!{d}/"]
    if glob:
        cmd += ["-g", glob]
    cmd += ["-e", rx, "--", str(base)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(ctx.root))
    hits = []
    more = False
    assert proc.stdout is not None
    for raw in proc.stdout:
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("type") != "match":
            continue
        d = ev["data"]
        path = d["path"].get("text") or ""
        text = d["lines"].get("text") or ""
        rel = ctx.rel(Path(path) if os.path.isabs(path) else ctx.root / path)
        if is_secret_path(Path(rel)):
            continue
        hits.append((rel, d["line_number"], _clip(text)))
        if len(hits) >= limit:
            more = True
            break
    proc.kill()
    proc.wait()
    err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    if proc.returncode not in (0, 1, -9) and not hits and "regex parse error" in err:
        raise AKError("bad regex", err.strip().splitlines()[-1][:200], "retry with --fixed for a literal search")
    return hits, more


def _py(ctx: Ctx, rx: str, base: Path, fixed: bool, icase: bool, glob: str | None, no_ignore: bool, limit: int):
    try:
        pat = re.compile(re.escape(rx) if fixed else rx, re.IGNORECASE if icase else 0)
    except re.error as e:
        raise AKError("bad regex", str(e), "retry with --fixed for a literal search")
    grx = re.compile(_glob_to_re(glob)) if glob else None
    hits = []
    for rel in list_files(ctx, base, no_ignore=no_ignore):
        if grx and not (grx.fullmatch(rel) or grx.fullmatch(rel.rsplit("/", 1)[-1])):
            continue
        p = ctx.root / rel
        if is_secret_path(Path(rel)):
            continue
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if is_binary_bytes(data[:MAX_TEXT_BYTES_SNIFF]):
            continue
        for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if pat.search(line):
                hits.append((rel, i, _clip(line)))
                if len(hits) >= limit:
                    return hits, True
    return hits, False


def cmd_search(ctx: Ctx, a) -> Result:
    base = ctx.resolve(a.path) if a.path else ctx.root
    if not base.exists():
        raise AKError(f"not found: {a.path}", "search path does not exist", ctx.next_cmd("ls"))
    eng = _engine()
    fn = _rg if eng == "rg" else _py
    hits, capped = fn(ctx, a.pattern, base, a.fixed, a.ignore_case, a.glob, a.no_ignore, MAX_COLLECT)
    lines = [f"{p}:{n}:{t}" for p, n, t in hits][a.offset:]
    skeleton = {"pattern": a.pattern, "engine": eng, "total": len(hits) if not capped else f">={len(hits)}",
                "offset": a.offset, "matches": []}
    room = room_for(ctx, skeleton, reserve=len(ctx.with_args({"--offset": 10**6})) + 60)
    taken, trunc = fill_lines(lines, room)
    skeleton["matches"] = taken
    nxt = ctx.with_args({"--offset": a.offset + len(taken)}) if trunc else None
    if not hits:
        skeleton["hint"] = "no matches; try -i, --fixed, a shorter pattern, or --no-ignore"
    return Result(skeleton, trunc, nxt)


def cmd_ls(ctx: Ctx, a) -> Result:
    base = ctx.resolve(a.path or ".")
    if not base.is_dir():
        raise AKError(f"not a directory: {a.path}", "ls takes a directory", ctx.next_cmd("read", a.path or "."))
    files = list_files(ctx, base, no_ignore=a.no_ignore)
    brel = ctx.rel(base)
    prefix = "" if brel in ("", ".") else brel + "/"
    entries: dict = {}
    for f in files:
        if not f.startswith(prefix):
            continue
        rest = f[len(prefix):]
        if "/" in rest and not a.recursive:
            d = rest.split("/", 1)[0] + "/"
            entries[d] = entries.get(d, 0) + 1
        else:
            entries[rest] = None
    items = []
    for k in sorted(entries, key=lambda x: (not x.endswith("/"), x)):
        if k.endswith("/"):
            items.append(f"{k} ({entries[k]} files)")
        else:
            try:
                items.append(f"{k} {(base / k).stat().st_size}B")
            except OSError:
                items.append(k)
    items = items[a.offset:]
    skeleton = {"dir": brel or ".", "total": len(entries), "offset": a.offset, "entries": []}
    room = room_for(ctx, skeleton, reserve=len(ctx.with_args({"--offset": 10**6})) + 60)
    taken, trunc = fill_lines(items, room)
    skeleton["entries"] = taken
    return Result(skeleton, trunc, ctx.with_args({"--offset": a.offset + len(taken)}) if trunc else None)


def cmd_glob(ctx: Ctx, a) -> Result:
    base = ctx.resolve(a.path or ".")
    pat = a.pattern
    rx = re.compile(_glob_to_re(pat.lstrip("./")))
    files = list_files(ctx, base, no_ignore=a.no_ignore)
    brel = ctx.rel(base)
    prefix = "" if brel in ("", ".") else brel + "/"
    matched = []
    for f in files:
        rest = f[len(prefix):] if f.startswith(prefix) else f
        if rx.fullmatch(rest) or ("/" not in pat and rx.fullmatch(rest.rsplit("/", 1)[-1])):
            matched.append(f)
    matched.sort()
    items = matched[a.offset:]
    skeleton = {"pattern": pat, "total": len(matched), "offset": a.offset, "paths": []}
    room = room_for(ctx, skeleton, reserve=len(ctx.with_args({"--offset": 10**6})) + 60)
    taken, trunc = fill_lines(items, room)
    skeleton["paths"] = taken
    return Result(skeleton, trunc, ctx.with_args({"--offset": a.offset + len(taken)}) if trunc else None)


_ = (ALWAYS_SKIP_DIRS, dumps)
