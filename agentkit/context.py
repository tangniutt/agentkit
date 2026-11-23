# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""C-08 worker-transcript compression: session dedup + progressive aging.

Ideas (no code copied) from OmniRoute (MIT): engines/session-dedup (content-hash repeated
blocks -> short reference) and progressiveAging.ts (recent turns verbatim, older ones
summarised). Our rules:

- The stable prefix (messages[0] system prompt incl. memory brief, messages[1] task, and the
  tool schemas) is never rewritten, so provider prompt caches keep hitting.
- Only tool-result *contents* and old assistant tool-call *arguments* are rewritten; no message
  is removed or reordered, so every tool_call keeps its matching tool result.
- Aging happens in batches (when enough old text has piled up), not every round, so the
  rewritten region (and with it the cache break) moves rarely.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from .compress import best_why
from .core import dumps

DEDUP_TOOLS = {"read", "search", "ls", "glob", "repo_map", "run", "memory_search", "memory_show"}
AGED = "[aged]"
VOLATILE = ("duration_s", "logs", "filtered", "s")


def result_key(name: str, args: dict, env: dict) -> Optional[str]:
    """Content hash of a tool result, ignoring volatile fields (timings, log file names)."""
    if name not in DEDUP_TOOLS or not isinstance(env, dict):
        return None
    data = env.get("data")
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k not in VOLATILE}
    body = {"ok": env.get("ok"), "data": data, "error": (env.get("error") or {}).get("what") if env.get("error") else None,
            "truncated": env.get("truncated")}
    if name == "run":
        body["cmd"] = str(args.get("command", ""))
    return hashlib.sha256(dumps(body).encode()).hexdigest()[:24]


def same_as(turn: int, name: str, args: dict, env: dict) -> str:
    what = _call_label(name, args)
    return dumps({"ok": env.get("ok"), "data": {"same_as_turn": turn,
                                                "note": f"identical to the result of {what} at turn {turn} (unchanged since)"}})


def _call_label(name: str, args: dict) -> str:
    if name == "read":
        return f"read {args.get('spec') or args.get('path', '')}"
    if name == "run":
        return f"run `{str(args.get('command', ''))[:80]}`"
    if name == "search":
        return f"search {str(args.get('pattern', ''))[:60]!r}"
    return name


def summarize(name: str, args: dict, content: str) -> str:
    """One line standing in for an old tool result."""
    try:
        env = json.loads(content)
    except ValueError:
        return f"{AGED} {name}: {len(content)} chars; head: {content[:120]}"
    if not isinstance(env, dict):
        return f"{AGED} {name}: {len(content)} chars"
    d = env.get("data")
    if not env.get("ok"):
        err = env.get("error") or {}
        return f"{AGED} {_call_label(name, args)} -> error: {str(err.get('what', ''))[:140]}"
    if isinstance(d, dict) and "same_as_turn" in d:
        return f"{AGED} {_call_label(name, args)} -> same as turn {d['same_as_turn']}"
    if name == "read" and isinstance(d, dict):
        lines = d.get("lines") or []
        rng = f"{lines[0]}-{lines[1]}" if len(lines) == 2 else "?"
        return (f"{AGED} read {d.get('path', args.get('spec', ''))} lines {rng} of {d.get('total_lines', '?')} "
                f"(sha {d.get('sha', '?')}); read again if you need the text")
    if name == "run" and isinstance(d, dict):
        txt = (d.get("stderr_tail") or "") + "\n" + (d.get("stdout_tail") or "")
        return f"{AGED} run `{str(args.get('command', ''))[:80]}` -> exit {d.get('exit_code')}; {best_why(txt, 160)}"
    if name == "search" and isinstance(d, dict):
        files = []
        for m in d.get("matches") or d.get("results") or []:
            p = str(m).split(":", 1)[0] if not isinstance(m, dict) else m.get("path", "")
            if p and p not in files:
                files.append(p)
        return f"{AGED} search {str(args.get('pattern', ''))[:60]!r} -> {d.get('total', len(files))} matches in {', '.join(files[:6]) or '?'}"
    head = dumps(d)[:140]
    return f"{AGED} {_call_label(name, args)}: ok, {len(content)} chars; head: {head}"


class TranscriptCompressor:
    """Session dedup + progressive aging over a worker's message list (mutated in place)."""

    def __init__(self, cfg: Optional[dict] = None):
        cfg = cfg or {}
        on = bool(cfg.get("enabled", True))
        self.dedup_on = on and bool(cfg.get("dedup", True))
        self.aging_on = on and bool(cfg.get("aging", True))
        self.keep_turns = int(cfg.get("keep_turns", 2))
        self.min_chars = int(cfg.get("age_min_chars", 4000))
        self.small = int(cfg.get("age_small", 300))
        self.canon: dict = {}      # key -> (msg index, turn, full content)
        self.refs: list = []       # (msg index, key) dedup references
        self.calls: dict = {}      # tool_call_id -> (name, args)
        self.saved_chars = 0
        self.deduped = 0
        self.aged = 0

    # ------------------------------------------------ dedup
    def tool_result(self, messages: list, turn: int, call_id: str, name: str, args: dict, env: dict, content: str) -> str:
        """Content to append for this tool result (a reference when identical to an earlier verbatim one)."""
        self.calls[call_id] = (name, args)
        key = result_key(name, args, env) if self.dedup_on else None
        if key is None:
            return content
        hit = self.canon.get(key)
        if hit and len(content) > 200:
            ref = same_as(hit[1], name, args, env)
            if len(ref) < len(content):
                self.refs.append((len(messages), key))
                self.deduped += 1
                self.saved_chars += len(content) - len(ref)
                return ref
        self.canon[key] = (len(messages), turn, content)
        return content

    def note_call(self, call_id: str, name: str, args: dict):
        self.calls[call_id] = (name, args)

    # ------------------------------------------------ aging
    def _window_start(self, messages: list) -> int:
        asst = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
        if len(asst) <= self.keep_turns:
            return 2  # every turn is still inside the recent window: nothing is old yet (only the prefix precedes it)
        return asst[-self.keep_turns]

    def age(self, messages: list) -> int:
        """Summarise tool results (and big tool-call arguments) older than the last keep_turns turns.
        Runs only when at least age_min_chars of old text would be removed. Returns chars saved."""
        if not self.aging_on:
            return 0
        cut = self._window_start(messages)
        todo = []
        pending = 0
        for i in range(2, min(cut, len(messages))):  # 0 = system, 1 = task: the stable prefix
            m = messages[i]
            c = str(m.get("content") or "")
            if m.get("role") == "tool" and not c.startswith(AGED) and len(c) > self.small:
                todo.append(i)
                pending += len(c)
            elif m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    a = (tc.get("function") or {}).get("arguments") or ""
                    if len(a) > self.small and '"_aged"' not in a:
                        todo.append(i)
                        pending += len(a)
                        break
        if pending < self.min_chars:
            return 0
        before = sum(len(str(messages[i].get("content") or "")) + len(dumps(messages[i].get("tool_calls") or []))
                     for i in set(todo))
        refd = {}
        for ri, key in self.refs:
            refd.setdefault(self.canon.get(key, (None,))[0], []).append(ri)
        for i in sorted(set(todo)):
            m = messages[i]
            if m.get("role") == "tool":
                # a verbatim reference in the recent window points here: move the full text there first
                for ri in refd.get(i, []):
                    if ri >= cut:
                        key = next(k for r, k in self.refs if r == ri)
                        full = self.canon[key][2]
                        messages[ri]["content"] = full
                        self.canon[key] = (ri, self.canon[key][1], full)
                        self.refs = [(r, k) for r, k in self.refs if r != ri]
                        break
                name, args = self.calls.get(m.get("tool_call_id"), ("tool", {}))
                m["content"] = summarize(name, args, str(m.get("content") or ""))
                for k, v in list(self.canon.items()):
                    if v[0] == i:
                        del self.canon[k]
                self.aged += 1
            else:
                if m.get("content") and len(str(m["content"])) > self.small:
                    m["content"] = str(m["content"])[:self.small] + " …[aged]"
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    a = fn.get("arguments") or ""
                    if len(a) > self.small:
                        try:
                            obj = json.loads(a)
                        except ValueError:
                            obj = {}
                        keep = {k: v for k, v in (obj.items() if isinstance(obj, dict) else [])
                                if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 120}
                        keep["_aged"] = f"{len(a)} chars of arguments"
                        fn["arguments"] = json.dumps(keep)
        after = sum(len(str(messages[i].get("content") or "")) + len(dumps(messages[i].get("tool_calls") or []))
                    for i in set(todo))
        self.saved_chars += max(0, before - after)
        return max(0, before - after)

    def stats(self) -> dict:
        return {"deduped": self.deduped, "aged": self.aged, "saved_chars": self.saved_chars}
