# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-09 checkpoint: content-addressed snapshots under .agentkit/, no git needed.

Idea (snapshot before every mutation, cheap rollback) from jcode's agent-native VCS notes
and Claude Code-style checkpoints; implementation is our own.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from pathlib import Path
from typing import Optional

from .core import AKError, Ctx, Result, atomic_write, dumps, file_lock, fill_lines, room_for, sha256_bytes

KEEP = 200
_CID_RE = re.compile(r"cp-[0-9]{5,}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")


def _objects(ctx: Ctx) -> Path:
    return ctx.state("objects")


def _cps(ctx: Ctx) -> Path:
    return ctx.state("checkpoints")


def _object_path(ctx: Ctx, h: str) -> Path:
    if not isinstance(h, str) or not _SHA_RE.fullmatch(h):
        raise AKError("invalid checkpoint object id", "blob hash must be a full sha256", None, code="AK-PATH")
    return ctx.state_path("objects", h[:2], h)


def _store_blob(ctx: Ctx, data: bytes) -> str:
    h = sha256_bytes(data)
    p = _object_path(ctx, h)
    if not p.exists():
        ctx.state_path("objects", h[:2], create=True)
        p = _object_path(ctx, h)
        atomic_write(p, data)
    elif not p.is_file():
        raise AKError("checkpoint object is not a file", str(p), None, code="AK-PATH")
    return h


def _blob(ctx: Ctx, h: str) -> bytes:
    p = _object_path(ctx, h)
    if not p.is_file():
        raise AKError("checkpoint object is missing", str(p), "agentkit checkpoint list", code="AK-PATH")
    return p.read_bytes()


def _checked_rel(ctx: Ctx, path: str) -> str:
    if not isinstance(path, str) or not path or Path(path).is_absolute() or Path(path).drive or ".." in Path(path).parts:
        raise AKError("invalid checkpoint path", "snapshot paths must be repo-relative files", None, code="AK-PATH")
    p = ctx.resolve(path)
    if p == ctx.root:
        raise AKError("invalid checkpoint path", "snapshot the repo root is not supported", None, code="AK-PATH")
    if p.exists() and not p.is_file():
        raise AKError(f"is a directory: {path}", "checkpoint paths must be files", None, code="AK-PATH")
    return ctx.rel(p)


def _validate_manifest(ctx: Ctx, man: dict) -> dict:
    if not isinstance(man, dict) or not isinstance(man.get("files"), list):
        raise AKError("invalid checkpoint manifest", "manifest must contain a files list", None, code="AK-PATH")
    files = []
    seen = set()
    for f in man["files"]:
        if not isinstance(f, dict):
            raise AKError("invalid checkpoint manifest", "file entries must be objects", None, code="AK-PATH")
        rel = _checked_rel(ctx, f.get("path"))
        if rel in seen:
            raise AKError("invalid checkpoint manifest", f"duplicate path: {rel}", None, code="AK-PATH")
        seen.add(rel)
        exists = f.get("exists") is True
        item = {"path": rel, "exists": exists}
        if exists:
            h = f.get("sha")
            if not isinstance(h, str) or not _SHA_RE.fullmatch(h):
                raise AKError("invalid checkpoint manifest", f"bad blob hash for {rel}", None, code="AK-PATH")
            mode = f.get("mode", 0o644)
            if not isinstance(mode, int) or not 0 <= mode <= 0o7777:
                raise AKError("invalid checkpoint manifest", f"bad mode for {rel}", None, code="AK-PATH")
            item.update(sha=h, mode=mode)
        files.append(item)
    return {**man, "files": files}


def _new_checkpoint_dir(ctx: Ctx) -> tuple[str, Path]:
    ctx.state_path("checkpoints", create=True)
    with file_lock(ctx.state_path("checkpoints", ".id.lock")):
        existing = all_ids(ctx)
        n = int(existing[-1].split("-")[1]) + 1 if existing else 1
        cid = f"cp-{n:05d}"
        d = ctx.state_path("checkpoints", cid)
        d.mkdir()
        return cid, d


def snapshot(ctx: Ctx, rels: list, reason: str) -> str:
    """Snapshot the current state of repo-relative paths (missing files are recorded as absent)."""
    files = []
    for rel in sorted({_checked_rel(ctx, rel) for rel in rels}):
        p = ctx.resolve(rel)
        if p.is_file():
            data = p.read_bytes()
            files.append({"path": rel, "exists": True, "sha": _store_blob(ctx, data), "mode": p.stat().st_mode & 0o777})
        else:
            files.append({"path": rel, "exists": False})
    cid, d = _new_checkpoint_dir(ctx)
    try:
        atomic_write(d / "manifest.json", json.dumps(
            {"id": cid, "time": time.time(), "reason": reason, "files": files}
        ).encode("utf-8"))
    except BaseException:
        try:
            (d / "manifest.json").unlink(missing_ok=True)
            d.rmdir()
        except OSError:
            pass
        raise
    _prune(ctx)
    return cid


def _prune(ctx: Ctx):
    ids = all_ids(ctx)
    for cid in ids[:-KEEP]:
        old = ctx.state_path("checkpoints", cid)
        for f in old.iterdir():
            f.unlink()
        old.rmdir()


def load(ctx: Ctx, cid: str) -> dict:
    if not isinstance(cid, str) or not _CID_RE.fullmatch(cid):
        raise AKError(f"no such checkpoint: {cid}", "invalid checkpoint id", ctx.next_cmd("checkpoint", "list"), code="AK-PATH")
    p = ctx.state_path("checkpoints", cid, "manifest.json")
    if not p.is_file():
        raise AKError(f"no such checkpoint: {cid}", "id not found", ctx.next_cmd("checkpoint", "list"))
    try:
        man = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise AKError(f"invalid checkpoint: {cid}", str(e), ctx.next_cmd("checkpoint", "list"), code="AK-PATH")
    if not isinstance(man, dict) or man.get("id") != cid:
        raise AKError(f"invalid checkpoint: {cid}", "manifest id does not match its directory", None, code="AK-PATH")
    return _validate_manifest(ctx, man)


def all_ids(ctx: Ctx) -> list:
    ids = []
    for p in _cps(ctx).iterdir():
        if not _CID_RE.fullmatch(p.name):
            continue
        try:
            safe = ctx.state_path("checkpoints", p.name)
        except AKError:
            continue
        if safe.is_dir():
            ids.append(p.name)
    return sorted(ids)


def restore(ctx: Ctx, man: dict) -> list:
    """Restore files of a manifest. Returns [{path, ok, sha}]. Verifies hash after write."""
    man = _validate_manifest(ctx, man)
    payloads = {f["path"]: _blob(ctx, f["sha"]) for f in man["files"] if f["exists"]}
    results = []
    for f in man["files"]:
        p = ctx.resolve(f["path"])
        if f["exists"]:
            data = payloads[f["path"]]
            p.parent.mkdir(parents=True, exist_ok=True)
            p = ctx.resolve(f["path"])
            atomic_write(p, data, f.get("mode", 0o644))
            ok = sha256_bytes(p.read_bytes()) == f["sha"]
            results.append({"path": f["path"], "ok": ok, "sha": f["sha"][:16]})
        else:
            if p.exists():
                if p.is_dir():
                    raise AKError(f"is a directory: {f['path']}", "refusing to remove a directory", None, code="AK-PATH")
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
            p = ctx.resolve(f["path"])
            new = p.read_bytes().decode("utf-8", "replace") if p.is_file() else ""
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
