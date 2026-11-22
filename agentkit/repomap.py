# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-06 repo_map: directory overview + top symbols, ranked, within budget.

Ranking idea from Aider's repo map (aider/repomap.py, Apache-2.0): a definition matters
more when other files mention its name. We approximate Aider's PageRank with a cheap
cross-file identifier count. No Aider code was copied.
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict

from .core import Ctx, Result, dumps, room_for
from .fsutil import MAX_TEXT_BYTES_SNIFF, is_binary_bytes, list_files
from .symbols import extract_symbols, lang_for

MAX_PARSE_FILES = 3000
MAX_FILE_BYTES = 300_000
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOP = {"self", "this", "None", "True", "False", "return", "import", "from", "class", "def", "function",
         "const", "let", "var", "func", "type", "struct", "impl", "public", "private", "static", "new", "init",
         "main", "test", "String", "string", "int", "len", "get", "set"}


def cmd_repo_map(ctx: Ctx, a) -> Result:
    base = ctx.resolve(a.path or ".")
    files = list_files(ctx, base, no_ignore=False)
    # directory summary
    dir_count: Counter = Counter()
    lang_count: Counter = Counter()
    for f in files:
        parts = f.split("/")
        dir_count[parts[0] + "/" if len(parts) > 1 else "."] += 1
        if len(parts) > 2:
            dir_count["/".join(parts[:2]) + "/"] += 1
        lang_count[lang_for(f) or os.path.splitext(f)[1].lstrip(".") or "other"] += 1

    code_files = [f for f in files if lang_for(f)]
    code_files.sort(key=lambda f: (f.count("/"), "test" in f.lower(), f))
    parsed = code_files[:MAX_PARSE_FILES]
    defs: dict = {}
    mentions: dict = defaultdict(Counter)  # name -> Counter(file)
    engines: Counter = Counter()
    for f in parsed:
        p = ctx.root / f
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            raw = p.read_bytes()
        except OSError:
            continue
        if is_binary_bytes(raw[:MAX_TEXT_BYTES_SNIFF]):
            continue
        text = raw.decode("utf-8", "replace")
        syms, eng = extract_symbols(text, f)
        engines[eng] += 1
        defs[f] = syms
        for w in set(_IDENT.findall(text)):
            mentions[w][f] += 1

    def sym_score(f, s):
        if s.name in _STOP or s.name.startswith("_"):
            return 0.1
        others = sum(1 for g in mentions.get(s.name, {}) if g != f)
        return 1 + others + (0.5 if s.kind in ("class", "interface", "struct", "trait", "type") else 0)

    ranked_files = []
    for f, syms in defs.items():
        if not syms:
            continue
        scored = sorted(((sym_score(f, s), s) for s in syms), key=lambda x: (-x[0], x[1].start))
        fscore = sum(sc for sc, _ in scored[:10]) / (1 + 0.5 * ("test" in f.lower()))
        ranked_files.append((fscore, f, [s for _, s in scored]))
    ranked_files.sort(key=lambda x: (-x[0], x[1]))

    dirs = [f"{d} {n}" for d, n in sorted(dir_count.items(), key=lambda x: (-x[1], x[0]))]
    data = {
        "root": ctx.rel(base) or ".",
        "files": len(files),
        "langs": dict(lang_count.most_common(8)),
        "parsed": len(defs),
        "engine": engines.most_common(1)[0][0] if engines else "none",
        "dirs": [],
        "symbols": {},
    }
    if len(code_files) > MAX_PARSE_FILES:
        data["unparsed_code_files"] = len(code_files) - MAX_PARSE_FILES
    budget = ctx.budget_chars
    nxt_reserve = len(ctx.with_args({"--offset": 10**5})) + 60
    # 1) directories (up to ~25% of budget)
    dir_room = max(200, budget // 4)
    used = 0
    for d in dirs:
        c = len(dumps(d)) + 1
        if used + c > dir_room:
            break
        data["dirs"].append(d)
        used += c
    # 2) symbols, skipping `offset` files
    room = room_for(ctx, data, reserve=nxt_reserve)
    used = 0
    shown = 0
    truncated = False
    per_file = max(1, a.symbols_per_file)
    for _score, f, syms in ranked_files[a.offset:]:
        entries = [f"{s.kind} {s.qualname}:{s.start}" for s in syms[:per_file]]
        if len(syms) > per_file:
            entries.append(f"+{len(syms) - per_file} more")
        cost = len(dumps(f)) + len(dumps(entries)) + 2
        if used + cost > room:
            # try a slimmer entry for this file
            entries = entries[:2]
            cost = len(dumps(f)) + len(dumps(entries)) + 2
            if used + cost > room:
                truncated = True
                break
        data["symbols"][f] = entries
        used += cost
        shown += 1
    data["files_shown"] = shown
    data["files_with_symbols"] = len(ranked_files)
    nxt = ctx.with_args({"--offset": a.offset + shown}) if truncated else None
    return Result(data, truncated, nxt)
