# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-21 project memory (basic): markdown files + front matter under .agentkit/memory/.

Design follows docs/feature-sources.md section D (ideas from Qoder's public docs; no code).
Front matter values are JSON (a valid subset of YAML flow style) so parsing needs no YAML lib.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

from .core import AKError, Ctx, Result, atomic_write, dumps, fill_lines, room_for, sha256_bytes
from .redact import redact_text
from .symbols import find_symbol

CATEGORIES = ("project-info", "conventions", "lessons", "tools-models", "preferences")
BRIEF_TOKENS = 500
_ID_RE = re.compile(r"[\w-]{1,80}\Z", re.UNICODE)


def _dir(ctx: Ctx) -> Path:
    return ctx.state("memory")


def slugify(s: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", s.strip().lower(), flags=re.UNICODE).strip("-")
    return (s or "memory")[:60]


def _parse(p: Path) -> dict:
    text = p.read_text(encoding="utf-8")
    meta: dict = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end > 0:
            for line in text[4:end].splitlines():
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                v = v.strip()
                try:
                    meta[k.strip()] = json.loads(v)
                except ValueError:
                    meta[k.strip()] = v
            body = text[end + 4:].lstrip("\n")
    meta.setdefault("id", p.stem)
    meta["body"] = body
    return meta


def _memory_path(ctx: Ctx, mid: str) -> Path:
    if not isinstance(mid, str) or not _ID_RE.fullmatch(mid) or mid in (".", ".."):
        raise AKError(f"invalid memory id '{str(mid)[:40]}'", "use letters, numbers, underscores or hyphens only", None, code="AK-MEMORY")
    return _dir(ctx) / f"{mid}.md"


def _write(ctx: Ctx, m: dict):
    keys = ["id", "title", "category", "pinned", "related", "anchors", "source", "created", "updated"]
    lines = ["---"]
    for k in keys:
        if k in m and m[k] is not None:
            lines.append(f"{k}: {json.dumps(m[k], ensure_ascii=False)}")
    lines.append("---")
    text = "\n".join(lines) + "\n" + m.get("body", "").rstrip() + "\n"
    atomic_write(_memory_path(ctx, m["id"]), text.encode("utf-8"))


def load_all(ctx: Ctx) -> list:
    return [_parse(p) for p in sorted(_dir(ctx).glob("*.md"))]


def get(ctx: Ctx, mid: str) -> dict:
    p = _memory_path(ctx, mid)
    if not p.exists():
        raise AKError(f"no memory '{mid}'", "id not found", ctx.next_cmd("memory", "list"))
    return _parse(p)


# ---------------------------------------------------------------- anchors


def _anchor_hash(ctx: Ctx, path: str, symbol: Optional[str]) -> Optional[str]:
    p = ctx.root / path
    if not p.is_file():
        return None
    raw = p.read_bytes()
    if not symbol:
        return sha256_bytes(raw)[:16]
    text = raw.decode("utf-8", "replace")
    hits = find_symbol(text, path, symbol)
    if not hits:
        return None
    s = hits[0]
    seg = "\n".join(text.splitlines()[s.start - 1:s.end])
    return sha256_bytes(seg.encode())[:16]


def make_anchor(ctx: Ctx, spec: str) -> dict:
    path, _, sym = spec.partition("#")
    rel = ctx.rel(ctx.resolve(path))
    h = _anchor_hash(ctx, rel, sym or None)
    if h is None:
        raise AKError(f"anchor not found: {spec}", "file or symbol does not exist", ctx.next_cmd("glob", Path(path).name))
    a = {"path": rel, "hash": h}
    if sym:
        a["symbol"] = sym
    return a


def stale_anchors(ctx: Ctx, m: dict) -> list:
    out = []
    for a in m.get("anchors") or []:
        h = _anchor_hash(ctx, a["path"], a.get("symbol"))
        if h != a.get("hash"):
            out.append(a["path"] + (f"#{a['symbol']}" if a.get("symbol") else "") + (" (missing)" if h is None else ""))
    return out


# ---------------------------------------------------------------- write paths


def add(ctx: Ctx, title: str, body: str, category: str, pinned=False, related=None, anchors=None,
        mid: Optional[str] = None, source="host") -> dict:
    if category not in CATEGORIES:
        raise AKError(f"bad category: {category}", f"use one of {', '.join(CATEGORIES)}", None)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    mid = mid or slugify(title)
    existing = _memory_path(ctx, mid)
    created = now
    if existing.exists():
        old = _parse(existing)
        if old.get("title") == title or mid == old.get("id"):
            created = old.get("created", now)  # same topic: newer memory replaces older (conflict rule)
    m = {
        "id": mid, "title": redact_text(title), "category": category, "pinned": bool(pinned),
        "related": related or [], "anchors": [make_anchor(ctx, a) for a in (anchors or [])],
        "source": source, "created": created, "updated": now, "body": redact_text(body),
    }
    _write(ctx, m)
    return m


def upsert_auto(ctx: Ctx, mid: str, title: str, category: str, body: str):
    add(ctx, title, body, category, pinned=True, mid=mid, source="auto")


# ---------------------------------------------------------------- search


def _fts_search(ctx: Ctx, mems: list, query: str) -> Optional[list]:
    try:
        db = sqlite3.connect(str(_dir(ctx) / "index.sqlite"))
        db.execute("DROP TABLE IF EXISTS mem")
        db.execute("CREATE VIRTUAL TABLE mem USING fts5(id UNINDEXED, title, body, category)")
        db.executemany("INSERT INTO mem VALUES (?,?,?,?)",
                       [(m["id"], m.get("title", ""), m.get("body", ""), m.get("category", "")) for m in mems])
        words = [w for w in re.findall(r"\w+", query, flags=re.UNICODE) if w]
        if not words:
            return []
        q = " OR ".join('"' + w.replace('"', "") + '"*' for w in words)
        rows = db.execute(
            "SELECT id, snippet(mem, 2, '[', ']', '…', 12) FROM mem WHERE mem MATCH ? ORDER BY bm25(mem) LIMIT 50", (q,)
        ).fetchall()
        db.commit()
        db.close()
        return rows
    except sqlite3.Error:
        return None


def _grep_search(mems: list, query: str) -> list:
    words = [w.lower() for w in re.findall(r"\w+", query) if w]
    scored = []
    for m in mems:
        hay = (m.get("title", "") + " " + m.get("body", "")).lower()
        sc = sum(hay.count(w) for w in words)
        if sc:
            i = min((hay.find(w) for w in words if w in hay), default=0)
            scored.append((sc, m["id"], m["body"][max(0, i - 40): i + 80].replace("\n", " ")))
    scored.sort(key=lambda x: -x[0])
    return [(mid, snip) for _, mid, snip in scored]


def _line(ctx: Ctx, m: dict) -> str:
    st = stale_anchors(ctx, m)
    flags = (" pinned" if m.get("pinned") else "") + (" maybe_stale" if st else "")
    return f"{m['id']}: {m.get('title', '')} [{m.get('category', '')}{flags}]"


def _agents_md(ctx: Ctx) -> Optional[dict]:
    for name in ("AGENTS.md", "CLAUDE.md", ".agents.md"):
        p = ctx.root / name
        if p.is_file():
            text = redact_text(p.read_text(errors="replace"))
            return {"path": name, "lines": text.count("\n") + 1, "text": text}
    return None


def cmd_memory(ctx: Ctx, a) -> Result:
    sub = a.mem_cmd
    if sub == "add":
        body = a.body if a.body is not None else sys.stdin.read()
        m = add(ctx, a.title, body, a.category, a.pin, a.related or [], a.anchor or [], a.id)
        return Result({"id": m["id"], "category": m["category"], "pinned": m["pinned"], "anchors": m["anchors"]})
    if sub == "forget":
        p = _memory_path(ctx, a.id)
        if not p.exists():
            raise AKError(f"no memory '{a.id}'", "id not found", ctx.next_cmd("memory", "list"))
        p.unlink()
        return Result({"forgotten": a.id})
    if sub == "show":
        m = get(ctx, a.id)
        st = stale_anchors(ctx, m)
        data = {k: m.get(k) for k in ("id", "title", "category", "pinned", "related", "anchors", "updated")}
        data["maybe_stale"] = bool(st)
        if st:
            data["stale_anchors"] = st
        rel_titles = []
        for r in m.get("related") or []:
            try:
                rel_titles.append(f"{r}: {get(ctx, r).get('title', '')}")
            except AKError:
                pass
        data["related_titles"] = rel_titles
        data["body"] = ""
        room = room_for(ctx, data, reserve=120)
        body = m["body"][a.offset:]
        trunc = len(dumps(body)) > room
        if trunc:
            cut = room
            while cut > 0 and len(dumps(body[:cut])) > room:
                cut = int(cut * 0.9)
            body = body[:cut]
        data["body"] = body
        return Result(data, trunc, ctx.with_args({"--offset": a.offset + len(body)}) if trunc else None)
    if sub == "list":
        mems = [m for m in load_all(ctx) if not a.category or m.get("category") == a.category]
        items = [_line(ctx, m) for m in mems][a.offset:]
        sk = {"total": len(mems), "offset": a.offset, "memories": []}
        taken, tr = fill_lines(items, room_for(ctx, sk, reserve=200))
        sk["memories"] = taken
        return Result(sk, tr, ctx.with_args({"--offset": a.offset + len(taken)}) if tr else None)
    if sub == "search":
        mems = load_all(ctx)
        rows = _fts_search(ctx, mems, a.query)
        engine = "fts5"
        if rows is None:
            rows, engine = _grep_search(mems, a.query), "grep"
        by_id = {m["id"]: m for m in mems}
        items = []
        for mid, snip in rows:
            m = by_id.get(mid)
            if not m:
                continue
            rel = [r for r in (m.get("related") or []) if r in by_id]
            line = _line(ctx, m) + f" — {snip.strip()[:160]}"
            if rel:
                line += " | related: " + ", ".join(f"{r} ({by_id[r].get('title', '')[:40]})" for r in rel[:3])
            items.append(line)
        items = items[a.offset:]
        sk = {"query": a.query, "engine": engine, "total": len(rows), "offset": a.offset, "results": []}
        taken, tr = fill_lines(items, room_for(ctx, sk, reserve=200))
        sk["results"] = taken
        return Result(sk, tr, ctx.with_args({"--offset": a.offset + len(taken)}) if tr else None)
    if sub == "brief":
        return brief(ctx)
    raise AKError(f"unknown memory subcommand {sub}", "", "agentkit memory list")


def brief(ctx: Ctx) -> Result:
    """Core (pinned) memories + titles index, ≤500 tokens total (envelope included)."""
    limit = min(ctx.budget, BRIEF_TOKENS) * 4
    mems = load_all(ctx)
    pinned = [m for m in mems if m.get("pinned")]
    others = [m for m in mems if not m.get("pinned")]
    data: dict = {"agents_md": None, "core": [], "index": [], "total": len(mems)}
    am = _agents_md(ctx)
    truncated = False
    nxt_parts = []
    reserve = 160
    if am:
        data["agents_md"] = {"path": am["path"], "lines": am["lines"], "head": ""}
    # 1) pinned bodies (clip each), 2) titles of the rest, 3) AGENTS.md head with leftovers
    for m in pinned:
        st = stale_anchors(ctx, m)
        entry = f"{m.get('title', '')}: " + " ".join(m["body"].split())[:220] + (" [maybe_stale]" if st else "")
        if len(dumps(data)) + len(dumps(entry)) + reserve > limit:
            truncated = True
            nxt_parts.append("core")
            break
        data["core"].append(entry)
    for m in others:
        entry = _line(ctx, m)
        if len(dumps(data)) + len(dumps(entry)) + reserve > limit:
            truncated = True
            nxt_parts.append("index")
            break
        data["index"].append(entry)
    if am:
        room = limit - len(dumps(data)) - reserve
        head = " ".join(am["text"].split())
        if room > 40:
            if len(dumps(head)) > room:
                cut = room
                while cut > 0 and len(dumps(head[:cut])) + 2 > room:
                    cut = int(cut * 0.9)
                data["agents_md"]["head"] = head[:cut] + "…"
                truncated = True
                nxt_parts.append("agents")
            else:
                data["agents_md"]["head"] = head
        else:
            truncated = True
            nxt_parts.append("agents")
    nxt = None
    if truncated:
        if "index" in nxt_parts or "core" in nxt_parts:
            nxt = ctx.next_cmd("memory", "list")
        else:
            nxt = ctx.next_cmd("read", am["path"]) if am else ctx.next_cmd("memory", "list")
    return Result(data, truncated, nxt, budget_chars=limit)
