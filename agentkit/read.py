# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-04 read: `file`, `file:start-end`, `file#Symbol`. Budgeted, binary-safe, chunked.

Windowed reading idea from SWE-agent tools/windowed (MIT); implementation is our own.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import state
from .core import AKError, Ctx, Result, dumps, room_for, sha256_bytes
from .fsutil import MAX_TEXT_BYTES_SNIFF, is_binary_bytes
from .symbols import find_symbol

MAX_LINE_CHARS = 400
CHUNK_BYTES = 1 << 20  # files above 1MB are read in line chunks, never whole into the output

_SPEC = re.compile(r"^(?P<path>.+?)(?::(?P<start>\d+)(?:-(?P<end>\d+|\$)?)?|#(?P<sym>[\w.$:<>]+))?$")


def parse_spec(spec: str):
    m = _SPEC.match(spec)
    if not m:
        raise AKError("bad read spec", f"cannot parse {spec!r}", "agentkit read path/to/file.py:10-40")
    path = m.group("path")
    start = int(m.group("start")) if m.group("start") else None
    end_s = m.group("end")
    end = None if end_s in (None, "$") else int(end_s)
    if start is not None and m.group("end") is None and ":" in spec and not spec.endswith("-"):
        end = start  # `file:12` means just that line? Use a window of one line.
    return path, start, end, m.group("sym")


def cmd_read(ctx: Ctx, a) -> Result:
    path_s, start, end, sym = parse_spec(a.spec)
    p = ctx.resolve(path_s, allow_artifact=True, allow_secret=a.allow_secret_file)
    if not p.exists() and ":" in a.spec and "#" not in a.spec:
        candidate = ctx.resolve(a.spec, allow_artifact=True, allow_secret=a.allow_secret_file)
        if candidate.exists():
            p, start, end = candidate, None, None  # filename that contains ':'
    if not p.exists():
        raise AKError(f"not found: {path_s}", "file does not exist", "agentkit glob '**/" + Path(path_s).name + "'")
    if p.is_dir():
        raise AKError(f"is a directory: {path_s}", "read takes a file", ctx.next_cmd("ls", path_s))
    raw = p.read_bytes()
    if is_binary_bytes(raw[:MAX_TEXT_BYTES_SNIFF]):
        raise AKError(f"binary file: {path_s}", f"{len(raw)} bytes, not UTF-8 text", "use run with a dedicated tool (xxd, file)")
    sha = sha256_bytes(raw)
    rel = ctx.rel(p)
    state.record(ctx, rel, sha)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    big = len(raw) > CHUNK_BYTES

    symbol_info = None
    if sym:
        matches = find_symbol(text, str(p), sym)
        if not matches:
            from .symbols import extract_symbols

            syms, eng = extract_symbols(text, str(p))
            names = [s.qualname for s in syms][:30]
            raise AKError(
                f"symbol not found: {sym}",
                f"{len(syms)} symbols in {rel} (engine {eng})",
                ctx.next_cmd("read", f"{path_s}:1-80"),
                data={"symbols": names},
            )
        s = matches[0]
        start, end = s.start, s.end
        symbol_info = {"name": s.qualname, "kind": s.kind}
        if len(matches) > 1:
            symbol_info["other_matches"] = [f"{m.qualname}@{m.start}" for m in matches[1:5]]

    start = max(1, start or 1)
    if end is None:
        end = total
    end = min(end, total)
    if start > max(total, 1):
        raise AKError(f"start {start} beyond end of file", f"{rel} has {total} lines", ctx.next_cmd("read", f"{path_s}:1-{min(total, 100)}"))
    full_end = end
    if big and (a.spec == path_s):
        end = min(end, start + 2000 - 1)  # >1MB: chunk even before budget fitting

    skeleton = {"path": rel, "lines": [start, end], "total_lines": total, "sha": sha[:16], "content": ""}
    if symbol_info:
        skeleton["symbol"] = symbol_info
    room = room_for(ctx, skeleton, reserve=len(dumps(ctx.next_cmd("read", f"{path_s}:{total}-{total}"))) + 40)
    out = []
    used = 0
    last = start - 1
    cut_line = False
    for i in range(start, end + 1):
        ln = lines[i - 1]
        if len(ln) > MAX_LINE_CHARS and not getattr(a, "full_lines", False):
            ln = ln[:MAX_LINE_CHARS] + f"…[+{len(ln) - MAX_LINE_CHARS} chars]"
            cut_line = True
        piece = f"{i}|{ln}\n"
        cost = len(dumps(piece)) - 2
        if used + cost > room:
            if not out:  # first line alone is too big: hard-cut it
                piece = piece[: max(10, room // 2)] + "…\n"
                out.append(piece)
                last = i
            break
        out.append(piece)
        used += cost
        last = i
    skeleton["lines"] = [start, last]
    skeleton["content"] = "".join(out)
    if cut_line:
        skeleton["long_lines_cut"] = True
    truncated = last < full_end or cut_line
    nxt = None
    if truncated:
        if cut_line:
            nxt = ctx.next_cmd("read", f"{path_s}:{start}-{full_end}", "--full-lines",
                               "--budget", str(max(ctx.budget * 2, 2000)))
        else:
            nxt = ctx.next_cmd("read", f"{path_s}:{last + 1}-{full_end}")
    return Result(skeleton, truncated, nxt)
