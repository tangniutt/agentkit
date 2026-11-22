# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
#
# _spawn/_kill_group adapt the process-group handling of mini-swe-agent
#   src/minisweagent/environments/local.py  (https://github.com/SWE-agent/mini-swe-agent)
#   Copyright (c) 2025 Kilian A. Lieret and Carlos E. Jimenez, MIT License (see NOTICE).
"""M-08 run: timeout, process-group kill, exit code + duration, stderr tail first, full log on disk."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import permissions
from .core import AKError, Ctx, Result, dumps, room_for
from .redact import redact_text

DEFAULT_TIMEOUT = 120
ACTIVE: set = set()  # running Popen objects; the task watchdog kills their groups on budget/cancel


def kill_active():
    for proc in list(ACTIVE):
        try:
            _kill_group(proc)
        except Exception:
            pass


def _kill_group(proc: subprocess.Popen):
    if os.name != "posix":
        proc.kill()
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=2)
            # leader gone; still SIGKILL the group to catch stragglers
        except subprocess.TimeoutExpired:
            continue
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def execute(command: str, cwd: Path, timeout: float, log_dir: Path, env: Optional[dict] = None) -> dict:
    """Run a shell command; stdout/stderr go to files (no pipe deadlocks). Returns raw facts."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}-{int(time.time() * 1000) % 100000}"
    out_p = log_dir / f"run-{stamp}.stdout.log"
    err_p = log_dir / f"run-{stamp}.stderr.log"
    t0 = time.monotonic()
    timed_out = False
    with open(out_p, "wb") as fo, open(err_p, "wb") as fe:
        proc = subprocess.Popen(
            command, shell=True, cwd=str(cwd), stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
            start_new_session=os.name == "posix", env=env,
        )
        ACTIVE.add(proc)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            proc.wait()
        else:
            # leader exited: reap any background children left in its group
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        finally:
            ACTIVE.discard(proc)
    dur = time.monotonic() - t0
    for p in (out_p, err_p):
        _redact_file(p)
    return {"exit_code": proc.returncode, "timed_out": timed_out, "duration_s": round(dur, 2),
            "stdout_log": out_p, "stderr_log": err_p}


def _redact_file(p: Path):
    try:
        data = p.read_bytes()
    except OSError:
        return
    text = data.decode("utf-8", "replace")
    red = redact_text(text)
    if red != text:
        p.write_text(red, encoding="utf-8")


def tail(p: Path, max_chars: int) -> tuple[str, bool, int]:
    """Last max_chars of a log (aligned to a line). Returns (text, cut, total_lines)."""
    text = p.read_bytes().decode("utf-8", "replace")
    total = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    if len(text) <= max_chars:
        return text, False, total
    t = text[-max_chars:]
    nl = t.find("\n")
    if 0 <= nl < len(t) // 3:
        t = t[nl + 1:]
    return "…" + t, True, total


def fit_tails(err_p: Path, out_p: Path, room: int) -> tuple[str, str, bool, bool, int, int]:
    """Split room between stderr (priority) and stdout. Sizes are measured as JSON-escaped chars."""
    err_raw = err_p.read_bytes().decode("utf-8", "replace")
    out_raw = out_p.read_bytes().decode("utf-8", "replace")
    def jlen(s):
        return len(dumps(s))
    e_need, o_need = jlen(err_raw), jlen(out_raw)
    e_room = min(e_need, max(room // 2, room - o_need))
    o_room = max(0, room - min(e_need, e_room))
    def cut(raw, r, p):
        total = raw.count("\n") + (0 if raw.endswith("\n") or not raw else 1)
        if jlen(raw) <= r:
            return raw, False, total
        # shrink by escaped length
        lo = max(0, r - 10)
        t = raw[-lo:] if lo else ""
        while t and jlen(t) + 3 > r:
            t = t[len(t) // 8 + 1:]
        return ("…" + t) if t else "", True, total
    e, e_cut, e_total = cut(err_raw, e_room, err_p)
    o, o_cut, o_total = cut(out_raw, o_room, out_p)
    return e, o, e_cut, o_cut, e_total, o_total


def cmd_run(ctx: Ctx, a) -> Result:
    v = permissions.check(a.command, ctx.config)
    if not v.allowed:
        raise AKError(
            f"refused by permission rule '{v.rule}'",
            f"{v.why} (segment: {(v.segment or '')[:120]})",
            "ask the human to run it, or allow it in .agentkit/config.toml [run] allow=[...]",
            data={"rule": v.rule, "denied": True},
        )
    timeout = a.timeout or float(ctx.config.get("run", {}).get("timeout", DEFAULT_TIMEOUT))
    cwd = ctx.resolve(a.cwd) if a.cwd else ctx.cwd
    r = execute(a.command, cwd, timeout, ctx.state("logs"))
    so, se = ctx.rel(r["stdout_log"]), ctx.rel(r["stderr_log"])
    data = {
        "exit_code": r["exit_code"],
        "timed_out": r["timed_out"],
        "duration_s": r["duration_s"],
        "stderr_tail": "",
        "stdout_tail": "",
        "logs": {"stderr": se, "stdout": so},
    }
    if r["timed_out"]:
        data["note"] = f"killed process group after {timeout:g}s"
    room = room_for(ctx, data, reserve=len(ctx.next_cmd("read", f"{so}:1-999999")) + 60)
    e, o, e_cut, o_cut, e_total, o_total = fit_tails(r["stderr_log"], r["stdout_log"], room)
    data["stderr_tail"], data["stdout_tail"] = e, o
    truncated = e_cut or o_cut
    nxt = None
    if e_cut:
        nxt = ctx.next_cmd("read", f"{se}:1-{e_total}")
    elif o_cut:
        nxt = ctx.next_cmd("read", f"{so}:1-{o_total}")
    return Result(data, truncated, nxt)


_ = tail
