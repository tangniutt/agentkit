# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""C-08 command-aware output compression (OmniRoute / RTK style, own implementation).

Ideas from OmniRoute's RTK engine (MIT, github.com/diegosouzapw/OmniRoute,
open-sse/services/compression/engines/rtk) and RTK (Apache-2.0, github.com/rtk-ai/rtk):
filters are *data* (agentkit/filters/*.json) with inline test samples; a filter is chosen by
command, then by output shape; ANSI / progress noise is removed; repeated lines collapse with
counts; truncation keeps head, tail and every diagnostic line. The severity word list is
adapted from OmniRoute's severityVocabulary.ts (MIT, see NOTICE).

Safety (validate): the compressed text must keep every error / failure line, every fenced code
block, every protected line of the filter (summaries, changed files) and the paths / URLs /
versions on those lines. Otherwise the original is returned. The raw output always stays in
.agentkit/logs (redacted) and `agentkit logs RUN --raw` shows it.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

FILTER_DIR = Path(__file__).with_name("filters")

# adapted from OmniRoute severityVocabulary.ts (MIT): words that mark a line as diagnostic
SEVERITY = re.compile(
    r"(?i)(\berr!|\b(?:error|errors|fail|failed|failure|failures|failing|exception|traceback|assert\w*|panic(?:ked)?|"
    r"fatal|critical|severe|denied|refused|timed out|timeout|aborted|segmentation fault|oomkilled|out of memory|"
    r"unreachable|corrupt\w*|deadlock|ts\d{4})\b|✖|✗|✘|^E\s{2,}|^E\s+\S|^>\s{3,})"
)
WARNING = re.compile(r"(?i)(\bwarn(?:ing|ings)?\b|deprecat\w*)")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# generic progress noise (never applied to protected / diagnostic lines)
PROGRESS = [
    re.compile(r"^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷]\s"),
    re.compile(r"\[[#=>\-.\s]{6,}\]\s*\d{1,3}(?:\.\d+)?\s?%"),
    re.compile(r"^\s*\d{1,3}%\s*\|[█▉▊▋▌▍▎▏#\s]+\|"),
    re.compile(r"^\s*(?:[━─█▉▊▋▌▍▎▏░▒▓]\s*){10,}"),
    re.compile(r"^\s*(?:⸨|\[)[#░▒▓█\s.]+(?:⸩|\])\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏|/\\-]\s"),
]
PATHISH = re.compile(r"(?:[\w.-]*/)+[\w.-]+|\b[\w-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|c|h|cpp|rb|toml|json|ya?ml|md|txt|cfg|ini|lock)\b")
URL = re.compile(r"https?://[^\s)\]\"'>]+")
VERSION = re.compile(r"\bv?\d+\.\d+\.\d+(?:[-+.][\w.]+)?\b")
FENCE = re.compile(r"```.*?```", re.S)
OMIT = "… {n} lines omitted"

MIN_CHARS = 1200   # outputs smaller than this are left alone (nothing worth saving)
MIN_LINES = 25


# ---------------------------------------------------------------- filters (data)

@lru_cache(maxsize=1)
def load_filters() -> tuple:
    out = []
    for p in sorted(FILTER_DIR.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        d["_cmd"] = [re.compile(x) for x in d.get("commands", [])]
        d["_detect"] = [re.compile(x, re.M) for x in d.get("detect", [])]
        for k in ("drop", "protect", "dedup", "group"):
            d["_" + k] = [re.compile(x) for x in d.get(k, [])]
        out.append(d)
    out.sort(key=lambda d: -int(d.get("priority", 50)))
    return tuple(out)


def get_filter(fid: str) -> Optional[dict]:
    for f in load_filters():
        if f["id"] == fid:
            return f
    return None


def _segments(command: str) -> list:
    segs = []
    for s in re.split(r"&&|\|\||;|\|", command or ""):
        s = s.strip()
        s = re.sub(r"^(?:\w+=\S+\s+)+", "", s)  # FOO=1 cmd
        s = re.sub(r"^(?:cd\s+\S+|time|env|sudo|uv\s+run|poetry\s+run|npx|pnpm\s+exec)\s+", "", s)
        if s:
            segs.append(s)
    return segs


def select_filter(command: str, text: str) -> dict:
    fs = load_filters()
    segs = _segments(command)
    for f in fs:  # by command (priority order)
        if any(rx.search(seg) for rx in f["_cmd"] for seg in segs):
            return f
    for f in fs:  # by output shape
        if f["_detect"] and any(rx.search(text) for rx in f["_detect"]):
            return f
    return get_filter("generic")


# ---------------------------------------------------------------- core

def normalize(text: str) -> list:
    """Strip ANSI, resolve carriage-return redraws and backspaces; return lines."""
    text = ANSI.sub("", text)
    lines = []
    for ln in text.split("\n"):
        if "\r" in ln:
            parts = [p for p in ln.split("\r") if p.strip()]
            ln = parts[-1] if parts else ""
        while "\b" in ln:
            ln = re.sub(r"[^\b]\b", "", ln, count=1) if re.search(r"[^\b]\b", ln) else ln.replace("\b", "")
        lines.append(ln.rstrip())
    while lines and not lines[-1]:
        lines.pop()
    return lines


def is_severe(line: str) -> bool:
    return bool(SEVERITY.search(line))


def _protected(line: str, f: dict) -> bool:
    return is_severe(line) or any(rx.search(line) for rx in f["_protect"])


def compress(command: str, text: str, filt: Optional[dict] = None, force: bool = False) -> tuple:
    """Return (compressed_text, info). info: filter, raw_lines, lines, applied (bool), fallback (reason)."""
    f = filt or select_filter(command, text)
    raw_lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    info = {"filter": f["id"], "raw_lines": raw_lines, "lines": raw_lines, "applied": False}
    if not force and len(text) < MIN_CHARS and raw_lines < MIN_LINES:
        return text, info
    out = apply_filter(text, f)
    problems = validate(text, out, f)
    if problems:
        info["fallback"] = problems[0][:120]
        return text, info
    if len(out) >= len(text):
        return text, info
    info.update(applied=True, lines=out.count("\n") + (1 if out else 0))
    return out, info


def apply_filter(text: str, f: dict) -> str:
    lines = normalize(text)
    prot = [_protected(ln, f) for ln in lines]
    keep_blank = f.get("keep_blank", False)

    # 1. drop progress noise + filter drop patterns (never protected / warning lines)
    kept = []
    for ln, p in zip(lines, prot):
        if not p:
            if not ln.strip() and not keep_blank:
                if kept and not kept[-1][0].strip():
                    continue  # squeeze blank runs
            if any(rx.search(ln) for rx in PROGRESS) and not WARNING.search(ln):
                continue
            if any(rx.search(ln) for rx in f["_drop"]) and not WARNING.search(ln):
                continue
        kept.append((ln, p))

    # 2. global dedup: warnings, filter dedup patterns and (unless disabled) identical diagnostic lines
    dedup_sev = f.get("dedup_severity", True)
    counts: dict = {}
    for ln, p in kept:
        if ln.strip() and (WARNING.search(ln) or any(rx.search(ln) for rx in f["_dedup"]) or (p and dedup_sev)):
            counts[ln] = counts.get(ln, 0) + 1
    seen = set()
    stage = []
    for ln, p in kept:
        if ln in counts and counts[ln] > 1:
            if ln in seen:
                continue
            seen.add(ln)
            stage.append((f"{ln}  [×{counts[ln]}]", p))
        else:
            stage.append((ln, p))

    # 3. consecutive identical lines (outside diffs) -> one line with a count
    if f.get("collapse_repeats", True):
        coll = []
        for ln, p in stage:
            if coll and coll[-1][0] == ln and ln.strip():
                coll[-1][2] += 1
                continue
            coll.append([ln, p, 1])
        stage = [((f"{ln}  [×{n}]" if n >= 2 else ln), p) for ln, p, n in coll]

    # 4. runs of similar lines (filter group patterns) -> first line + count
    if f["_group"]:
        grouped, i = [], 0
        while i < len(stage):
            ln, p = stage[i]
            rx = next((r for r in f["_group"] if r.search(ln)), None) if not p else None
            if rx is None:
                grouped.append((ln, p))
                i += 1
                continue
            j, run = i, 0
            while j < len(stage) and not stage[j][1] and (rx.search(stage[j][0]) or not stage[j][0].strip()):
                run += 1 if stage[j][0].strip() else 0
                j += 1
            while j > i + 1 and not stage[j - 1][0].strip():
                j -= 1  # leave trailing blank lines alone
            if run >= 4:
                grouped.append((ln, False))
                grouped.append((f"… +{run - 1} similar lines", False))
            else:
                grouped.extend(stage[i:j])
            i = j
        stage = grouped

    # 5. per-file cap (diffs)
    fm = f.get("file_marker")
    per_file = int(f.get("per_file_lines", 0))
    if fm and per_file:
        rx = re.compile(fm)
        capped, n_in, hidden = [], 0, 0
        for ln, p in stage:
            if rx.search(ln):
                if hidden:
                    capped.append((OMIT.format(n=hidden) + " in this file", False))
                n_in, hidden = 0, 0
                capped.append((ln, True))
                continue
            n_in += 1
            if n_in > per_file and not p:
                hidden += 1
                continue
            capped.append((ln, p))
        if hidden:
            capped.append((OMIT.format(n=hidden) + " in this file", False))
        stage = capped

    # 6. head / tail / max_lines, keeping every protected line in the middle
    mx = int(f.get("max_lines", 0))
    if mx and len(stage) > mx:
        head, tail = int(f.get("head", mx // 3)), int(f.get("tail", mx // 2))
        out, hidden = list(stage[:head]), 0
        for ln, p in stage[head: len(stage) - tail]:
            if p:
                if hidden:
                    out.append((OMIT.format(n=hidden), False))
                    hidden = 0
                out.append((ln, p))
            else:
                hidden += 1
        if hidden:
            out.append((OMIT.format(n=hidden), False))
        out.extend(stage[len(stage) - tail:])
        stage = out

    if f.get("file_summary") and fm:
        stage = _diff_summary(lines, re.compile(fm)) + stage
    res = "\n".join(ln for ln, _ in stage).strip("\n")
    if not res.strip() and f.get("on_empty"):
        res = f["on_empty"]
    return res + ("\n" if res and text.endswith("\n") else "")


def _diff_summary(lines: list, marker) -> list:
    """One line naming every changed file with +/- counts, so a budget cut never hides which files changed."""
    files, cur = [], None
    for ln in lines:
        if marker.search(ln):
            m = re.search(r" b/(\S+)$", ln)
            cur = [m.group(1) if m else ln.split()[-1], 0, 0]
            files.append(cur)
        elif cur and ln.startswith("+") and not ln.startswith("+++"):
            cur[1] += 1
        elif cur and ln.startswith("-") and not ln.startswith("---"):
            cur[2] += 1
    if len(files) < 2:
        return []
    return [(f"[{len(files)} files] " + ", ".join(f"{p} (+{a} -{d})" for p, a, d in files), True)]


# ---------------------------------------------------------------- safety

def _important_lines(text: str, f: dict) -> list:
    return [ln for ln in normalize(text) if ln.strip() and _protected(ln, f)]


def validate(original: str, compressed: str, f: Optional[dict] = None) -> list:
    """Problems that make `compressed` unsafe to show instead of `original` (empty list = ok)."""
    f = f or get_filter("generic")
    probs = []
    if original.strip() and not compressed.strip():
        return ["compressed output is empty"]
    for block in (FENCE.findall(ANSI.sub("", original)) if f.get("check_fences", True) else []):
        if block not in compressed:
            probs.append("code block changed or missing")
            break
    for ln in _important_lines(original, f):
        core = ln.strip()
        if core not in compressed:
            probs.append(f"important line missing: {core[:80]}")
            break
        for rx, what in ((PATHISH, "path"), (URL, "URL"), (VERSION, "version")):
            for tok in rx.findall(ln):
                if tok not in compressed:
                    probs.append(f"{what} missing: {tok[:80]}")
                    break
    return probs


# ---------------------------------------------------------------- filter self-tests (verify gate)

def verify_filters() -> list:
    """Run every filter's inline samples. Returns a list of failures (empty = all good)."""
    fails = []
    for f in load_filters():
        if not f.get("tests"):
            fails.append(f"{f['id']}: no inline test samples")
        for t in f.get("tests", []):
            name = f"{f['id']}/{t.get('name', '?')}"
            if t.get("command") is not None and select_filter(t["command"], t["input"])["id"] != f["id"]:
                fails.append(f"{name}: command {t['command']!r} selects {select_filter(t['command'], t['input'])['id']}")
            out, info = compress(t.get("command", ""), t["input"], filt=f, force=True)
            if t.get("compresses", True) and not info["applied"]:
                fails.append(f"{name}: not compressed ({info.get('fallback', 'no saving')})")
            for s in t.get("expect", []):
                if s not in out:
                    fails.append(f"{name}: expected {s!r} in output")
            for s in t.get("absent", []):
                if s in out:
                    fails.append(f"{name}: {s!r} should be removed")
    return fails


def best_why(text: str, limit: int = 140) -> str:
    """One line explaining a failed check: the last summary / diagnostic line, else the last line."""
    lines = [ln for ln in normalize(text) if ln.strip() and not any(rx.search(ln) for rx in PROGRESS)]
    if not lines:
        return ""
    summ = re.compile(r"(?i)\b\d+\s+(?:failed|passed|errors?|problems?)\b|^(?:FAILED|FAIL|ERROR)\b|\berror\b")
    for ln in reversed(lines[-60:]):
        if summ.search(ln):
            return ln.strip()[-limit:]
    return lines[-1].strip()[-limit:]
