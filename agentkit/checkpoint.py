# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-09 checkpoint: content-addressed snapshots under .agentkit/, no git needed.

Idea (snapshot before every mutation, cheap rollback) from jcode's agent-native VCS notes
and Claude Code-style checkpoints; implementation is our own.
"""

from __future__ import annotations

import difflib
import json
import os
import time
from pathlib import Path
from typing import Optional

from .core import AKError, Ctx, Result, dumps, fill_lines, room_for, sha256_bytes

KEEP = 200


def _objects(ctx: Ctx) -> Path:
    return ctx.state("objects")


def _cps(ctx: Ctx) -> Path:
    return ctx.state("checkpoints")


def _store_blob(ctx: Ctx, data: bytes) -> str:
    h = sha256_bytes(data)
    p = _objects(ctx) / h[:2] / h
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
    return h


def _blob(ctx: Ctx, h: str) -> bytes:
    return (_objects(ctx) / h[:2] / h).read_bytes()


def _next_id(ctx: Ctx) -> str:
    existing = sorted(p.name for p in _cps(ctx).iterdir() if p.name.startswith("cp-"))
    n = int(existing[-1].split("-")[1]) + 1 if existing else 1
    return f"cp-{n:05d}"


def snapshot(ctx: Ctx, rels: list, reason: str) -> str:
    """Snapshot the current state of repo-relative paths (missing files are recorded as absent)."""
    files = []
    for rel in sorted(set(rels)):
        p = ctx.root / rel
        if p.is_file():
            data = p.read_bytes()
            files.append({"path": rel, "exists": True, "sha": _store_blob(ctx, data), "mode": p.stat().st_mode & 0o777})
        else:
            files.append({"path": rel, "exists": False})
    cid = _next_id(ctx)
    d = _cps(ctx) / cid
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"id": cid, "time": time.time(), "reason": reason, "files": files}))
    _prune(ctx)
    return cid


def _prune(ctx: Ctx):
    ids = sorted(p for p in _cps(ctx).iterdir() if p.name.startswith("cp-"))
    for old in ids[:-KEEP]:
        for f in old.iterdir():
            f.unlink()
        old.rmdir()


def load(ctx: Ctx, cid: str) -> dict:
    p = _cps(ctx) / cid / "manifest.json"
    if not p.exists():
        raise AKError(f"no such checkpoint: {cid}", "id not found", ctx.next_cmd("checkpoint", "list"))
    return json.loads(p.read_text())


def all_ids(ctx: Ctx) -> list:
    return sorted(p.name for p in _cps(ctx).iterdir() if p.name.startswith("cp-"))


def restore(ctx: Ctx, man: dict) -> list:
    """Restore files of a manifest. Returns [{path, ok, sha}]. Verifies hash after write."""
    results = []
    for f in man["files"]:
        p = ctx.root / f["path"]
        if f["exists"]:
            data = _blob(ctx, f["sha"])
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".agentkit-tmp")
            tmp.write_bytes(data)
            os.chmod(tmp, f.get("mode", 0o644))
            os.replace(tmp, p)
            ok = sha256_bytes(p.read_bytes()) == f["sha"]
            results.append({"path": f["path"], "ok": ok, "sha": f["sha"][:16]})
        else:
            if p.exists():
                p.unlink()
            results.append({"path": f["path"], "ok": not p.exists(), "sha": None})
    return results


# ---------------------------------------------------------------- CLI


def cmd_checkpoint(ctx: Ctx, a) -> Result:
    sub = a.cp_cmd
    if sub == "create":
        rels = [ctx.rel(ctx.resolve(x)) for x in a.paths]
        if not rels:
            raise AKError("no paths given", "checkpoint create needs files", ctx.next_cmd("checkpoint", "create", "FILE"))
        return Result({"id": snapshot(ctx, rels, a.reason or "manual"), "files": rels})
    if sub == "list":
        ids = all_ids(ctx)[::-1]
        items = []
        for cid in ids:
            m = load(ctx, cid)
            t = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(m["time"]))
            paths = [f["path"] for f in m["files"]]
            more = f" +{len(paths) - 3}" if len(paths) > 3 else ""
            items.append(f"{cid} {t} {m['reason']} [{', '.join(paths[:3])}{more}]")
        items = items[a.offset:]
        sk = {"total": len(ids), "offset": a.offset, "checkpoints": []}
        taken, tr = fill_lines(items, room_for(ctx, sk, reserve=200))
        sk["checkpoints"] = taken
        return Result(sk, tr, ctx.with_args({"--offset": a.offset + len(taken)}) if tr else None)
    if sub == "diff":
        m = load(ctx, a.id)
        chunks = []
        for f in m["files"]:
            old = _blob(ctx, f["sha"]).decode("utf-8", "replace") if f["exists"] else ""
            p = ctx.root / f["path"]
            new = p.read_text(errors="replace") if p.is_file() else ""
            chunks += list(difflib.unified_diff(
                old.splitlines(), new.splitlines(), f"{a.id}/{f['path']}", f"now/{f['path']}", lineterm="", n=2))
        lines = chunks[a.offset:]
        sk = {"id": a.id, "changed": bool(chunks), "offset": a.offset, "diff": []}
        taken, tr = fill_lines(lines, room_for(ctx, sk, reserve=200))
        sk["diff"] = taken
        return Result(sk, tr, ctx.with_args({"--offset": a.offset + len(taken)}) if tr else None)
    if sub == "rollback":
        m = load(ctx, a.id)
        safety = snapshot(ctx, [f["path"] for f in m["files"]], f"before rollback to {a.id}")
        res = restore(ctx, m)
        from . import state

        for f in m["files"]:
            state.record(ctx, f["path"], f.get("sha"))
        ok = all(r["ok"] for r in res)
        return Result({"rolled_back_to": a.id, "hash_match": ok, "files": res, "undo": ctx.next_cmd("checkpoint", "rollback", safety)})
    raise AKError(f"unknown checkpoint subcommand {sub}", "", "agentkit checkpoint list")


_ = (dumps, Optional)
