# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-07 edit / patch / create / delete / rename.

All mutations go through one engine: validate everything in memory (unique match,
read-hash freshness, syntax), snapshot (M-09), then write; any write failure restores
the snapshot, so a multi-file patch is all-or-nothing.

"Reject the edit if it introduces a syntax error" follows SWE-agent's
tools/windowed_edit_linting (MIT) and Aider's linter (Apache-2.0); code is our own.
"""

from __future__ import annotations

import difflib
import json
import os
import sys
from pathlib import Path

from . import checkpoint, state
from .core import AKError, Ctx, Result, sha256_bytes
from .fsutil import read_text
from .symbols import syntax_errors


def _closest(text: str, old: str, k: int = 3) -> list:
    """Top-k windows of `text` most similar to `old` (non-overlapping), as {line, score, text}."""
    lines = text.splitlines()
    olines = old.splitlines() or [old]
    n = max(1, len(olines))
    if not lines:
        return []
    cands = []
    target = "\n".join(l.strip() for l in olines)
    first = olines[0].strip()
    step_lines = range(0, max(1, len(lines) - n + 1))
    quick = difflib.SequenceMatcher(None, "", target)
    for i in step_lines:
        window = "\n".join(l.strip() for l in lines[i:i + n])
        quick.set_seq1(window)
        if quick.real_quick_ratio() < 0.2:
            continue
        r = quick.ratio()
        if first and lines[i].strip() == first:
            r += 0.05
        cands.append((r, i))
    cands.sort(key=lambda x: -x[0])
    out, taken = [], []
    for r, i in cands:
        if any(abs(i - j) < n for j in taken):
            continue
        taken.append(i)
        snippet = "\n".join(lines[i:i + n])
        if len(snippet) > 300:
            snippet = snippet[:300] + "…"
        out.append({"line": i + 1, "similarity": round(min(r, 1.0), 2), "text": snippet})
        if len(out) >= k:
            break
    return out


def _occurrences(text: str, old: str) -> list:
    pos, out = 0, []
    while True:
        i = text.find(old, pos)
        if i < 0:
            return out
        out.append(i)
        pos = i + max(1, len(old))


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


class _Plan:
    def __init__(self):
        self.writes: dict = {}  # rel -> new text (None = delete)
        self.orig: dict = {}  # rel -> original text or None
        self.notes: list = []


def _current(ctx: Ctx, plan: _Plan, rel: str):
    if rel in plan.writes:
        return plan.writes[rel]
    p = ctx.root / rel
    if not p.exists():
        return None
    if p.is_dir():
        raise AKError(f"is a directory: {rel}", "", None)
    return read_text(p)


def _check_fresh(ctx: Ctx, rel: str):
    seen = state.last_seen(ctx, rel)
    if seen is None:
        return
    cur = sha256_bytes((ctx.root / rel).read_bytes())
    if cur != seen:
        raise AKError(
            f"{rel} changed since you last read it",
            "file hash differs from the one recorded at read/edit time; refusing to overwrite",
            ctx.next_cmd("read", rel),
        )


def _rel_checked(ctx: Ctx, path: str) -> str:
    p = ctx.resolve(path)
    if not ctx.inside_root(p):
        raise AKError(f"path outside repo: {path}", f"edits are limited to {ctx.root}", None)
    rel = ctx.rel(p)
    if rel.startswith(".agentkit/") or rel == ".agentkit" or rel.startswith(".git/"):
        raise AKError(f"refused: {rel}", "agentkit state and .git are not editable via edit", None)
    return rel


def plan_ops(ctx: Ctx, ops: list) -> _Plan:
    plan = _Plan()
    for n, op in enumerate(ops):
        kind = op.get("op", "replace")
        tag = f"op[{n}] {kind}"
        if kind == "replace":
            rel = _rel_checked(ctx, op["path"])
            cur = _current(ctx, plan, rel)
            if cur is None:
                raise AKError(f"{tag}: not found: {rel}", "file does not exist", ctx.next_cmd("create", rel))
            if rel not in plan.orig:
                _check_fresh(ctx, rel)
                plan.orig[rel] = cur
            old, new = op["old"], op.get("new", "")
            if old == "":
                raise AKError(f"{tag}: empty old text", "old must be a unique snippet of the file", ctx.next_cmd("read", rel))
            occ = _occurrences(cur, old)
            if not occ:
                cands = _closest(cur, old)
                raise AKError(
                    f"{tag}: old text not found in {rel}",
                    "0 matches; closest candidates are in data.candidates (check whitespace/indentation)",
                    ctx.next_cmd("read", f"{rel}:{cands[0]['line']}-{cands[0]['line'] + 20}") if cands else ctx.next_cmd("read", rel),
                    data={"path": rel, "matches": 0, "candidates": cands},
                )
            if len(occ) > 1 and not op.get("all"):
                cands = []
                for i in occ[:3]:
                    ln = _line_of(cur, i)
                    snippet = "\n".join(cur.splitlines()[ln - 1: ln - 1 + max(1, old.count("\n") + 1)])
                    cands.append({"line": ln, "similarity": 1.0, "text": snippet[:300]})
                raise AKError(
                    f"{tag}: old text matches {len(occ)} times in {rel}",
                    "must be unique: add surrounding lines to old, or pass all=true / --all",
                    ctx.next_cmd("read", f"{rel}:{cands[0]['line']}-{cands[0]['line'] + 10}"),
                    data={"path": rel, "matches": len(occ), "candidates": cands},
                )
            plan.writes[rel] = cur.replace(old, new) if op.get("all") else cur[:occ[0]] + new + cur[occ[0] + len(old):]
        elif kind == "create":
            rel = _rel_checked(ctx, op["path"])
            cur = _current(ctx, plan, rel)
            if cur is not None and not op.get("overwrite"):
                raise AKError(f"{tag}: exists: {rel}", "create refuses to overwrite", "use edit, or create --overwrite")
            if rel not in plan.orig:
                if cur is not None:
                    _check_fresh(ctx, rel)
                plan.orig[rel] = cur
            plan.writes[rel] = op.get("content", "")
        elif kind == "delete":
            rel = _rel_checked(ctx, op["path"])
            cur = _current(ctx, plan, rel)
            if cur is None:
                raise AKError(f"{tag}: not found: {rel}", "nothing to delete", None)
            if rel not in plan.orig:
                _check_fresh(ctx, rel)
                plan.orig[rel] = cur
            plan.writes[rel] = None
        elif kind == "rename":
            src = _rel_checked(ctx, op["path"])
            dst = _rel_checked(ctx, op["to"])
            cur = _current(ctx, plan, src)
            if cur is None:
                raise AKError(f"{tag}: not found: {src}", "nothing to rename", None)
            if _current(ctx, plan, dst) is not None:
                raise AKError(f"{tag}: destination exists: {dst}", "rename refuses to overwrite", None)
            for r in (src, dst):
                if r not in plan.orig:
                    if r == src:
                        _check_fresh(ctx, r)
                    plan.orig[r] = _current(ctx, plan, r)
            plan.writes[dst] = cur
            plan.writes[src] = None
        else:
            raise AKError(f"{tag}: unknown op", "op must be replace|create|delete|rename", None)
    return plan


def check_syntax(plan: _Plan, ctx: Ctx, force: bool):
    for rel, new in plan.writes.items():
        if new is None or force:
            continue
        issues, eng = syntax_errors(new, rel)
        if not issues:
            continue
        old = plan.orig.get(rel)
        old_issues = syntax_errors(old, rel)[0] if old is not None else []
        if old_issues and len(issues) <= len(old_issues):
            plan.notes.append(f"{rel}: file already had syntax errors before this edit ({eng}); not blocking")
            continue
        first = issues[0]
        lines = new.splitlines()
        ctx_lines = [f"{i}|{lines[i - 1]}" for i in range(max(1, first.line - 2), min(len(lines), first.line + 2) + 1)]
        raise AKError(
            f"syntax error in {rel} line {first.line}: {first.msg}",
            f"edit NOT applied (checked by {eng}); fix the new text and retry",
            None,
            data={"path": rel, "line": first.line, "col": first.col, "engine": eng, "context": ctx_lines,
                  "errors": [f"{i.line}:{i.col} {i.msg}" for i in issues[:5]]},
        )


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".agentkit-tmp")
    mode = p.stat().st_mode & 0o777 if p.exists() else None
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, p)


def apply(ctx: Ctx, ops: list, reason: str, force_syntax: bool = False, dry_run: bool = False) -> dict:
    plan = plan_ops(ctx, ops)
    check_syntax(plan, ctx, force_syntax)
    changed = sorted(plan.writes)
    if dry_run:
        return {"dry_run": True, "would_change": changed, "notes": plan.notes}
    cid = checkpoint.snapshot(ctx, changed, reason)
    try:
        for rel, new in plan.writes.items():
            p = ctx.root / rel
            if new is None:
                if p.exists():
                    p.unlink()
            else:
                _write(p, new)
    except BaseException as e:
        checkpoint.restore(ctx, checkpoint.load(ctx, cid))
        raise AKError("write failed; all files restored", f"{type(e).__name__}: {e}", ctx.next_cmd("checkpoint", "diff", cid))
    files = []
    for rel in changed:
        p = ctx.root / rel
        sha = sha256_bytes(p.read_bytes()) if p.exists() else None
        state.record(ctx, rel, sha)
        old = plan.orig.get(rel)
        new = plan.writes[rel]
        if old is None:
            files.append({"path": rel, "action": "created", "sha": sha[:16] if sha else None})
        elif new is None:
            files.append({"path": rel, "action": "deleted"})
        else:
            d = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0))
            plus = sum(1 for l in d if l.startswith("+") and not l.startswith("+++"))
            minus = sum(1 for l in d if l.startswith("-") and not l.startswith("---"))
            first = next((l for l in d if l.startswith("@@")), "")
            files.append({"path": rel, "action": "modified", "+": plus, "-": minus, "at": first, "sha": sha[:16]})
    out = {"checkpoint": cid, "files": files, "undo": ctx.next_cmd("checkpoint", "rollback", cid)}
    if plan.notes:
        out["notes"] = plan.notes
    return out


# ---------------------------------------------------------------- CLI handlers


def _text_arg(val, file_val, label):
    if file_val:
        return Path(file_val).read_text()
    if val is None:
        return None
    return val


def cmd_edit(ctx: Ctx, a) -> Result:
    old = _text_arg(a.old, a.old_file, "old")
    new = _text_arg(a.new, a.new_file, "new")
    if a.stdin:
        payload = json.loads(sys.stdin.read())
        old = payload.get("old", old)
        new = payload.get("new", new)
    if old is None or new is None:
        raise AKError("edit needs --old and --new", "missing text", 'agentkit edit FILE --old "a" --new "b"  (or --stdin JSON {"old","new"})')
    op = {"op": "replace", "path": a.path, "old": old, "new": new, "all": a.all}
    return Result(apply(ctx, [op], f"edit {a.path}", a.no_syntax_check, a.dry_run))


def cmd_patch(ctx: Ctx, a) -> Result:
    raw = Path(a.file).read_text() if a.file else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise AKError("patch input is not JSON", str(e), 'echo \'{"ops":[{"op":"replace","path":"a.py","old":"x","new":"y"}]}\' | agentkit patch')
    ops = payload["ops"] if isinstance(payload, dict) else payload
    return Result(apply(ctx, ops, f"patch ({len(ops)} ops)", a.no_syntax_check, a.dry_run))


def cmd_create(ctx: Ctx, a) -> Result:
    content = Path(a.content_file).read_text() if a.content_file else (sys.stdin.read() if a.content is None else a.content)
    op = {"op": "create", "path": a.path, "content": content, "overwrite": a.overwrite}
    return Result(apply(ctx, [op], f"create {a.path}", a.no_syntax_check))


def cmd_delete(ctx: Ctx, a) -> Result:
    return Result(apply(ctx, [{"op": "delete", "path": p} for p in a.paths], f"delete {' '.join(a.paths)}"))


def cmd_rename(ctx: Ctx, a) -> Result:
    return Result(apply(ctx, [{"op": "rename", "path": a.src, "to": a.dst}], f"rename {a.src} -> {a.dst}"))
