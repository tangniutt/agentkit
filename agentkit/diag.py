# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""C-03 `logs` / `debug-bundle` and C-04 `feedback`.

feedback only DRAFTS an issue (title, body, prefilled URL). It never posts anything:
issues are public, so the agent must ask its human first.
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import time
import urllib.parse
from pathlib import Path

from . import __version__
from .config import repo_config_path
from .core import AKError, Ctx, Result, dumps, fill_lines, room_for
from .events import ERRORS, RUN_ID_RE, hint, read_events
from .redact import redact_text, register_secret

REPO_URL = "https://github.com/tangniutt/agentkit"
URL_MAX = 6000


def _check_id(rid: str) -> str:
    if not RUN_ID_RE.match(rid or ""):
        raise AKError(f"no events for '{str(rid)[:40]}'", "ids look like r1123045501ab12 (error.run) or t1a2b3c (task)",
                      "agentkit task status", code="AK-LOGS-UNKNOWN")
    return rid


def _events(ctx: Ctx, rid: str) -> list:
    evs = read_events(ctx.root, _check_id(rid))
    if not evs:
        raise AKError(f"no events for '{rid}'", "unknown id, or its logs were rotated/cleaned", "agentkit task status",
                      code="AK-LOGS-UNKNOWN")
    return evs


def _fmt(e: dict) -> str:
    t = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
    parts = [t, f"#{e.get('step')}" if e.get("step") is not None else "#-", e.get("component") or "-", e.get("event") or "-"]
    if e.get("duration") is not None:
        parts.append(f"{e['duration']}s")
    if e.get("error_code"):
        parts.append(e["error_code"])
    extra = {k: v for k, v in e.items() if k not in ("ts", "run_id", "step", "component", "event", "duration", "error_code")}
    if extra:
        parts.append(dumps(extra)[:200])
    return " ".join(str(p) for p in parts)


def _register_keys(ctx: Ctx):
    from .models import all_models

    for m in all_models(ctx.config).values():
        if m.key_env and os.environ.get(m.key_env):
            register_secret(os.environ[m.key_env])


def cmd_logs(ctx: Ctx, a) -> Result:
    _register_keys(ctx)
    evs = _events(ctx, a.id)
    total = len(evs)
    if a.errors:
        evs = [e for e in evs if e.get("error_code") or e.get("event") in ("error", "crash", "model_error", "interrupted")]
    if a.tail:
        evs = evs[-a.tail:]
    lines = [_fmt(e) for e in evs]
    data = {"id": a.id, "events": total, "shown": 0, "format": "time #step component event [duration] [code] {extra}", "lines": []}
    crash = ctx.root / ".agentkit" / "logs" / f"crash-{a.id}.log"
    if crash.exists():
        data["traceback"] = ctx.rel(crash)
    codes = sorted({e["error_code"] for e in evs if e.get("error_code")})
    if codes:
        data["hints"] = {c: hint(c) for c in codes[:4]}
    # newest lines matter most: fill from the end
    taken, trunc = fill_lines(list(reversed(lines)), room_for(ctx, data, reserve=240))
    data["lines"] = list(reversed(taken))
    data["shown"] = len(taken)
    nxt = None
    if trunc:
        nxt = ctx.with_args({"--tail": max(1, len(taken) // 2)}) if not a.tail else ctx.with_args({"--budget": ctx.budget * 2})
    return Result(data, trunc, nxt)


def _versions() -> dict:
    try:
        gv = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        gv = "unavailable"
    return {"agentkit": __version__, "python": sys.version.split()[0], "platform": platform.platform(), "git": gv}


_SECRETISH_KEY = re.compile(r"(?i)^\s*([\w.-]*(key|token|secret|password|passwd)[\w.-]*)\s*=\s*(.+)$")


def sanitized_config(ctx: Ctx) -> str:
    p = repo_config_path(ctx.root)
    if not p.exists():
        return "# no .agentkit/config.toml\n"
    out = []
    for line in p.read_text(errors="replace").splitlines():
        m = _SECRETISH_KEY.match(line)
        if m and m.group(1).lower() != "key_env":
            line = f"{m.group(1)} = \"[REDACTED]\""
        out.append(line)
    return redact_text("\n".join(out) + "\n")


def build_bundle(ctx: Ctx, rid: str, with_transcript: bool = False) -> tuple:
    _register_keys(ctx)
    evs = _events(ctx, rid)
    files: dict = {}
    files["events.jsonl"] = "\n".join(json.dumps(e, ensure_ascii=False) for e in evs) + "\n"
    files["config.toml"] = sanitized_config(ctx)
    logs = ctx.root / ".agentkit" / "logs"
    crash = logs / f"crash-{rid}.log"
    if crash.exists():
        files["crash.log"] = crash.read_text(errors="replace")
    if rid.startswith("t"):
        td = ctx.root / ".agentkit" / "tasks" / rid
        for name in ("task.json", "report.json", "worker.log") + (("transcript.jsonl",) if with_transcript else ()):
            p = td / name
            if p.exists():
                files[name] = p.read_text(errors="replace")[-400_000:]
    manifest = {"run_id": rid, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "versions": _versions(),
                "files": sorted(files), "redacted": True, "contains_code": with_transcript,
                "error_codes": sorted({e["error_code"] for e in evs if e.get("error_code")})}
    files["manifest.json"] = json.dumps(manifest, indent=1)
    out_dir = ctx.root / ".agentkit" / "bundles"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"debug-{rid}.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        for name, text in sorted(files.items()):
            data = redact_text(text).encode("utf-8")
            info = tarfile.TarInfo(f"agentkit-debug-{rid}/{name}")
            info.size = len(data)
            info.mtime = int(time.time())
            tf.addfile(info, io.BytesIO(data))
    return path, manifest


def cmd_debug_bundle(ctx: Ctx, a) -> Result:
    path, manifest = build_bundle(ctx, a.id, a.with_transcript)
    return Result({"bundle": ctx.rel(path), "bytes": path.stat().st_size, "files": manifest["files"],
                   "error_codes": manifest["error_codes"],
                   "note": "secrets redacted, config keeps only key_env names; review before sharing"
                           + ("" if a.with_transcript else "; worker transcript excluded (may contain code): --with-transcript")},
                  next=ctx.next_cmd("feedback", "--bug", a.id))


# ---------------------------------------------------------------- feedback (C-04)


def _issue_url(template: str, title: str, fields: dict) -> str:
    q = {"template": template, "title": title}
    q.update({k: v for k, v in fields.items() if v})
    url = f"{REPO_URL}/issues/new?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)
    while len(url) > URL_MAX:
        # shrink the longest field until the URL fits browsers' limits
        k = max((k for k in fields if fields[k]), key=lambda k: len(fields[k]))
        fields[k] = fields[k][: max(0, len(fields[k]) * 2 // 3)] + "…"
        q.update({k: fields[k]})
        url = f"{REPO_URL}/issues/new?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)
    return url


def cmd_feedback(ctx: Ctx, a) -> Result:
    _register_keys(ctx)
    v = _versions()
    env_line = f"agentkit {v['agentkit']}, python {v['python']}, {v['git']}, {v['platform']}"
    if a.bug:
        evs = _events(ctx, a.bug)
        errs = [e for e in evs if e.get("error_code")]
        last = errs[-1] if errs else evs[-1]
        code = last.get("error_code") or "unknown"
        what = str(last.get("what") or last.get("event") or "")[:100]
        tj = ctx.root / ".agentkit" / "tasks" / a.bug / "task.json"
        if a.bug.startswith("t") and tj.exists():
            t = json.loads(tj.read_text())
            if t.get("error_code"):
                code = t["error_code"]
                what = str(t.get("reason") or t.get("status") or "")[:100]
                last = {"component": t.get("component"), "step": t.get("last_step")}
        command = next((str(e.get("step")) for e in evs if e.get("component") == "cli" and e.get("step")), None) or \
            ("task" if a.bug.startswith("t") else "?")
        tail = "\n".join(_fmt(e) for e in evs[-12:])
        title = redact_text(f"[bug] {code}: {what or ERRORS.get(code, ('',))[0]} (agentkit {v['agentkit']})")[:120]
        fields = {
            "version": v["agentkit"], "command": command, "error_code": code, "run_id": a.bug,
            "repro": "1. <minimal steps; no private code or secrets>\n2. ",
            "expected": "<what should have happened>", "actual": redact_text(f"{code} at {last.get('component')} step {last.get('step')}: {what}"),
            "logs": redact_text(tail)[:2500], "environment": env_line,
        }
        template = "bug_report.yml"
    else:
        idea = redact_text(a.idea.strip())
        title = f"[idea] {idea[:90]}"
        fields = {"problem": "<what you were trying to do and why AgentKit got in the way>", "proposal": idea[:1500],
                  "alternatives": "", "version": v["agentkit"]}
        template = "feature_request.yml"
    body = "\n\n".join(f"### {k}\n{val}" for k, val in fields.items() if val) + \
        "\n\n- [x] Filed by an AI agent on behalf of a user (the user approved filing)"
    url = _issue_url(template, title, dict(fields))
    search = f"{REPO_URL}/issues?" + urllib.parse.urlencode({"q": f"is:issue {fields.get('error_code') or title[7:50]}"},
                                                            quote_via=urllib.parse.quote)
    data = {"posted": False, "title": title, "url": url, "search_first": search, "body": body,
            "rules": "search existing issues first; ask your human before filing (issues are public); "
                     "no secrets or private code; one issue per problem"}
    room = room_for(ctx, {k: v for k, v in data.items() if k != "body"}, reserve=200)
    trunc = False
    if len(dumps(body)) > room:
        cut = max(0, room - 20)
        while cut and len(dumps(body[:cut])) > room:
            cut = int(cut * 0.9)
        data["body"] = body[:cut] + "…"
        trunc = True
    return Result(data, trunc, ctx.with_args({"--budget": ctx.budget * 2}) if trunc else None)
