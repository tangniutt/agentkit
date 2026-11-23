# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-13 task system, M-14 worktree isolation, M-15 anti-cheat report, M-16 budgets.

Design (ideas from Grok Workhorse delegate/wait/result + recover-on-restart, worker-bridge
per-task worktree + independent verification, LooppooL "the verifier assumes the worker
lies"; all MIT, no code copied):

- No long-lived daemon. Each task runs in its own detached process (`agentkit _task-run ID`).
  All state is on disk under .agentkit/tasks/<id>/task.json. Any task command first
  *recovers* (a running task whose process is gone becomes `interrupted`) and then
  *schedules* (starts queued tasks up to the global concurrency limit). Finishing workers
  schedule too, so the queue drains without a daemon.
- Each task gets branch agentkit/<id> checked out at .agentkit/worktrees/<id> from HEAD.
- When the worker stops (submitted, failed, budget, ...) the verifier copies the worker's
  diff onto a fresh detached worktree at the base commit and re-runs the checks there.
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from . import verify as V
from .config import load_config, model_configured
from .core import AKError, Ctx, Result, dumps
from .events import EventLog
from .redact import redact, register_secret

VERDICT_CODES = {"fail": "AK-VERIFY-FAIL", "invalid": "AK-VERIFY-INVALID", "needs_review": "AK-VERIFY-SUSPICIOUS"}
TERMINAL = {"done", "failed", "stopped", "cancelled", "interrupted", "paused"}
RESUMABLE = {"paused", "interrupted", "stopped"}
ACTIVE = {"running", "verifying"}
STATUS_CHARS = 400  # < 100 tokens
REPORT_CHARS = 300 * 4
DEFAULT_CONCURRENCY = 2
_JUNK = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache)/|\.pyc$")
_SKIP_MARK = re.compile(
    r"pytest\.mark\.(skip|xfail)|@unittest\.skip|\.skipTest\(|pytest\.skip\(|\b(it|test|describe)\.skip\(|\bx(it|describe)\(|t\.Skip\(|#\[ignore\]|@Disabled|@Ignore"
)
_HARDCODE_IF = re.compile(r"^\s*if\s*\(?\s*[\w.\[\]'\"]+\s*={2,3}\s*(-?\d[\w.]*|'[^']*'|\"[^\"]*\")")
_RETURN_LIT = re.compile(r"\breturn\s+(-?\d[\w.]*|'[^']*'|\"[^\"]*\"|True|False|true|false)\s*;?\s*$")
# `if n == 0: return "0 B"` is a base case, not a test-input special case: one comparison against a boundary
# value whose returned literal is itself trivial or spelled out in the goal. Anything else stays suspect.
_BOUNDARY = {"0", "0.0", "1", "-1", "''", '""'}
_TRIVIAL_RET = _BOUNDARY | {"True", "False", "true", "false"}


def _hardcode_suspect(line: str, nxt: str, goal: str) -> bool:
    m = _HARDCODE_IF.search(line)
    if not m:
        return False
    r = _RETURN_LIT.search(line) or _RETURN_LIT.search(nxt or "")
    if not r:
        return False
    cond = line.split(":", 1)[0] if ":" in line else line
    single = not re.search(r"\b(and|or)\b|&&|\|\|", cond)
    if single and m.group(1) in _BOUNDARY:
        ret = r.group(1)
        lit = ret[1:-1] if ret[:1] in "'\"" else ret
        if ret in _TRIVIAL_RET or (lit and lit in (goal or "")):
            return False
    return True


# ---------------------------------------------------------------- store


def tasks_dir(root: Path) -> Path:
    d = root / ".agentkit" / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def locked(root: Path):
    p = tasks_dir(root) / ".lock"
    with open(p, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def tpath(root: Path, tid: str) -> Path:
    if not re.match(r"^t[0-9a-f]{6,12}$", tid or ""):
        raise AKError(f"invalid task id '{str(tid)[:40]}'", "ids look like t1a2b3c", "agentkit task status")
    return tasks_dir(root) / tid


def load(root: Path, tid: str) -> dict:
    p = tpath(root, tid) / "task.json"
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        raise AKError(f"unknown task '{tid}'", "no such task in this repo", "agentkit task status")


def save(root: Path, t: dict):
    t["updated"] = time.time()
    d = tpath(root, t["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"task.json.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(redact(t), ensure_ascii=False, indent=1))
    os.replace(tmp, d / "task.json")


def update(root: Path, tid: str, **fields) -> dict:
    with locked(root):
        t = load(root, tid)
        t.update(fields)
        save(root, t)
        return t


def all_tasks(root: Path) -> list:
    out = []
    for d in sorted(tasks_dir(root).iterdir()):
        p = d / "task.json"
        if d.is_dir() and p.exists():
            try:
                out.append(json.loads(p.read_text()))
            except ValueError:
                pass
    return sorted(out, key=lambda t: t.get("created", 0))


# ---------------------------------------------------------------- process liveness


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return False
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        return b"_task-run" in cmd  # guards against PID reuse
    except (OSError, IndexError):
        return True


def recover(root: Path) -> list:
    """Running tasks whose process is gone -> interrupted (state survives crashes/restarts)."""
    changed = []
    for t in all_tasks(root):
        if t.get("status") in ACTIVE and not pid_alive(t.get("pid")):
            t["status"] = "interrupted"
            t["reason"] = "worker process ended unexpectedly (crash / kill / reboot); worktree kept; continue with agentkit task resume ID"
            t["ended"] = time.time()
            t["error_code"] = "AK-TASK-INTERRUPTED"
            t.setdefault("component", "worker")
            EventLog(root, t["id"], "task").emit("interrupted", step=t.get("rounds"), error_code="AK-TASK-INTERRUPTED")
            save(root, t)
            changed.append(t["id"])
        elif t.get("status") == "starting" and time.time() - t.get("updated", 0) > 30 and not pid_alive(t.get("pid")):
            t["status"] = "queued"
            save(root, t)
    return changed


def concurrency(cfg: dict) -> int:
    try:
        return max(1, int((cfg.get("tasks") or {}).get("concurrency", DEFAULT_CONCURRENCY)))
    except (TypeError, ValueError):
        return DEFAULT_CONCURRENCY


def schedule(root: Path) -> None:
    cfg = load_config(root)
    with locked(root):
        recover(root)
        ts = all_tasks(root)
        running = sum(1 for t in ts if t.get("status") in ACTIVE | {"starting"})
        for t in ts:
            if running >= concurrency(cfg):
                break
            if t.get("status") != "queued":
                continue
            d = tpath(root, t["id"])
            logf = open(d / "worker.log", "ab")
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.Popen(
                [sys.executable, "-m", "agentkit", "--root", str(root), "_task-run", t["id"]],
                cwd=str(root), stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                start_new_session=True, env=env, close_fds=True,
            )
            logf.close()
            t["status"] = "starting"
            t["pid"] = proc.pid
            save(root, t)
            running += 1


def queue_pos(root: Path, tid: str) -> Optional[int]:
    q = [t["id"] for t in all_tasks(root) if t.get("status") == "queued"]
    return q.index(tid) + 1 if tid in q else None


# ---------------------------------------------------------------- git helpers


def _git(root: Path, *args, check=True, env=None) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120, env=env)
    if check and r.returncode != 0:
        raise AKError(f"git {' '.join(args[:2])} failed", r.stderr.strip()[-300:], "agentkit doctor")
    return r.stdout


def _ensure_state_ignored(root: Path):
    gi = root / ".agentkit" / ".gitignore"
    have = gi.read_text() if gi.exists() else ""
    add = [x for x in ("tasks/", "worktrees/") if x not in have.split()]
    if not gi.exists():
        from .core import _AGENTKIT_GITIGNORE

        gi.parent.mkdir(parents=True, exist_ok=True)
        gi.write_text(_AGENTKIT_GITIGNORE)
    elif add:
        gi.write_text(have.rstrip("\n") + "\n" + "\n".join(add) + "\n")


def task_changes(wt: Path, base: str) -> dict:
    ch = V._git_changes(wt, base)
    return {p: s for p, s in ch.items() if not _JUNK.search(p)}


GUARD_HOOK = "#!/bin/sh\necho 'agentkit: workers may not commit or push (M-14)' >&2\nexit 1\n"


def guard_env(root: Path) -> dict:
    """Environment for worker tool processes: a hooks dir whose pre-commit/pre-push always refuse."""
    hooks = root / ".agentkit" / "state" / "guard-hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for h in ("pre-commit", "pre-push", "commit-msg", "pre-merge-commit"):
        p = hooks / h
        if not p.exists():
            p.write_text(GUARD_HOOK)
            p.chmod(0o755)
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(hooks),
            "GIT_TERMINAL_PROMPT": "0"}


def scrubbed_env(cfg: dict) -> dict:
    """os.environ minus every configured model key variable (M-17)."""
    from .models import all_models

    env = dict(os.environ)
    for m in all_models(cfg).values():
        if m.key_env:
            if env.get(m.key_env):
                register_secret(env[m.key_env])
            env.pop(m.key_env, None)
    return env


# ---------------------------------------------------------------- create


def content_hash(goal, checks, allow, model, tier, budget, base) -> str:
    blob = json.dumps([goal.strip(), list(checks), sorted(allow), model, tier, budget, base], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def task_type(goal: str) -> str:
    g = goal.lower()
    for typ, words in (("bugfix", ("fix", "bug", "broken", "fail", "error", "修")), ("test", ("test", "测试")),
                       ("refactor", ("refactor", "rename", "clean", "重构")),
                       ("feature", ("add", "implement", "support", "create", "new", "实现", "增加"))):
        if any(w in g for w in words):
            return typ
    return "other"


def create(ctx: Ctx, a) -> dict:
    from . import models as M

    cfg = ctx.config
    if not model_configured(cfg) or not M.all_models(cfg):
        raise AKError(
            "direct mode: no worker model configured",
            "task dispatch needs a worker model; every tool works without one",
            "do it yourself: agentkit repo_map -> search/read -> edit -> run -> verify --check '<test cmd>'"
            " (or: agentkit model add NAME --base-url URL --key-env VAR)",
            data={"mode": "direct", "task_created": False},
        )
    if not a.goal or not a.goal.strip():
        raise AKError("task needs a goal", "", 'agentkit task create "fix the failing test in calc.py" --check "pytest -q"')
    root = ctx.root
    if not ctx.is_git or not shutil.which("git"):
        raise AKError("not a git repository", "dispatch mode isolates each worker in a git worktree (M-14)",
                      "git init && git add -A && git commit -m init   (direct-mode tools work without git)")
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True)
    if head.returncode != 0:
        raise AKError("repository has no commits", "the worker's worktree starts from HEAD", "git add -A && git commit -m init")
    base = head.stdout.strip()
    spec = M.pick(cfg, a.model, a.tier)
    if spec.key_env and not os.environ.get(spec.key_env):
        raise AKError(f"environment variable {spec.key_env} is not set", f"model '{spec.name}' reads its key from it",
                      f"export {spec.key_env}=...  then  agentkit model test {spec.name}")
    for c in a.check or []:
        v = V.permissions.check(c, cfg)
        if not v.allowed:
            raise AKError(f"check refused by permission rule '{v.rule}'", v.why or "", "use a check command that is allowed")
    budget = {"tokens": a.budget_tokens or (cfg.get("worker") or {}).get("budget_tokens") or 0,
              "usd": a.budget_usd or (cfg.get("worker") or {}).get("budget_usd") or 0,
              "minutes": a.budget_minutes or (cfg.get("worker") or {}).get("budget_minutes") or 30}
    h = content_hash(a.goal, a.check or [], a.allow or [], spec.name, a.tier, budget, base)
    warnings = []
    dirty = [ln for ln in _git(root, "status", "--porcelain").splitlines() if ln[3:].split("/")[0] != ".agentkit"]
    if dirty:
        warnings.append(f"{len(dirty)} uncommitted change(s) are NOT visible to the worker (worktree starts at HEAD)")
    _ensure_state_ignored(root)
    with locked(root):
        recover(root)
        for t in all_tasks(root):
            if t.get("hash") == h and (t.get("status") in ACTIVE | {"queued", "starting"}
                                       or (t.get("status") == "done" and t.get("verdict") == "pass")):
                d = {"id": t["id"], "status": t["status"], "deduped": True}
                if queue_pos(root, t["id"]):
                    d["queue_pos"] = queue_pos(root, t["id"])
                return d
        free = shutil.disk_usage(str(root)).free // (1024 * 1024)
        min_free = int((cfg.get("tasks") or {}).get("min_free_mb", 200))
        if free < min_free:
            raise AKError(f"disk almost full ({free} MB free)", "refusing to create a new worktree",
                          "agentkit task clean --all   (removes worktrees of finished tasks)")
        tid = "t" + secrets.token_hex(3)
        while (tasks_dir(root) / tid).exists():
            tid = "t" + secrets.token_hex(3)
        wt = root / ".agentkit" / "worktrees" / tid
        branch = f"agentkit/{tid}"
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(root, "worktree", "add", "-q", "-b", branch, str(wt), base)
        # tool config for the worker: [run]/[output] only, never models
        sub = {k: cfg[k] for k in ("run", "output") if k in cfg}
        if sub:
            from .config import set_table

            for k, v in sub.items():
                set_table(wt, k, v)
        t = {
            "id": tid, "goal": a.goal, "checks": a.check or [], "allow": a.allow or [], "model": spec.name,
            "tier": spec.tier, "budget": budget, "hash": h, "base": base, "branch": branch,
            "worktree": str(wt.relative_to(root)), "status": "queued", "created": time.time(),
            "type": task_type(a.goal), "rounds": 0, "tokens": 0, "usd": 0.0,
        }
        save(root, t)
        EventLog(root, tid, "task").emit("created", step=0, model=spec.name, cli_run=ctx.run_id or None)
    schedule(root)
    t = load(root, tid)
    # C-06: lean output. model / branch are known to the host (branch = agentkit/<id>); next goes in the envelope
    out = {"id": tid, "status": t["status"]}
    qp = queue_pos(root, tid)
    if qp:
        out["queue_pos"] = qp
    if a.model is None and len(M.all_models(cfg)) > 1:
        out["model"] = spec.name
    if warnings:
        out["warnings"] = warnings
    if not a.check:
        out.setdefault("warnings", []).append("no --check given: the verdict can only be 'invalid'; pass --check '<test cmd>'")
    return out


# ---------------------------------------------------------------- runner (detached process)


def run_task(root: Path, tid: str) -> None:
    """Entry point of the detached worker process."""
    from . import memory
    from .models import pick
    from .run import kill_active
    from .worker import StopWorker, Worker

    cfg = load_config(root)
    wcfg = dict(cfg.get("worker") or {})
    wcfg["compress"] = dict(cfg.get("compress") or {})  # C-08
    t = update(root, tid, status="running", pid=os.getpid(), started=time.time())
    events = EventLog(root, tid, "task")
    events.emit("started", step=0, pid=os.getpid())
    spec = pick(cfg, t["model"])
    spec.key()  # read + register the key value for redaction while it is still in the env
    keyval = os.environ.get(spec.key_env) if spec.key_env else None
    # M-17: no tool process may see any configured key; the client keeps its own copy
    clean = scrubbed_env(cfg)
    for k in list(os.environ):
        if k not in clean:
            os.environ.pop(k, None)
    if spec.key_env and keyval:
        spec_key_env = spec.key_env
        spec.key = lambda: keyval  # type: ignore[method-assign]
        _ = spec_key_env
    os.environ.update(guard_env(root))
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"  # edit+rerun within 1 s must not hit a stale .pyc
    wt = root / t["worktree"]
    os.chdir(wt)
    d = tpath(root, tid)
    budget = t.get("budget") or {}
    t0 = time.monotonic()
    state = {"stop": None, "stop_at": None, "finalizing": False}
    main_tid = threading.get_ident()
    lock = threading.Lock()

    def request_stop(reason: str):
        with lock:
            if state["stop"] or state["finalizing"]:
                return
            state["stop"], state["stop_at"] = reason, time.time()
        kill_active()
        try:
            signal.pthread_kill(main_tid, signal.SIGUSR1)
        except (ProcessLookupError, OSError):
            pass

    def on_signal(signum, frame):
        if state["finalizing"]:
            return
        if signum == signal.SIGTERM and not state["stop"]:
            state["stop"], state["stop_at"] = "cancelled", time.time()
            kill_active()
        raise StopWorker(state["stop"] or "cancelled")

    signal.signal(signal.SIGUSR1, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    minutes = float(budget.get("minutes") or 0)

    def watchdog():
        while not state["finalizing"]:
            if minutes and time.monotonic() - t0 > minutes * 60:
                request_stop("budget")
            if (d / "cancel").exists():
                request_stop("cancelled")
            if state["stop"] and state["stop_at"] and time.time() - state["stop_at"] > 4 and not state["finalizing"]:
                # main thread did not react: record the stop ourselves, report is built lazily
                try:
                    code = "AK-TASK-CANCELLED" if state["stop"] == "cancelled" else "AK-BUDGET"
                    events.emit("forced_stop", component="task", error_code=code)
                    update(root, tid, status="cancelled" if state["stop"] == "cancelled" else "stopped",
                           stopped=state["stop"], reason=f"stopped: {state['stop']}", stopped_at=time.time(), ended=time.time(),
                           error_code=code, component="task")
                finally:
                    os._exit(3)
            time.sleep(0.2)

    threading.Thread(target=watchdog, daemon=True).start()

    def progress(stats):
        try:
            update(root, tid, rounds=stats["rounds"], tokens=stats["tokens_in"] + stats["tokens_out"], usd=stats["usd"])
        except Exception:
            pass

    worker = Worker(spec, wt, root, t, on_progress=progress, transcript=d / "transcript.jsonl", cfg=wcfg,
                    check_stop=lambda: state["stop"], events=events)
    brief_txt = ""
    try:
        b = memory.brief(Ctx(root=root, cwd=root, budget=500))
        brief_txt = dumps(b.data)
    except Exception:
        pass
    resume_txt = None
    if t.get("resumes"):
        from .worker import resume_context

        resume_txt = resume_context(d / "transcript.jsonl", wt, t["base"], {**t, "reason": t.get("resumed_from")})
        events.emit("resumed", step=worker.rounds, resume=t["resumes"])
    code = None
    try:
        outcome = worker.run(brief_txt, resume=resume_txt)
        status, reason, stopped, claim, code = outcome.status, outcome.reason, outcome.stopped, outcome.claim, outcome.code
    except StopWorker as e:
        status, reason, stopped, claim = "stopped", f"stopped: {e.reason}", e.reason, ""
        code = "AK-TASK-CANCELLED" if e.reason == "cancelled" else "AK-BUDGET"
    except Exception as e:  # never die silently; traceback to a log file, not to the AI
        import traceback

        from .redact import redact_text

        (root / ".agentkit" / "logs").mkdir(parents=True, exist_ok=True)
        (root / ".agentkit" / "logs" / f"crash-{tid}.log").write_text(redact_text(traceback.format_exc()))
        status, reason, stopped, claim = "failed", f"worker crashed: {type(e).__name__}: {str(e)[:160]}", None, ""
        code = "AK-WORKER-CRASH"
        events.emit("crash", step=worker.rounds, component=worker.last_component, error_code=code, log=f"crash-{tid}.log")
    with lock:
        state["finalizing"] = True
    kill_active()
    if stopped == "cancelled":
        status = "cancelled"
        code = "AK-TASK-CANCELLED"
    elif status == "stopped" and stopped:
        reason = f"stopped: {stopped}" + (f" ({reason})" if reason and not reason.startswith("stopped") else "")
    st = worker.stats()
    fields = dict(status="verifying", worker_status=status, reason=reason, stopped=stopped, claim=claim,
                  rounds=st["rounds"], tokens=st["tokens_in"] + st["tokens_out"], tokens_in=st["tokens_in"],
                  tokens_out=st["tokens_out"], usd=st["usd"], compactions=st["compactions"], repairs=st["repairs"],
                  compression=st.get("compression"),
                  denied=sorted(set(worker.denied) | set(t.get("denied") or [])),
                  worker_s=round(time.monotonic() - t0 + float((t.get("prior") or {}).get("worker_s") or 0), 1),
                  error_code=code, component=worker.last_component if code else None, last_step=worker.last_step)
    events.emit("worker_end", step=worker.rounds, component=worker.last_component, error_code=code, status=status,
                duration=time.monotonic() - t0)
    if state["stop_at"]:
        fields["stopped_at"] = time.time()
        fields["stop_requested_at"] = state["stop_at"]
    update(root, tid, **fields)
    final = "done" if status == "submitted" else status
    try:
        if final != "cancelled":
            tv = time.monotonic()
            rep = verify_task(root, tid)
            final_fields = dict(status=final, verdict=rep["verdict"], ended=time.time())
            vcode = VERDICT_CODES.get(rep["verdict"])
            events.emit("verified", component="verifier", duration=time.monotonic() - tv, error_code=vcode, verdict=rep["verdict"])
            if code is None and vcode:
                final_fields.update(error_code=vcode, component="verifier")
        else:
            final_fields = dict(status=final, ended=time.time())
    except Exception as e:
        final_fields = dict(status=final, verdict="invalid", ended=time.time(), reason=(reason + f"; verify error: {e}")[:300])
    update(root, tid, **final_fields)
    try:
        learn(root, load(root, tid))
    except Exception:
        pass
    try:
        schedule(root)
    except Exception:
        pass


# ---------------------------------------------------------------- verification (M-15)


def _added_lines(old: str, new: str) -> list:
    import difflib

    return [l[1:] for l in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
            if l.startswith("+") and not l.startswith("+++")]


def _base_text(root: Path, base: str, rel: str) -> str:
    r = subprocess.run(["git", "-C", str(root), "show", f"{base}:{rel}"], capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def _allowed(path: str, allow: list) -> bool:
    for pat in allow:
        p = pat.rstrip("/")
        if fnmatch.fnmatch(path, pat) or path == p or path.startswith(p + "/") or fnmatch.fnmatch(path, p + "/*"):
            return True
    return False


def task_flags(root: Path, t: dict, wt: Path, changes: dict, checks: list, verdict: str, clean_base: Path) -> list:
    flags = V.base_flags(changes, t.get("checks") or [], checks, verdict, [clean_base, wt])
    base = t["base"]
    skipped, hard = [], []
    for p, s in changes.items():
        if s == "D" or not (wt / p).is_file():
            continue
        try:
            new = (wt / p).read_text(errors="replace")
        except OSError:
            continue
        added = _added_lines(_base_text(root, base, p) if s == "M" else "", new)
        is_test = bool(V._TEST_PATH.search(p))
        if is_test and any(_SKIP_MARK.search(l) for l in added):
            skipped.append(p)
        if not is_test:
            for i, l in enumerate(added):
                if _hardcode_suspect(l, added[i + 1] if i + 1 < len(added) else "", t.get("goal") or ""):
                    hard.append(p)
                    break
    if skipped:
        flags.append({"flag": "tests_skipped", "paths": skipped[:3]})
    allow = t.get("allow") or []
    if allow:
        outside = sorted(p for p in changes if not _allowed(p, allow))
        if outside:
            flags.append({"flag": "outside_allow", "paths": outside[:3]})
    head = _git(wt, "rev-parse", "HEAD", check=False).strip()
    if head and head != base:
        flags.append({"flag": "worker_committed", "paths": [_git(wt, "log", "--oneline", "-1", check=False).strip()[:60]]})
    escapes = []
    rw = wt.resolve()
    for p in changes:
        q = wt / p
        if q.is_symlink():
            real = Path(os.path.realpath(q))
            if real != rw and rw not in real.parents:
                escapes.append(p)
    if escapes:
        flags.append({"flag": "path_escape", "paths": escapes[:3]})
    if t.get("denied"):
        flags.append({"flag": "denied_commands", "paths": [c[:60] for c in t["denied"][:3]]})
    if hard:
        flags.append({"flag": "hardcoded_return_suspect", "paths": hard[:3]})
    return flags


def verify_task(root: Path, tid: str) -> dict:
    """Independent verification: copy the worker's diff onto a fresh worktree at the base commit
    and re-run the checks there. The worker's own claim is never consulted."""
    t = load(root, tid)
    cfg = load_config(root)
    wt = root / t["worktree"]
    if not wt.exists():
        raise AKError("task worktree is gone", "it was cleaned", "agentkit task create ... to run again")
    t0 = time.monotonic()
    base = t["base"]
    changes = task_changes(wt, base)
    tmp = Path(tempfile.mkdtemp(prefix="agentkit-taskverify-"))
    clean = tmp / "wt"
    timeout = float((cfg.get("run") or {}).get("timeout", 120))
    try:
        _git(root, "worktree", "add", "-q", "--detach", "--force", str(clean), base)
        # dependency detection on the pristine tree (before the worker's changes are applied)
        deps_snapshot = {c: V._check_paths(c, clean) for c in t.get("checks") or []}
        for rel, st in changes.items():
            src, dst = wt / rel, clean / rel
            if st == "D":
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                shutil.copy2(src, dst)
        log_dir = root / ".agentkit" / "logs"
        env = scrubbed_env(cfg)
        checks = V.run_checks(t.get("checks") or [], clean, timeout, log_dir, env=env) if t.get("checks") else []
    finally:
        subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", str(clean)], capture_output=True)
        subprocess.run(["git", "-C", str(root), "worktree", "prune"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    if not checks or any(c["result"] == "invalid" for c in checks):
        verdict = "invalid"
    elif all(c["result"] == "pass" for c in checks):
        verdict = "pass"
    else:
        verdict = "fail"
    # flags: use the pristine dependency set too (a deleted check script must still count)
    flags = task_flags(root, t, wt, changes, checks, verdict, wt)
    extra = sorted(p for p in changes if any(p == d or (d.endswith("/") and p.startswith(d)) for ds in deps_snapshot.values() for d in ds))
    if extra and not any(f["flag"] == "check_script_modified" for f in flags):
        flags.append({"flag": "check_script_modified", "paths": extra[:3]})
    if verdict == "pass" and flags:
        verdict = "needs_review"
    t = load(root, tid)
    report = {
        "task": tid,
        "verdict": verdict,
        "status": t.get("worker_status") if t.get("status") == "verifying" else t.get("status"),
        "checks": checks,
        "changed": [f"{s} {p}" for p, s in sorted(changes.items())],
        "suspicious": flags,
        "cost": {"s": round((t.get("worker_s") or 0) + time.monotonic() - t0, 1), "tokens": t.get("tokens", 0),
                 "usd": round(t.get("usd", 0.0), 4)},
        "env": "git-worktree",
        "branch": t.get("branch"),
    }
    if t.get("stopped"):
        report["stopped"] = t["stopped"]
    if t.get("reason") and t.get("worker_status") != "submitted":
        report["why"] = t["reason"][:160]
    if t.get("claim"):
        report["claim_untrusted"] = t["claim"][:120]
    code = t.get("error_code") or VERDICT_CODES.get(verdict)
    if code:
        report["error"] = {"code": code, "component": t.get("component") or "verifier",
                           "step": t.get("last_step", t.get("rounds", 0)), "log": f"agentkit logs {tid} --errors"}
    (tpath(root, tid) / "report.json").write_text(json.dumps(redact(report), ensure_ascii=False, indent=1))
    return report


# ---------------------------------------------------------------- auto memory (M-21)


def learn(root: Path, t: dict) -> None:
    from . import memory

    ctx = Ctx(root=root, cwd=root)
    rep_p = tpath(root, t["id"]) / "report.json"
    rep = json.loads(rep_p.read_text()) if rep_p.exists() else {}
    if rep.get("suspicious"):
        return  # never learn from a run that looks like cheating
    stats_p = ctx.state("state") / "model-stats.json"
    try:
        stats = json.loads(stats_p.read_text())
    except (FileNotFoundError, ValueError):
        stats = {}
    passed = t.get("status") == "done" and t.get("verdict") == "pass"
    s = stats.setdefault(t["model"], {}).setdefault(t.get("type", "other"), {"pass": 0, "fail": 0})
    s["pass" if passed else "fail"] += 1
    stats_p.write_text(json.dumps(stats, indent=1))
    lines = [f"- {m} | {typ}: {c['pass']} pass / {c['fail']} fail" for m, by in sorted(stats.items()) for typ, c in sorted(by.items())]
    memory.add(ctx, "Worker model results by task type", "\n".join(lines[-20:]), "tools-models", pinned=False,
               mid="model-stats", source="auto")
    if not passed:
        why = t.get("reason") or ""
        if rep.get("verdict") == "fail":
            bad = [c for c in rep.get("checks", []) if c.get("result") != "pass"]
            why = f"check failed: {bad[0]['cmd'][:60]} -> {str(bad[0].get('why', ''))[:80]}" if bad else why
        elif t.get("status") == "done":
            why = f"verdict {t.get('verdict')}"
        known = []
        try:
            known = [l for l in memory.get(ctx, "task-failures")["body"].splitlines() if l.startswith("- ")]
        except AKError:
            pass
        known.append(f"- {time.strftime('%Y-%m-%d')} {t.get('type')} via {t['model']}: {why[:140]}")
        memory.add(ctx, "Recent task failure reasons", "\n".join(known[-8:]), "lessons", pinned=False,
                   mid="task-failures", source="auto")


# ---------------------------------------------------------------- CLI views


def compact_status(root: Path, t: dict) -> dict:
    s = {"id": t["id"], "status": t["status"]}
    if t["status"] == "queued":
        s["pos"] = queue_pos(root, t["id"])
    if t.get("verdict"):
        s["verdict"] = t["verdict"]
    if t.get("stopped"):
        s["stopped"] = t["stopped"]
    s["rounds"] = t.get("rounds", 0)
    s["tok"] = t.get("tokens", 0)
    if t.get("usd"):
        s["usd"] = round(t["usd"], 4)
    start = t.get("started") or t.get("created")
    end = t.get("ended") or time.time()
    if start:
        s["s"] = int(end - start)
    if t.get("error_code") and t["status"] in TERMINAL:
        s["code"] = t["error_code"]
        s["at"] = f"{t.get('component') or 'task'}#{t.get('last_step', t.get('rounds', 0))}"
        s["log"] = f"agentkit logs {t['id']} --errors"
    if t["status"] in TERMINAL and t.get("reason") and t.get("worker_status") != "submitted":
        s["why"] = str(t["reason"])[:70]
    while len(dumps(s)) > STATUS_CHARS - 80 and "why" in s and len(s["why"]) > 10:
        s["why"] = s["why"][: len(s["why"]) // 2]
    return s


def compact_report(rep: dict) -> tuple:
    """≤300 tokens. C-06 trims what the host already knows: the check command and log id of a
    passing check (its index is its identity), the constant env, the task branch (always
    agentkit/<id>) and the worker's claim when the verifier confirmed a pass."""
    checks = []
    for c in rep.get("checks") or []:
        c = dict(c)
        if c.get("result") == "pass":
            c.pop("cmd", None)
            c.pop("log", None)
        checks.append(c)
    base = {k: rep[k] for k in ("verdict", "changed", "suspicious", "cost") if k in rep}
    base["checks"] = checks
    base = {k: base[k] for k in ("verdict", "checks", "changed", "suspicious", "cost") if k in base}
    head = {k: rep[k] for k in ("task", "status", "stopped") if rep.get(k)}
    tail = {k: str(rep[k])[:80] for k in ("why",) if rep.get(k)}
    if rep.get("claim_untrusted") and rep.get("verdict") != "pass":
        tail["claim_untrusted"] = str(rep["claim_untrusted"])[:80]
    if rep.get("error"):
        tail["error"] = rep["error"]
    room = REPORT_CHARS - 200 - len(dumps(head)) - len(dumps(tail))
    comp, trunc = V._compact(base, room)
    return {**head, **comp, **tail}, trunc


def report_view(ctx: Ctx, tid: str) -> tuple:
    root = ctx.root
    t = load(root, tid)
    rp = tpath(root, tid) / "report.json"
    if not rp.exists():
        if t["status"] not in TERMINAL:
            return {"task": tid, "status": t["status"], "verdict": None, "hint": "not finished yet",
                    "_next": ctx.next_cmd("task", "wait", tid, "--timeout", "600")}, False
        verify_task(root, tid)  # interrupted / killed runs: verify lazily
        t = load(root, tid)
        if not t.get("verdict"):
            update(root, tid, verdict=json.loads(rp.read_text())["verdict"])
    rep = json.loads(rp.read_text())
    t = load(root, tid)
    rep["status"] = t["status"]
    out, trunc = compact_report(rep)
    nxt = ctx.next_cmd("read", ctx.rel(rp)) if trunc else (
        ctx.next_cmd("task", "resume", tid) if t["status"] in RESUMABLE and not t.get("cleaned") else None)
    if nxt:
        out["_next"] = nxt
    return out, trunc


def _ids(root: Path, ids: list) -> list:
    return [load(root, i)["id"] for i in ids]


def cmd_task(ctx: Ctx, a) -> Result:
    root = ctx.root
    sub = a.task_cmd
    if sub == "create":
        out = create(ctx, a)
        nxt = ctx.next_cmd("task", "report" if out.get("status") in TERMINAL else "wait", out["id"],
                           *([] if out.get("status") in TERMINAL else ["--timeout", "600"]))
        return Result(out, False, nxt)
    if not (root / ".agentkit" / "tasks").exists() and sub != "create":
        if sub == "status" and not a.ids:
            return Result({"tasks": [], "hint": "no tasks yet: agentkit task create GOAL --check CMD"})
    if sub == "status":
        schedule(root)
        if a.ids:
            ts = [compact_status(root, load(root, i)) for i in a.ids]
            return Result(ts[0] if len(ts) == 1 else {"tasks": ts}, budget_chars=STATUS_CHARS * max(1, len(ts)) if len(ts) > 1 else STATUS_CHARS)
        ts = all_tasks(root)[-8:]
        return Result({"tasks": [compact_status(root, t) for t in ts], "total": len(all_tasks(root))})
    if sub == "wait":
        if not a.ids:
            raise AKError("wait needs task ids", "", "agentkit task wait ID [ID..] --timeout 600")
        _ids(root, a.ids)
        deadline = time.monotonic() + float(a.timeout)
        while True:
            schedule(root)
            ts = [load(root, i) for i in a.ids]
            if all(t["status"] in TERMINAL for t in ts) or time.monotonic() >= deadline:
                break
            time.sleep(0.3)
        done = all(t["status"] in TERMINAL for t in ts)
        if done and len(ts) == 1 and not getattr(a, "no_report", False):
            # C-06: one finished task -> the ≤300-token report inline, saving the host a `task report` round trip
            rep, trunc = report_view(ctx, ts[0]["id"])
            nxt = rep.pop("_next", None)
            return Result({"done": True, **rep}, trunc, nxt, budget_chars=REPORT_CHARS)
        data = {"done": done, "tasks": [compact_status(root, t) for t in ts]}
        nxt = ctx.next_cmd("task", "report", a.ids[0]) if done else ctx.next_cmd("task", "wait", *a.ids, "--timeout", str(int(float(a.timeout))))
        return Result(data, False, nxt)
    if sub == "report":
        if not a.ids:
            raise AKError("report needs a task id", "", "agentkit task report ID")
        schedule(root)
        out, trunc = report_view(ctx, a.ids[0])
        nxt = out.pop("_next", None)
        return Result(out, trunc, nxt, budget_chars=REPORT_CHARS)
    if sub == "resume":
        out = resume(ctx, a)
        return Result(out, False, ctx.next_cmd("task", "wait", out["id"], "--timeout", "600"))
    if sub == "cancel":
        if not a.ids:
            raise AKError("cancel needs task ids", "", "agentkit task cancel ID")
        out = []
        for tid in a.ids:
            out.append(cancel(root, tid))
        schedule(root)
        return Result(out[0] if len(out) == 1 else {"tasks": out})
    if sub == "clean":
        targets = a.ids or ([t["id"] for t in all_tasks(root) if t["status"] in TERMINAL] if a.all else [])
        if not targets:
            raise AKError("nothing to clean", "give task ids or --all (finished tasks only)", "agentkit task clean --all")
        cleaned = []
        for tid in targets:
            t = load(root, tid)
            if t["status"] not in TERMINAL:
                raise AKError(f"task {tid} is {t['status']}", "only finished tasks can be cleaned", f"agentkit task cancel {tid}")
            wt = root / t["worktree"]
            subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", str(wt)], capture_output=True)
            subprocess.run(["git", "-C", str(root), "worktree", "prune"], capture_output=True)
            if not a.keep_branch:
                subprocess.run(["git", "-C", str(root), "branch", "-D", t["branch"]], capture_output=True)
            update(root, tid, cleaned=True)
            cleaned.append(tid)
        return Result({"cleaned": cleaned, "branches_kept": bool(a.keep_branch)})
    raise AKError(f"unknown task subcommand '{sub}'", "use create|status|wait|report|resume|cancel|clean", "agentkit task --help")


def resume(ctx: Ctx, a) -> dict:
    """Continue a paused / interrupted / stopped task in its own worktree with a compacted transcript."""
    from . import models as M

    root = ctx.root
    if not a.ids:
        raise AKError("resume needs a task id", "", "agentkit task resume ID")
    tid = a.ids[0]
    with locked(root):
        recover(root)
        t = load(root, tid)
        if t["status"] not in RESUMABLE:
            raise AKError(f"task {tid} is {t['status']}", "only paused, interrupted or stopped tasks can be resumed",
                          "agentkit task create ... for a new attempt" if t["status"] in TERMINAL else f"agentkit task wait {tid}")
        if t.get("cleaned") or not (root / t["worktree"]).exists():
            raise AKError("task worktree is gone", "it was cleaned; nothing to resume", "agentkit task create ... to run again")
        spec = M.pick(ctx.config, t["model"])
        if spec.key_env and not os.environ.get(spec.key_env):
            raise AKError(f"environment variable {spec.key_env} is not set", f"model '{spec.name}' reads its key from it",
                          f"export {spec.key_env}=...  then  agentkit task resume {tid}")
        budget = dict(t.get("budget") or {})
        for k, v in (("tokens", a.budget_tokens), ("usd", a.budget_usd), ("minutes", a.budget_minutes)):
            if v:
                budget[k] = v
        prior = {"tokens_in": t.get("tokens_in", t.get("tokens", 0)), "tokens_out": t.get("tokens_out", 0),
                 "usd": t.get("usd", 0.0), "rounds": t.get("rounds", 0), "worker_s": t.get("worker_s", 0)}
        if t.get("stopped") == "budget" and budget == (t.get("budget") or {}):
            raise AKError(f"task {tid} is out of budget", "resuming with the same budget would stop at once",
                          f"agentkit task resume {tid} --budget-tokens N | --budget-usd X | --budget-minutes M")
        prev = t.get("reason") or t["status"]
        for k in ("verdict", "ended", "error_code", "component", "stopped", "claim", "worker_status", "pid", "started",
                  "stopped_at", "stop_requested_at", "last_step"):
            t.pop(k, None)
        t.update(status="queued", budget=budget, prior=prior, resumes=int(t.get("resumes") or 0) + 1,
                 resumed_from=prev, reason=None)
        d = tpath(root, tid)
        for f in ("report.json", "cancel"):
            (d / f).unlink(missing_ok=True)
        save(root, t)
        EventLog(root, tid, "task").emit("resume_requested", step=t.get("rounds", 0), cli_run=ctx.run_id or None)
    schedule(root)
    t = load(root, tid)
    out = {"id": tid, "status": t["status"], "resumes": t["resumes"]}
    if queue_pos(root, tid):
        out["queue_pos"] = queue_pos(root, tid)
    return out


def cancel(root: Path, tid: str) -> dict:
    with locked(root):
        t = load(root, tid)
        if t["status"] in TERMINAL:
            return compact_status(root, t)
        (tpath(root, tid) / "cancel").write_text("1")
        if t["status"] == "queued":
            t.update(status="cancelled", reason="cancelled before start", ended=time.time(), error_code="AK-TASK-CANCELLED",
                     component="task", last_step=0)
            save(root, t)
            return compact_status(root, t)
        pid = t.get("pid")
    if pid and pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        t = load(root, tid)
        if t["status"] in TERMINAL or t["status"] == "verifying":
            break
        time.sleep(0.1)
    if pid and pid_alive(pid) and load(root, tid)["status"] not in TERMINAL | {"verifying"}:
        try:
            os.killpg(int(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        update(root, tid, status="cancelled", reason="cancelled (killed)", ended=time.time(), error_code="AK-TASK-CANCELLED",
               component="task")
    t = load(root, tid)
    if t["status"] == "verifying":
        # cancelled runner skips verification; wait briefly for it to settle
        for _ in range(50):
            t = load(root, tid)
            if t["status"] in TERMINAL:
                break
            time.sleep(0.1)
    return compact_status(root, t)
