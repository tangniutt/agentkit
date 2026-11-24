# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""C-03 `logs` / `debug-bundle`, C-04 `feedback` and C-12 the AI feedback loop.

feedback only DRAFTS an issue (title, body, prefilled URL). It never posts anything:
issues are public, so the agent must ask its human first.

C-12: `feedback --improve` drafts a design proposal with evidence collected from local
run / task events (codes, counts, rounds, tokens, flags; never code or secrets), every draft
carries dedupe keywords + a search URL, and `feedback_hint` gives task/verify results at most
one cheap `feedback` hint when a run ended with a signal worth reporting (none on clean runs).
docs/triage.md is how maintainers evaluate what comes in.
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
from collections import Counter
from pathlib import Path
from typing import Optional

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
    if getattr(a, "raw", False):
        return _raw_run(ctx, a.id)
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


def _raw_run(ctx: Ctx, rid: str) -> Result:
    """C-08: the unfiltered (redacted at write time) output of one `run` / check, by run log id."""
    from .run import fit_tails

    rid = rid if rid.startswith("run-") else "run-" + rid
    if not re.fullmatch(r"run-[\w-]{6,80}", rid):
        raise AKError(f"no events for '{rid[:40]}'", "run log ids look like run-20261123-024007-24337-7335",
                      "use the id from a run's filtered.raw or a check's log field", code="AK-LOGS-UNKNOWN")
    logs = ctx.root / ".agentkit" / "logs"
    out_p, err_p = logs / f"{rid}.stdout.log", logs / f"{rid}.stderr.log"
    if not (out_p.exists() or err_p.exists()):
        raise AKError(f"no events for '{rid[:40]}' (no raw run output)", "run logs are .agentkit/logs/run-*.stdout.log / .stderr.log, kept [logs] retain_days",
                      "use the id from a run's filtered.raw or a check's log field", code="AK-LOGS-UNKNOWN")
    for p in (out_p, err_p):
        if not p.exists():
            p.write_text("")
    data = {"id": rid, "stderr": "", "stdout": "", "logs": {"stderr": ctx.rel(err_p), "stdout": ctx.rel(out_p)}}
    e, o, e_cut, o_cut, e_total, o_total = fit_tails(err_p, out_p, room_for(ctx, data, reserve=160))
    data["stderr"], data["stdout"] = e, o
    nxt = None
    if e_cut:
        nxt = ctx.next_cmd("read", f"{ctx.rel(err_p)}:1-{e_total}")
    elif o_cut:
        nxt = ctx.next_cmd("read", f"{ctx.rel(out_p)}:1-{o_total}")
    return Result(data, bool(e_cut or o_cut), nxt)


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


AI_LABEL = "ai-feedback"
FILED_VIA = "agentkit feedback (AI agent)"


def _issue_url(template: str, title: str, fields: dict) -> str:
    # labels= only sticks for users with triage rights; the issue-triage workflow adds ai-feedback
    # from the prefilled `filed_via` field for everyone else (C-12)
    q = {"template": template, "title": title, "labels": AI_LABEL}
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
    if getattr(a, "improve", None) is not None:
        return _improve(ctx, a, v)
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
    fields["filed_via"] = FILED_VIA
    return _draft(ctx, template, title, fields, [fields.get("error_code") or "", title[7:]], by_code=template == "bug_report.yml")


def _draft(ctx: Ctx, template: str, title: str, fields: dict, kw_src: list, extra: Optional[dict] = None,
           by_code: bool = False) -> Result:
    body = "\n\n".join(f"### {k}\n{val}" for k, val in fields.items() if val and k != "filed_via") + \
        "\n\n- [x] Filed by an AI agent on behalf of a user (the user approved filing)"
    url = _issue_url(template, title, dict(fields))
    kws = keywords(*kw_src)
    data = {"posted": False, "title": title, "url": url, "search_first": search_url(kws, by_code), "keywords": kws}
    if extra:
        data.update(extra)
    data["body"] = body
    data["rules"] = ("open search_first: if an issue already covers it, comment/+1 there with your evidence instead of "
                     "a new issue; ask your human before filing (issues are public); no secrets or private code; "
                     "one problem or idea per issue")
    room = room_for(ctx, {k: v for k, v in data.items() if k != "body"}, reserve=200)
    trunc = False
    if len(dumps(body)) > room:
        cut = max(0, room - 20)
        while cut and len(dumps(body[:cut])) > room:
            cut = int(cut * 0.9)
        data["body"] = body[:cut] + "…"
        trunc = True
    return Result(data, trunc, ctx.with_args({"--budget": ctx.budget * 2}) if trunc else None)


# ---------------------------------------------------------------- C-12 dedupe keywords + search URL

_STOP = set("""about above after again against also because been before being below between both cannot could does doing
down during each from further have having here into itself just more most must need needs only other over same should
some such than that their them then there these they this those through under until very were what when where which
while with would your agentkit issue idea improve improvement proposal better make makes using used when tool tools
""".split())
_WORD = re.compile(r"AK-[A-Z0-9-]+|[A-Za-z][A-Za-z0-9_-]{3,}|[\u4e00-\u9fff]{2,8}")


def keywords(*texts: str, n: int = 5) -> list:
    """Short, deterministic keyword list for duplicate search: error codes first, then rare-ish words."""
    out: list = []
    for t in texts:
        for w in _WORD.findall(redact_text(t or "")):
            if w.startswith("AK-"):
                k = w
            else:
                k = w.lower().strip("-_")
                if k in _STOP or k.isdigit() or MASK_WORD in k:
                    continue
            if k and k not in out:
                out.append(k)
    codes = [k for k in out if k.startswith("AK-")]
    return (codes + [k for k in out if not k.startswith("AK-")])[:n]


MASK_WORD = "redacted"


def search_url(kws: list, by_code: bool = False) -> str:
    """bugs search by their error code; ideas / proposals by their first two words (GitHub ANDs terms)."""
    codes = [k for k in kws if k.startswith("AK-")]
    words = [k for k in kws if not k.startswith("AK-")]
    q = "is:issue " + " ".join(codes[:1] if by_code and codes else words[:2] or codes[:1])
    return f"{REPO_URL}/issues?" + urllib.parse.urlencode({"q": q.strip()}, quote_via=urllib.parse.quote)


# ---------------------------------------------------------------- C-12 evidence from local runs / tasks

EDIT_CODES = {"AK-TOOL-EDIT-NOMATCH", "AK-TOOL-EDIT-MULTI", "AK-TOOL-EDIT-SYNTAX", "AK-TOOL-EDIT-STALE", "AK-TOOL-EDIT-OP"}
REPEAT_MIN = 3  # same tool error this many times in one task = worth a look


def _task_json(root: Path, tid: str) -> dict:
    p = root / ".agentkit" / "tasks" / tid / "task.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _report_json(root: Path, tid: str) -> dict:
    p = root / ".agentkit" / "tasks" / tid / "report.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def signal(root: Path, rid: str, t: Optional[dict] = None, rep: Optional[dict] = None,
           evs: Optional[list] = None) -> Optional[tuple]:
    """(kind, reason) when a finished run is worth a feedback draft, else None. kind: bug | improve.

    Deterministic and cheap: reads the run's own event log and task/report JSON only."""
    root = Path(root)
    is_task = rid.startswith("t")
    t = t if t is not None else (_task_json(root, rid) if is_task else {})
    rep = rep if rep is not None else (_report_json(root, rid) if is_task else {})
    evs = evs if evs is not None else read_events(root, rid)
    codes = [e.get("error_code") for e in evs if e.get("error_code")]
    if t.get("error_code") in ("AK-INTERNAL", "AK-WORKER-CRASH") or "AK-INTERNAL" in codes or "AK-WORKER-CRASH" in codes:
        return "bug", t.get("error_code") if t.get("error_code") in ("AK-INTERNAL", "AK-WORKER-CRASH") else \
            ("AK-INTERNAL" if "AK-INTERNAL" in codes else "AK-WORKER-CRASH")
    if t.get("error_code") == "AK-WORKER-MALFORMED":
        return "improve", "AK-WORKER-MALFORMED: tool-call format failures"
    tool = Counter(e["error_code"] for e in evs if e.get("error_code") and str(e.get("component") or "").startswith("tool:"))
    if tool:
        code, n = tool.most_common(1)[0]
        if n >= REPEAT_MIN:
            return "improve", f"{code} x{n}"
        edits = sum(n for c, n in tool.items() if c in EDIT_CODES)
        if edits >= REPEAT_MIN:
            return "improve", f"edit failures x{edits}"
    if t.get("stopped") == "stuck" or t.get("error_code") == "AK-WORKER-STUCK":
        return "improve", "stuck"
    if t.get("status") in ("paused", "failed") and str(t.get("component") or "").startswith("tool:"):
        return "improve", f"{t.get('worker_status') or t.get('status')} at {t.get('component')}"
    flags = [f.get("flag") for f in (rep.get("suspicious") or []) if isinstance(f, dict)]
    if (rep.get("verdict") == "needs_review" or t.get("verdict") == "needs_review") and flags:
        return "improve", "needs_review " + sorted(set(flags))[0] + " (if a false alarm)"
    return None


def feedback_hint(root: Path, rid: str, **kw) -> Optional[str]:
    """At most one compact hint string, or None (clean runs cost zero extra tokens)."""
    try:
        s = signal(root, rid, **kw)
    except Exception:
        return None
    if not s:
        return None
    kind, why = s
    cmd = f"agentkit feedback --bug {rid}" if kind == "bug" else f"agentkit feedback --improve --from {rid}"
    return f"{cmd}  ({why[:80]}; only if AgentKit is at fault; ask your human before filing)"


def evidence(ctx: Ctx, ids: list) -> list:
    """One redacted line per run/task id: status, rounds, tokens, error codes with counts, flags. No code."""
    lines = []
    for rid in ids[:8]:
        evs = _events(ctx, rid)
        cnt = Counter(e["error_code"] for e in evs if e.get("error_code"))
        parts = [rid]
        if rid.startswith("t"):
            t, rep = _task_json(ctx.root, rid), _report_json(ctx.root, rid)
            parts.append(f"task {t.get('status', '?')}")
            for k in ("worker_status", "stopped", "verdict"):
                if t.get(k):
                    parts.append(f"{k}={t[k]}")
            parts.append(f"rounds={t.get('rounds', 0)} tokens={t.get('tokens', 0)}")
            if t.get("compactions"):
                parts.append(f"compactions={t['compactions']}")
            if t.get("repairs"):
                parts.append(f"repairs={t['repairs']}")
            flags = sorted({f.get("flag") for f in rep.get("suspicious") or [] if isinstance(f, dict)})
            if flags:
                parts.append("flags=" + ",".join(flags))
        else:
            cmd = next((str(e.get("step")) for e in evs if e.get("event") in ("command", "error", "crash") and e.get("step")), "?")
            parts.append(f"cmd={cmd}")
        if cnt:
            parts.append("codes " + ", ".join(f"{c}x{n}" for c, n in cnt.most_common(6)))
        s = signal(ctx.root, rid, evs=evs)
        if s:
            parts.append(f"signal: {s[1]}")
        lines.append(redact_text(" ".join(parts))[:300])
    return lines


# ---------------------------------------------------------------- C-12 improvement / design proposal

IMPROVE_KEYS = ("title", "observed", "proposal", "gain", "cost", "prior_art", "license", "verify", "from")
_PLACEHOLDER = {
    "observed": "<what you saw while really using AgentKit (commands, symptoms); evidence lines below>",
    "gain": "<expected gain: fewer host tokens / rounds / failures, by how much, for which tasks>",
    "cost": "<cost and risk: code size, memory, compatibility, new deps, behavior changes>",
    "verify": "<how to verify: an eval task or test that fails today and passes after>",
}


def _improve_input(a) -> dict:
    src: dict = {}
    if getattr(a, "input", None):
        try:
            raw = sys.stdin.read() if a.input == "-" else Path(a.input).read_text()
            src = json.loads(raw) if raw.strip() else {}
        except (OSError, ValueError) as e:
            raise AKError(f"usage: bad --input JSON: {str(e)[:80]}", "pass a JSON object file or - for stdin",
                          "agentkit manual feedback", code="AK-USAGE")
        if not isinstance(src, dict):
            raise AKError("usage: --input must be a JSON object", f"keys: {', '.join(IMPROVE_KEYS)}",
                          "agentkit manual feedback", code="AK-USAGE")
        if "cost_risk" in src and "cost" not in src:
            src["cost"] = src["cost_risk"]
    flags = {"observed": a.observed, "proposal": a.proposal or (a.improve or None), "gain": a.gain, "cost": a.cost,
             "prior_art": a.prior_art, "license": a.license, "verify": a.verify_by, "title": a.title}
    out = {k: str(src[k]) for k in IMPROVE_KEYS if k in src and k != "from" and src[k] not in (None, "")}
    out.update({k: str(v) for k, v in flags.items() if v not in (None, "")})
    ids = list(src.get("from") or []) if isinstance(src.get("from"), list) else ([src["from"]] if src.get("from") else [])
    ids += list(a.from_ or [])
    out["from"] = list(dict.fromkeys(str(i) for i in ids))
    return out


def _improve(ctx: Ctx, a, v: dict) -> Result:
    inp = _improve_input(a)
    if not inp.get("proposal"):
        raise AKError("usage: --improve needs a proposal", "pass --improve 'TEXT', --proposal or a JSON proposal key",
                      "agentkit feedback --improve 'one concrete change' --from TASK_ID", code="AK-USAGE")
    ev = evidence(ctx, inp["from"]) if inp["from"] else []
    proposal = redact_text(inp["proposal"])[:1500]
    title = redact_text(inp.get("title") or proposal.splitlines()[0])
    title = f"[improve] {title[:90]}"
    missing = [k for k in ("observed", "gain", "cost", "verify") if not inp.get(k)]
    if not ev:
        missing.append("from")
    fields = {
        "observed": redact_text(inp.get("observed") or _PLACEHOLDER["observed"])[:1200],
        "evidence": "\n".join(ev) or "<no run/task ids given: add --from ID from real use>",
        "proposal": proposal,
        "gain": redact_text(inp.get("gain") or _PLACEHOLDER["gain"])[:600],
        "cost_risk": redact_text(inp.get("cost") or _PLACEHOLDER["cost"])[:600],
        "prior_art": redact_text(inp.get("prior_art") or "")[:600],
        "license": redact_text(inp.get("license") or ("" if not inp.get("prior_art") else "<license of borrowed code, or 'ideas only'>"))[:120],
        "verify": redact_text(inp.get("verify") or _PLACEHOLDER["verify"])[:600],
        "version": v["agentkit"],
        "filed_via": FILED_VIA,
    }
    codes = [c for line in ev for c in re.findall(r"AK-[A-Z0-9-]+", line)]
    extra = {"missing": missing} if missing else None
    return _draft(ctx, "improvement.yml", title, fields, [" ".join(dict.fromkeys(codes)), title[10:], proposal], extra)
