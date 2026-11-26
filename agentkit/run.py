# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
#
# _spawn/_kill_group adapt the process-group handling of mini-swe-agent
#   src/minisweagent/environments/local.py  (https://github.com/SWE-agent/mini-swe-agent)
#   Copyright (c) 2025 Kilian A. Lieret and Carlos E. Jimenez, MIT License (see NOTICE).
"""M-08 run: timeout, process-group kill, exit code + duration, stderr tail first, full log on disk."""

from __future__ import annotations

import os
import re
import shutil
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
_ACTIVE_JOBS: dict = {}


def _windows_job(proc: subprocess.Popen):
    import ctypes

    kernel = ctypes.windll.kernel32
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError("CreateJobObjectW failed")
    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]
    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_uint64), ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64), ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64), ("OtherTransferCount", ctypes.c_uint64)]
    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS)]
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel.CloseHandle(job)
        raise OSError("SetInformationJobObject failed")
    if not kernel.AssignProcessToJobObject(job, int(proc._handle)):
        kernel.CloseHandle(job)
        raise OSError("AssignProcessToJobObject failed")
    return job


def _close_windows_job(proc: subprocess.Popen):
    import ctypes

    job = _ACTIVE_JOBS.pop(proc, None)
    if job:
        ctypes.windll.kernel32.CloseHandle(job)


def kill_active():
    for proc in list(ACTIVE):
        try:
            _kill_group(proc)
        except Exception:
            pass


def _kill_group(proc: subprocess.Popen):
    if os.name != "posix":
        job = _ACTIVE_JOBS.get(proc)
        if job:
            import ctypes

            ctypes.windll.kernel32.TerminateJobObject(job, 1)
        else:
            proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
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
        windows_path = bool(re.search(r"[A-Za-z]:[\\/]", command))
        bash = shutil.which("bash") if os.name == "nt" and not windows_path else None
        if bash:
            proc = subprocess.Popen([bash, "-c", command], cwd=str(cwd), stdout=fo, stderr=fe,
                                    stdin=subprocess.DEVNULL, env=env)
        else:
            proc = subprocess.Popen(
                command, shell=True, cwd=str(cwd), stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
                start_new_session=os.name == "posix", env=env,
            )
        ACTIVE.add(proc)
        if os.name == "nt":
            try:
                _ACTIVE_JOBS[proc] = _windows_job(proc)
            except OSError:
                pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            proc.wait()
        else:
            # leader exited: reap any background children left in its group/job
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            else:
                _kill_group(proc)
        finally:
            ACTIVE.discard(proc)
            if os.name == "nt":
                _close_windows_job(proc)
    dur = time.monotonic() - t0
    if os.name == "nt" and bash:
        for p in (out_p, err_p):
            data = p.read_bytes()
            if b"\r\n" in data:
                p.write_bytes(data.replace(b"\r\n", b"\n"))
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


def fit_tails(err_p, out_p, room: int, head_first: bool = False) -> tuple[str, str, bool, bool, int, int]:
    """Split room between stderr (priority) and stdout. Sizes are measured as JSON-escaped chars.
    err_p / out_p: log paths or already-filtered text."""
    err_raw = err_p if isinstance(err_p, str) else err_p.read_bytes().decode("utf-8", "replace")
    out_raw = out_p if isinstance(out_p, str) else out_p.read_bytes().decode("utf-8", "replace")
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
        if head_first:  # C-08 filters whose most useful part is the start (diffs: file summary first)
            t = raw[:lo] if lo else ""
            while t and jlen(t) + 3 > r:
                t = t[: len(t) - len(t) // 8 - 1]
            return (t + "…") if t else "", True, total
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
    err_txt, out_txt, filt = filter_streams(a.command, r["stderr_log"], r["stdout_log"],
                                            enabled=compression_on(ctx.config, "run_filters") and not getattr(a, "raw", False))
    if filt:
        # C-08: command-aware filter; the raw output stays in the logs (agentkit logs ID --raw)
        data["filtered"] = filt
    room = room_for(ctx, data, reserve=len(ctx.next_cmd("read", f"{so}:1-999999")) + 60)
    e, o, e_cut, o_cut, e_total, o_total = fit_tails(err_txt, out_txt, room, head_first=bool(filt and filt.pop("_head", False)))
    data["stderr_tail"], data["stdout_tail"] = e, o
    if filt:
        e_total, o_total = _lines(r["stderr_log"]), _lines(r["stdout_log"])
    truncated = e_cut or o_cut
    nxt = None
    if e_cut:
        nxt = ctx.next_cmd("read", f"{se}:1-{e_total}")
    elif o_cut:
        nxt = ctx.next_cmd("read", f"{so}:1-{o_total}")
    return Result(data, truncated, nxt)


def _lines(p: Path) -> int:
    t = p.read_bytes().decode("utf-8", "replace")
    return t.count("\n") + (0 if t.endswith("\n") or not t else 1)


def compression_on(config: dict, part: str) -> bool:
    c = config.get("compress") or {}
    return bool(c.get("enabled", True)) and bool(c.get(part, True))


def filter_streams(command: str, err_p: Path, out_p: Path, enabled: bool = True):
    """C-08: apply the command-aware filter to stderr and stdout. Returns (err, out, info|None)."""
    err = err_p.read_bytes().decode("utf-8", "replace")
    out = out_p.read_bytes().decode("utf-8", "replace")
    if not enabled:
        return err, out, None
    from .compress import compress, select_filter

    f = select_filter(command, out + "\n" + err)
    e2, ei = compress(command, err, filt=f)
    o2, oi = compress(command, out, filt=f)
    if not (ei["applied"] or oi["applied"]):
        return err, out, None
    raw = ei["raw_lines"] + oi["raw_lines"]
    shown = (ei["lines"] if ei["applied"] else ei["raw_lines"]) + (oi["lines"] if oi["applied"] else oi["raw_lines"])
    run_id = err_p.name.replace(".stderr.log", "")
    info = {"filter": f["id"], "lines": f"{shown}/{raw}", "raw": f"agentkit logs {run_id} --raw"}
    if f.get("fit") == "head":
        info["_head"] = True
    return e2, o2, info


_ = tail
