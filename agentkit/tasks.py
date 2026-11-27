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

import atexit
try:
    import fcntl
except ImportError:  # Windows: direct mode works; dispatch is rejected explicitly below.
    fcntl = None
import fnmatch
import hashlib
import json
import os
import platform
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
from types import SimpleNamespace
from typing import Optional

from . import verify as V
from .config import load_config, model_configured, saved_credentials_allowed
from .core import AKError, Ctx, Result, atomic_write, dumps, fill_lines, room_for
from .events import EventLog
from .protocol import SandboxStatus
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
    with open(p, "a+b") as f:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX)
        else:
            import msvcrt

            if f.seek(0, os.SEEK_END) == 0:
                f.write(b"\0")
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_UN)
            else:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


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


def _set_sandbox_state(root: Path, tid: str, state: str, provider: Optional[str] = None, *,
                       lock: bool = True) -> None:
    from . import sandbox as S

    task = load(root, tid)
    current = task.get("sandbox_status")
    if not isinstance(current, dict):
        current = {"sandbox_id": f"sandbox-{tid}", "task_id": tid,
                   "provider": provider or ("bubblewrap" if task.get("sandbox") == "bwrap" else "none"),
                   "capabilities": S.capabilities() if task.get("sandbox") == "bwrap" else S.no_sandbox_capabilities()}
    if provider:
        current["provider"] = provider
        current["capabilities"] = S.capabilities() if provider == "bubblewrap" else S.no_sandbox_capabilities()
    status = SandboxStatus.from_dict({**current, "state": state})
    if lock:
        update(root, tid, sandbox_status=status.to_dict())
    else:
        task["sandbox_status"] = status.to_dict()
        save(root, task)


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


def max_handoffs(cfg: dict) -> int:
    try:
        return max(0, int((cfg.get("tasks") or {}).get("max_handoffs", 2)))
    except (TypeError, ValueError):
        return 2


def _stop_proxy(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


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
            env = selected_worker_env(cfg, t.get("backend", "builtin"), t.get("model", ""))
            env["PYTHONUNBUFFERED"] = "1"
            env.update(guard_env(root))
            command = [sys.executable, "-m", "agentkit", "--root", str(root), "_task-run", t["id"]]
            base_command = command
            from . import sandbox as S

            smode = S.mode(cfg)
            sandboxed = False
            network = S.network_enabled(cfg)
            domains = None
            proxy_proc = None
            pid_file = ready_file = None
            if smode != "off":
                try:
                    backend = t.get("backend", "builtin")
                    if backend in ("codex", "opencode", "qoder", "cursor"):
                        from .backends import codex_config, cursor_config, opencode_config, qoder_config

                        backend_cfg = (codex_config(cfg) if backend == "codex" else
                                       opencode_config(cfg) if backend == "opencode" else
                                       qoder_config(cfg) if backend == "qoder" else cursor_config(cfg))
                        _check_credential_policy(cfg, backend, backend_cfg, root, "schedule", t["id"])
                    domains = S.network_domains(cfg)
                    import agentkit

                    pkg_root = Path(agentkit.__file__).resolve().parent.parent
                    host_home = Path.home()
                    host_xdg = {
                        "XDG_CONFIG_HOME": Path(os.environ.get("XDG_CONFIG_HOME") or host_home / ".config").resolve(),
                        "XDG_DATA_HOME": Path(os.environ.get("XDG_DATA_HOME") or host_home / ".local" / "share").resolve(),
                        "XDG_CACHE_HOME": Path(os.environ.get("XDG_CACHE_HOME") or host_home / ".cache").resolve(),
                        "XDG_STATE_HOME": Path(os.environ.get("XDG_STATE_HOME") or host_home / ".local" / "state").resolve(),
                        "XDG_RUNTIME_DIR": Path(os.environ.get("XDG_RUNTIME_DIR") or host_home / ".runtime").resolve(),
                    }
                    masked_paths = [host_home, *host_xdg.values()]
                    ro_paths = list(dict.fromkeys((pkg_root, root)))
                    lock_path = d.parent / ".lock"
                    open(lock_path, "a+b").close()
                    git_common = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-common-dir"],
                                                capture_output=True, text=True).stdout.strip()
                    git_dir = Path(git_common) if git_common else root / ".git"
                    if not git_dir.is_absolute():
                        git_dir = root / git_dir
                    state_dirs = [root / ".agentkit" / name for name in ("evidence", "retrospectives")]
                    backend_paths = []
                    if t.get("backend") == "opencode":
                        from .backends import opencode_state_paths

                        backend_paths = opencode_state_paths(cfg)
                    elif t.get("backend") == "qoder":
                        from .backends import qoder_config_dir, qoder_state_paths

                        qdir = qoder_config_dir(cfg, root)
                        try:
                            qdir.relative_to(root.resolve())
                            qdir_local = True
                        except ValueError:
                            qdir_local = False
                        backend_paths = qoder_state_paths(cfg, root) if saved_credentials_allowed(cfg) or qdir_local else []
                    elif t.get("backend") == "cursor":
                        from .backends import cursor_config, cursor_state_paths

                        backend_paths = cursor_state_paths(cfg, root) if cursor_config(cfg).get("config_dir") else []
                    for path in state_dirs + [git_dir / "worktrees"] + backend_paths:
                        path.mkdir(parents=True, exist_ok=True)
                    sandbox_environment(env, d, t.get("backend", "builtin"), backend_paths, host_home, host_xdg)
                    net_dir = None
                    if domains is not None:
                        net_dir = Path(tempfile.mkdtemp(prefix=f"aknet-{t['id']}-", dir=tempfile.gettempdir()))
                        socket_path = net_dir / "proxy.sock"
                        pid_file = net_dir / "worker.pid"
                        ready_file = net_dir / "ready"
                        env["AGENTKIT_NET_PROXY_SOCKET"] = str(socket_path)
                    command = S.wrap(command, root / t["worktree"], d, root / ".agentkit" / "logs",
                                     network=network, ro_paths=ro_paths, masked_paths=masked_paths,
                                     writable_paths=[lock_path, git_dir / "worktrees", *state_dirs, *backend_paths,
                                                     *([net_dir] if net_dir else [])])
                    sandboxed = True
                    if domains is not None:
                        proxy_args = [sys.executable, "-m", "agentkit.netproxy", "host", "--socket", str(socket_path),
                                      "--pid-file", str(pid_file), "--ready-file", str(ready_file)]
                        for domain in domains:
                            proxy_args += ["--allow", domain]
                        proxy_env = {"PATH": env.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
                                     "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(pkg_root)}
                        if env.get("AGENTKIT_NETPROXY_DEBUG"):
                            proxy_env["AGENTKIT_NETPROXY_DEBUG"] = "1"
                        proxy_proc = subprocess.Popen(proxy_args, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                                                      env=proxy_env, start_new_session=True, close_fds=True)
                        deadline = time.monotonic() + 5
                        while not ready_file.exists() and proxy_proc.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.02)
                        if not ready_file.exists():
                            raise S.SandboxUnavailable("sandbox network proxy failed to start")
                except S.SandboxUnavailable as e:
                    _stop_proxy(proxy_proc)
                    restricted = domains is not None or not network
                    sandboxed = False
                    command = base_command
                    domains = None
                    env.pop("AGENTKIT_NET_PROXY_SOCKET", None)
                    if smode == "required" or restricted:
                        logf.close()
                        t.update(status="failed", reason=f"restricted sandbox network required but unavailable: {e}",
                                 ended=time.time(), error_code="AK-TASK-STATE", component="task")
                        save(root, t)
                        _set_sandbox_state(root, t["id"], "failed", lock=False)
                        continue
                except AKError as e:
                    _stop_proxy(proxy_proc)
                    logf.close()
                    t.update(status="failed", reason=e.what, ended=time.time(), error_code=e.code, component="task")
                    save(root, t)
                    _set_sandbox_state(root, t["id"], "failed", lock=False)
                    continue
                except ValueError as e:
                    _stop_proxy(proxy_proc)
                    logf.close()
                    t.update(status="failed", reason=str(e), ended=time.time(), error_code="AK-CONFIG", component="task")
                    save(root, t)
                    _set_sandbox_state(root, t["id"], "failed", lock=False)
                    continue
            if sandboxed:
                env["AGENTKIT_SANDBOX"] = "1"
                if domains is not None:
                    env["AGENTKIT_NETWORK_MODE"] = "allowlist"
            _set_sandbox_state(root, t["id"], "prepared" if sandboxed else "pending",
                               "bubblewrap" if sandboxed else "none", lock=False)
            try:
                proc = subprocess.Popen(
                    command, cwd=str(root), stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                    start_new_session=True, env=env, close_fds=True,
                )
                if pid_file is not None:
                    pid_file.write_text(str(proc.pid) + "\n", encoding="ascii")
            except Exception:
                _stop_proxy(proxy_proc)
                raise
            finally:
                logf.close()
            t["status"] = "starting"
            t["pid"] = proc.pid
            t["sandbox"] = "bwrap" if sandboxed else "off"
            t["sandbox_network"] = (S.network_label(cfg) if sandboxed else "host")
            if domains is not None:
                t["sandbox_domains"] = domains
            save(root, t)
            _set_sandbox_state(root, t["id"], "running", "bubblewrap" if sandboxed else "none", lock=False)
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


_WORKER_ENV = {
    "PATH", "HOME", "USERPROFILE", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_PATH",
    "TERM", "COLORTERM", "NO_COLOR", "FORCE_COLOR", "CI", "GITHUB_ACTIONS", "RUNNER_OS",
    "AGENTKIT_MODEL", "AGENTKIT_SANDBOX", "AGENTKIT_NETWORK_MODE", "AGENTKIT_NET_PROXY_SOCKET",
    "AGENTKIT_USER_CONFIG_SNAPSHOT", "AGENTKIT_NETPROXY_DEBUG", "AGENTKIT_NO_TREESITTER",
    "AGENTKIT_SEARCH_ENGINE", "AGENTKIT_LSP_TEST_COMMAND",
}


def scrubbed_env(cfg: dict) -> dict:
    """Keep only worker runtime variables; callers add the selected backend key explicitly."""
    from .models import all_models

    env = {k: v for k, v in os.environ.items()
           if k in _WORKER_ENV or k.startswith(("PYTHON", "LC_", "XDG_"))}
    for m in all_models(cfg).values():
        if m.key_env:
            if os.environ.get(m.key_env):
                register_secret(os.environ[m.key_env])
            env.pop(m.key_env, None)
    return env


def sandbox_environment(env: dict, task_dir: Path, backend: str, backend_paths: list[Path],
                        host_home: Path, host_xdg: dict[str, Path]) -> None:
    """Keep host state invisible unless a backend path was explicitly authorized."""
    from .config import load_user_config

    task_dir.mkdir(parents=True, exist_ok=True)
    user = load_user_config()
    env["AGENTKIT_USER_CONFIG_SNAPSHOT"] = json.dumps(
        {k: user[k] for k in ("model", "models", "security") if k in user}, separators=(",", ":"))
    sandbox_home = task_dir / "home"
    sandbox_xdg = {name: task_dir / name.lower() for name in host_xdg}
    for path in [sandbox_home, *sandbox_xdg.values()]:
        path.mkdir(parents=True, exist_ok=True)
    env.update({"HOME": str(sandbox_home), "USERPROFILE": str(sandbox_home),
                **{name: str(path) for name, path in sandbox_xdg.items()}})
    for name in ("QODER_CONFIG_DIR", "CODEX_HOME", "OPENCODE_DB", "SSH_AUTH_SOCK", "SSH_ASKPASS",
                 "GPG_AGENT_INFO", "DBUS_SESSION_BUS_ADDRESS", "GIT_ASKPASS", "GIT_CREDENTIAL_HELPER",
                 "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE",
                 "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_CONFIG_DIR", "KUBECONFIG", "DOCKER_CONFIG",
                 "NPM_CONFIG_USERCONFIG", "PIP_CONFIG_FILE", "CLOUDSDK_CONFIG"):
        env.pop(name, None)
    if backend == "qoder" and backend_paths:
        env["QODER_CONFIG_DIR"] = str(backend_paths[0])
    elif backend == "opencode" and backend_paths:
        env.update({name: str(host_xdg[name]) for name in
                    ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")})


def selected_worker_env(cfg: dict, backend: str, model: str) -> dict:
    env = scrubbed_env(cfg)
    source = ""
    if backend == "builtin":
        from .models import all_models, pick

        if all_models(cfg):
            source = pick(cfg, model).key_env
    else:
        from .backends import codex_config, cursor_config, opencode_config, qoder_config

        wcfg = (codex_config(cfg) if backend == "codex" else
                opencode_config(cfg) if backend == "opencode" else
                qoder_config(cfg) if backend == "qoder" else cursor_config(cfg))
        if backend == "qoder":
            dedicated = str(wcfg.get("key_env") or "")
            source = dedicated if dedicated and os.environ.get(dedicated) else "QODER_PERSONAL_ACCESS_TOKEN"
        elif backend == "cursor":
            source = str(wcfg.get("key_env") or "CURSOR_API_KEY")
        else:
            source = str(wcfg.get("key_env") or "")
    if source and os.environ.get(source):
        env[source] = os.environ[source]
        register_secret(os.environ[source])
    return env


# ---------------------------------------------------------------- create


def content_hash(goal, checks, allow, model, tier, budget, base, baseline_must_fail=True, backend="builtin") -> str:
    blob = json.dumps([goal.strip(), list(checks), sorted(allow), model, tier, budget, base, baseline_must_fail, backend],
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def task_type(goal: str) -> str:
    g = goal.lower()
    for typ, words in (("bugfix", ("fix", "bug", "broken", "fail", "error", "修")), ("test", ("test", "测试")),
                       ("refactor", ("refactor", "rename", "clean", "重构")),
                       ("feature", ("add", "implement", "support", "create", "new", "实现", "增加"))):
        if any(w in g for w in words):
            return typ
    return "other"


def _check_backend_config_dir(path: Path, root: Path, backend: str, allowed: bool) -> None:
    path, root = path.resolve(), root.resolve()
    if path == Path(path.anchor) or path in root.parents:
        raise AKError(f"{backend} config_dir must not be the filesystem root or a repository ancestor",
                      "choose a dedicated backend state directory", code="AK-CONFIG")
    try:
        relative = path.relative_to(root)
    except ValueError as e:
        if not allowed:
            raise AKError(f"{backend} config_dir outside the repository requires user-level approval",
                          "use a repository-local directory or enable security.allow_saved_credentials in the user config",
                          code="AK-CONFIG") from e
        return
    if relative == Path(".") or any(part in (".git", ".agentkit") for part in relative.parts):
        raise AKError(f"{backend} config_dir must be an independent repository subdirectory",
                      "do not bind the repository root, .git or .agentkit as writable backend state", code="AK-CONFIG")
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--", relative.as_posix()],
                             capture_output=True, text=True).stdout.strip()
    if tracked:
        raise AKError(f"{backend} config_dir overlaps tracked repository files",
                      "choose an untracked directory dedicated to backend state", code="AK-CONFIG")


def _check_credential_policy(cfg: dict, backend: str, wcfg: dict, root: Path, command: str = "create", tid: str = "") -> None:
    allowed = saved_credentials_allowed(cfg)
    if backend == "qoder":
        key_env = str(wcfg.get("key_env") or "")
        has_key = bool(key_env and os.environ.get(key_env)) or bool(
            os.environ.get("QODER_PERSONAL_ACCESS_TOKEN"))
        if key_env and not has_key:
            raise AKError(f"environment variable {key_env} is not set",
                          "Qoder reads its dedicated PAT from it; QODER_PERSONAL_ACCESS_TOKEN is also supported",
                          f"export {key_env}=...  then  agentkit task {command} {tid} --backend qoder",
                          code="AK-MODEL-KEYENV")
        if not key_env and not has_key and not allowed:
            raise AKError("Qoder saved login requires user-level approval",
                          "set QODER_PERSONAL_ACCESS_TOKEN or enable security.allow_saved_credentials in the user config",
                          f"agentkit task {command} {tid} --backend qoder".replace("  ", " "), code="AK-CONFIG")
        raw_dir = wcfg.get("config_dir")
        if raw_dir:
            path = Path(str(raw_dir)).expanduser()
            path = path.resolve() if path.is_absolute() else (root / path).resolve()
            _check_backend_config_dir(path, root, "Qoder", allowed)
        elif allowed or os.environ.get("QODER_CONFIG_DIR"):
            from .backends import qoder_config_dir

            path = qoder_config_dir({"workers": {"qoder": wcfg}}, root)
            try:
                path.relative_to(root.resolve())
            except ValueError:
                if allowed:
                    _check_backend_config_dir(path, root, "Qoder", True)
            else:
                _check_backend_config_dir(path, root, "Qoder", True)
    elif backend == "opencode" and wcfg.get("allow_account_login", False) and not allowed:
        raise AKError("OpenCode saved login requires user-level approval",
                      "enable security.allow_saved_credentials in the user config or use provider API keys",
                      code="AK-CONFIG")
    elif backend == "cursor" and wcfg.get("config_dir"):
        path = Path(str(wcfg["config_dir"])).expanduser()
        path = path.resolve() if path.is_absolute() else (root / path).resolve()
        _check_backend_config_dir(path, root, "Cursor", allowed)


def create(ctx: Ctx, a) -> dict:
    from . import models as M

    cfg = ctx.config
    backend = getattr(a, "backend", "builtin") or "builtin"
    if backend not in ("builtin", "codex", "opencode", "qoder", "cursor"):
        raise AKError("unknown worker backend", "use builtin, codex, opencode, qoder or cursor", "agentkit task create --help", code="AK-USAGE")
    if backend == "builtin" and (not model_configured(cfg) or not M.all_models(cfg)):
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
    if os.name != "posix":
        raise AKError("dispatch mode requires Linux", "Windows supports direct mode only until process and filesystem sandboxing are verified",
                      "use agentkit read/search/edit/run/verify directly", code="AK-TASK-STATE")
    base = head.stdout.strip()
    try:
        from . import sandbox as S

        S.network_domains(cfg)
    except ValueError as e:
        raise AKError(str(e), "fix [sandbox] network allowlist", "agentkit doctor", code="AK-CONFIG")
    spec = None
    if backend in ("codex", "opencode", "qoder", "cursor"):
        from . import backends as B
        from . import sandbox as S

        label = backend.capitalize()
        wcfg = getattr(B, f"{backend}_config")(cfg)
        if not getattr(B, f"{backend}_available")(cfg):
            raise AKError(f"{backend} executable not found", f"set workers.{backend}.command or install {label} CLI",
                          "agentkit doctor", code="AK-MODEL-NONE")
        if not wcfg.get("allow_unsandboxed", False) and (S.mode(cfg) == "off" or not S.available()):
            raise AKError(f"{backend} backend requires a working sandbox", "install bubblewrap or set [sandbox] mode=required",
                          "agentkit doctor", code="AK-TASK-STATE")
        model_name = str(a.model or wcfg.get("model") or "")
        if backend == "cursor":
            key_env = str(wcfg.get("key_env") or "CURSOR_API_KEY")
            if not os.environ.get(key_env):
                raise AKError(f"environment variable {key_env} is not set",
                              "Cursor runs with an isolated config directory and requires an API key",
                              f"export {key_env}=... then agentkit task create --backend cursor", code="AK-MODEL-KEYENV")
            if wcfg.get("transport", "cli") == "sdk" and not model_name:
                raise AKError("Cursor SDK requires an explicit model", "set workers.cursor.model or pass --model",
                              "agentkit task create --help", code="AK-USAGE")
        _check_credential_policy(cfg, backend, wcfg, root)
        if backend == "opencode" and model_name and (len(model_name.split("/", 1)) != 2 or
                                                     not all(model_name.split("/", 1))):
            raise AKError("OpenCode model must use provider/model format", "for example openai/gpt-5.1",
                          "agentkit task create --help", code="AK-USAGE")
        tier = ""
        try:
            getattr(B, f"{backend}_prices")(cfg)
            if backend == "opencode":
                B.opencode_usd_budget_known(cfg)
            elif backend == "qoder":
                B.qoder_usd_budget_known(cfg)
        except ValueError as e:
            raise AKError(str(e), f"fix [workers.{backend}]", "agentkit doctor", code="AK-CONFIG")
    else:
        spec = M.pick(cfg, a.model, a.tier)
        model_name, tier = spec.name, spec.tier
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
    if backend == "cursor" and wcfg.get("transport", "cli") == "cli" and budget["tokens"]:
        raise AKError("Cursor stream-json does not report token usage",
                      "use --budget-minutes; token budgets require a backend with usage events",
                      "agentkit task create --help", code="AK-USAGE")
    if backend in ("codex", "opencode", "qoder", "cursor") and budget["usd"]:
        from . import backends as B

        known = (any(B.codex_prices(cfg)) if backend == "codex" else
                 B.opencode_usd_budget_known(cfg) if backend == "opencode" else
                 B.qoder_usd_budget_known(cfg) if backend == "qoder" else B.cursor_usd_budget_known(cfg))
        if not known:
            raise AKError(f"{backend.capitalize()} cost is unknown",
                          f"configure workers.{backend}.price_in/price_out or remove --budget-usd",
                          "agentkit task create --help", code="AK-USAGE")
    baseline_must_fail = not bool(getattr(a, "allow_baseline_pass", False))
    h = content_hash(a.goal, a.check or [], a.allow or [], model_name, tier, budget, base, baseline_must_fail, backend)
    acceptance = {"checks": a.check or [], "allow": a.allow or [], "baseline_must_fail": baseline_must_fail}
    acceptance_sha256 = hashlib.sha256(json.dumps(acceptance, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
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
            "id": tid, "goal": a.goal, "checks": a.check or [], "allow": a.allow or [], "model": model_name,
            "tier": tier, "backend": backend, "budget": budget, "hash": h, "base": base, "branch": branch,
            "acceptance": acceptance, "acceptance_sha256": acceptance_sha256,
            "worktree": str(wt.relative_to(root)), "status": "queued", "created": time.time(),
            "type": task_type(a.goal), "rounds": 0, "tokens": 0, "usd": 0.0,
        }
        from . import sandbox as S

        provider = "bubblewrap" if S.mode(cfg) != "off" else "none"
        t["sandbox_status"] = SandboxStatus(sandbox_id=f"sandbox-{tid}", task_id=tid, provider=provider,
                                            state="pending",
                                            capabilities=S.capabilities() if provider == "bubblewrap" else S.no_sandbox_capabilities()).to_dict()
        save(root, t)
        EventLog(root, tid, "task").emit("created", step=0, model=model_name or backend, backend=backend,
                                         cli_run=ctx.run_id or None)
    schedule(root)
    t = load(root, tid)
    # C-06: lean output. model / branch are known to the host (branch = agentkit/<id>); next goes in the envelope
    out = {"id": tid, "status": t["status"]}
    qp = queue_pos(root, tid)
    if qp:
        out["queue_pos"] = qp
    if backend != "builtin":
        out["backend"] = backend
    elif a.model is None and len(M.all_models(cfg)) > 1:
        out["model"] = spec.name
    if warnings:
        out["warnings"] = warnings
    if not a.check:
        out.setdefault("warnings", []).append("no --check given: the verdict can only be 'invalid'; pass --check '<test cmd>'")
    return out


# ---------------------------------------------------------------- runner (detached process)


def run_task(root: Path, tid: str) -> None:
    """Entry point of the detached worker process."""
    if os.name != "posix":
        update(root, tid, status="failed", reason="dispatch requires Linux", error_code="AK-TASK-STATE", component="task")
        return
    from . import memory
    from .models import pick
    from .run import kill_active
    from .worker import StopWorker, Worker

    cfg = load_config(root)
    wcfg = dict(cfg.get("worker") or {})
    wcfg["compress"] = dict(cfg.get("compress") or {})  # C-08
    wcfg["codeintel"] = dict(cfg.get("codeintel") or {})
    running = {"status": "running", "started": time.time()}
    if not os.environ.get("AGENTKIT_SANDBOX"):
        running["pid"] = os.getpid()
    t = update(root, tid, **running)
    events = EventLog(root, tid, "task")
    events.emit("started", step=0, pid=t.get("pid"))
    d = tpath(root, tid)
    relay_proc = None
    proxy_socket = os.environ.get("AGENTKIT_NET_PROXY_SOCKET")
    if proxy_socket:
        from .netproxy import start_relay

        try:
            relay_proc, proxy_url = start_relay(Path(proxy_socket), d / "network-relay.port",
                                                d / "network-relay.log")
        except Exception as e:
            update(root, tid, status="failed", reason=f"sandbox network relay failed: {e}", error_code="AK-TASK-STATE",
                   component="task")
            events.emit("crash", step=0, component="task", error_code="AK-TASK-STATE")
            return
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ[key] = proxy_url
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = ""

        def stop_relay():
            if relay_proc is not None and relay_proc.poll() is None:
                relay_proc.terminate()
                try:
                    relay_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    relay_proc.kill()

        atexit.register(stop_relay)
    backend = t.get("backend", "builtin")
    original_env = dict(os.environ)
    spec = None
    if backend in ("codex", "opencode", "qoder", "cursor"):
        from .backends import codex_config, cursor_config, opencode_config, qoder_config

        clean = scrubbed_env(cfg)
        wcfg = (codex_config(cfg) if backend == "codex" else
                opencode_config(cfg) if backend == "opencode" else
                qoder_config(cfg) if backend == "qoder" else cursor_config(cfg))
        key_env = str(wcfg.get("key_env") or "")
        if backend == "qoder":
            source_key = key_env if key_env and original_env.get(key_env) else "QODER_PERSONAL_ACCESS_TOKEN"
            if original_env.get(source_key):
                clean[source_key] = original_env[source_key]
                register_secret(original_env[source_key])
        if backend == "cursor":
            source_key = str(wcfg.get("key_env") or "CURSOR_API_KEY")
            if original_env.get(source_key):
                clean["CURSOR_API_KEY"] = original_env[source_key]
                register_secret(original_env[source_key])
            if source_key != "CURSOR_API_KEY":
                clean.pop(source_key, None)
        if backend not in ("cursor", "qoder") and key_env and original_env.get(key_env):
            clean[key_env] = original_env[key_env]
            register_secret(original_env[key_env])
    else:
        spec = pick(cfg, t["model"])
        spec.key()
        keyval = original_env.get(spec.key_env) if spec.key_env else None
        clean = scrubbed_env(cfg)
        if spec.key_env and keyval:
            spec_key_env = spec.key_env
            spec.key = lambda: keyval  # type: ignore[method-assign]
            _ = spec_key_env
    for k in list(os.environ):
        if k not in clean:
            os.environ.pop(k, None)
    os.environ.update(guard_env(root))
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"  # edit+rerun within 1 s must not hit a stale .pyc
    wt = root / t["worktree"]
    os.chdir(wt)
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
            extra = {k: stats[k] for k in ("credits", "credits_known") if k in stats}
            update(root, tid, rounds=stats["rounds"], tokens=stats["tokens_in"] + stats["tokens_out"],
                   usd=stats["usd"], **extra)
        except Exception:
            pass

    if backend in ("codex", "opencode", "qoder", "cursor"):
        from .backends import CodexBackend, CursorBackend, OpenCodeBackend, QoderBackend

        cls = (CodexBackend if backend == "codex" else OpenCodeBackend if backend == "opencode"
               else QoderBackend if backend == "qoder" else CursorBackend)
        backend_options = {"root": root} if backend in ("qoder", "cursor") else {}
        worker = cls(cfg, t, wt, d, **backend_options, on_progress=progress, check_stop=lambda: state["stop"],
                     events=events, max_rounds=int(wcfg.get("max_rounds", 40)))
    else:
        worker = Worker(spec, wt, root, t, on_progress=progress, transcript=d / "transcript.jsonl", cfg=wcfg,
                        check_stop=lambda: state["stop"], events=events)
    capabilities = worker.capabilities()
    update(root, tid, backend_capabilities=capabilities)
    events.emit("backend_capabilities", step=worker.rounds, component=backend, capabilities=capabilities)
    brief_txt = ""
    try:
        b = memory.brief(Ctx(root=root, cwd=root, budget=500))
        brief_txt = dumps(b.data)
    except Exception:
        pass
    resume_txt = None
    if t.get("resumes"):
        if backend in ("codex", "opencode", "qoder", "cursor"):
            diff = subprocess.run(["git", "-C", str(wt), "diff", "--no-ext-diff", t["base"], "--"],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120).stdout
            resume_txt = "Continue from the existing worktree and current diff. Do not repeat completed work.\n" + diff[:6000]
        else:
            from .worker import resume_context

            resume_txt = resume_context(d / "transcript.jsonl", wt, t["base"], {**t, "reason": t.get("resumed_from")})
        events.emit("resumed", step=worker.rounds, resume=t["resumes"])
    code = None
    outcome = None
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
                  error_code=code, component=worker.last_component if code else None, last_step=worker.last_step,
                  cost_known=st.get("cost_known", True),
                  question=outcome.question if outcome else None, waiting=bool(outcome and outcome.question))
    for key in ("credits", "credits_known"):
        if key in st:
            fields[key] = st[key]
    backend_session = getattr(worker, "session_id", "") or getattr(worker, "thread_id", "")
    if backend_session:
        fields["backend_session"] = backend_session
    if backend == "cursor":
        fields["backend_transport"] = worker.transport
    events.emit("worker_end", step=worker.rounds, component=worker.last_component, error_code=code, status=status,
                duration=time.monotonic() - t0)
    if state["stop_at"]:
        fields["stopped_at"] = time.time()
        fields["stop_requested_at"] = state["stop_at"]
    update(root, tid, **fields)
    final = "done" if status == "submitted" else status
    try:
        if final != "cancelled":
            _set_sandbox_state(root, tid, "verifying")
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
    sandbox_state = ("completed" if final == "done" else "cancelled" if final == "cancelled" else
                     "paused" if final in ("paused", "stopped", "interrupted") else "failed")
    _set_sandbox_state(root, tid, sandbox_state)
    if not os.environ.get("AGENTKIT_SANDBOX"):
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


def _diff_sha256(wt: Path, changes: dict) -> str:
    h = hashlib.sha256()
    for rel, status in sorted(changes.items()):
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(b"\0" + status.encode() + b"\0")
        p = wt / rel
        if status != "D" and p.is_file():
            h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _environment_sha256(root: Path) -> str:
    files = ("pyproject.toml", "requirements.txt", "package-lock.json", "pnpm-lock.yaml", "uv.lock",
             "poetry.lock", "Cargo.lock", "go.sum")
    h = hashlib.sha256()
    h.update(platform.platform().encode())
    h.update(sys.version.encode())
    for name in files:
        p = root / name
        if p.is_file():
            h.update(name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def _write_evidence(root: Path, t: dict, wt: Path, changes: dict, checks: list, verdict: str,
                    flags: list, report: dict) -> tuple[dict, dict]:
    from . import __version__
    from .protocol import EvidenceReceipt, Retrospective, canonical_json, sha256

    acceptance = t.get("acceptance") or {"checks": t.get("checks") or [], "baseline_must_fail": True}
    acceptance_hash = t.get("acceptance_sha256") or sha256(acceptance)
    receipt_checks = []
    for item in checks:
        c = dict(item)
        log = c.get("log")
        if log:
            lp = root / ".agentkit" / "logs"
            hashes = {}
            for stream in ("stdout", "stderr"):
                p = lp / f"{log}.{stream}.log"
                if p.is_file():
                    hashes[stream] = hashlib.sha256(p.read_bytes()).hexdigest()
            if hashes:
                c["log_sha256"] = hashes
        receipt_checks.append(c)
    diff_hash = _diff_sha256(wt, changes)
    env_hash = _environment_sha256(root)
    receipt_id = "er_" + sha256({"task": t["id"], "diff": diff_hash, "acceptance": acceptance_hash,
                                 "checks": receipt_checks, "verdict": verdict})[:24]
    evidence_dir = root / ".agentkit" / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = evidence_dir / f"{receipt_id}.json"
    receipt = EvidenceReceipt(
        receipt_id=receipt_id, subject={"kind": "task", "id": t["id"]}, base_commit=t["base"],
        diff_sha256=diff_hash, acceptance_sha256=acceptance_hash, environment_sha256=env_hash,
        checks=receipt_checks, verdict=verdict, suspicious=flags, verifier_version=__version__,
        worker={"backend": t.get("backend", "builtin"), "model": t.get("model"), "sandbox": t.get("sandbox", "unknown"),
                "sandbox_network": t.get("sandbox_network", "unknown"),
                **({"sandbox_domains": t["sandbox_domains"]} if "sandbox_domains" in t else {})},
        cost=report["cost"], created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    atomic_write(receipt_path, (canonical_json(receipt.to_dict()) + "\n").encode("utf-8"))
    receipt_data = json.loads(receipt_path.read_text(encoding="utf-8"))
    task_receipt = tpath(root, t["id"]) / "receipt.json"
    atomic_write(task_receipt, (canonical_json(receipt_data) + "\n").encode("utf-8"))

    retro_id = "rp_" + t["id"]
    retro_dir = root / ".agentkit" / "retrospectives"
    retro_dir.mkdir(parents=True, exist_ok=True)
    retro_path = retro_dir / f"{retro_id}.json"
    failures = [{"cmd": c.get("cmd"), "why": c.get("why")} for c in checks if c.get("result") != "pass"]
    next_actions = []
    if verdict == "pass":
        next_actions.append("review receipt and merge the task branch")
    elif verdict == "needs_review":
        next_actions.append("inspect suspicious flags and diff before merging")
    elif verdict == "invalid":
        next_actions.append("fix acceptance checks or baseline discrimination")
    else:
        next_actions.append("inspect failed checks and resume or create a new task")
    retro = Retrospective(
        id=retro_id, subject={"kind": "task", "id": t["id"]}, outcome=verdict,
        changes=[{"status": status, "path": rel} for rel, status in sorted(changes.items())],
        evidence=[{"receipt": receipt_id}], failures=failures, cost=report["cost"],
        next_actions=next_actions, created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    atomic_write(retro_path, (canonical_json(retro.to_dict()) + "\n").encode("utf-8"))
    report["receipt"] = {"id": receipt_id, "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}
    return report, json.loads(retro_path.read_text(encoding="utf-8"))


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
        log_dir = root / ".agentkit" / "logs"
        env = scrubbed_env(cfg)
        acceptance = t.get("acceptance") or {}
        baseline_required = bool(acceptance.get("baseline_must_fail", True))
        baseline_checks = []
        if baseline_required and t.get("checks"):
            baseline_checks = V.run_checks(t.get("checks") or [], clean, timeout, log_dir, env=env)
            _git(clean, "reset", "--hard", base)
            _git(clean, "clean", "-fdx")
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
        checks = V.run_checks(t.get("checks") or [], clean, timeout, log_dir, env=env) if t.get("checks") else []
    finally:
        subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", str(clean)], capture_output=True)
        subprocess.run(["git", "-C", str(root), "worktree", "prune"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    baseline_valid = not any(c["result"] == "invalid" for c in baseline_checks)
    baseline_failed = any(c["result"] == "fail" for c in baseline_checks)
    baseline_ok = not baseline_required or (baseline_valid and baseline_failed)
    if not checks or not baseline_ok or any(c["result"] == "invalid" for c in checks):
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
        "baseline": {"required": baseline_required,
                     "result": "skipped" if not baseline_required else "fail" if baseline_failed else "pass" if baseline_valid else "invalid",
                     "checks": baseline_checks},
        "acceptance_sha256": t.get("acceptance_sha256"),
        "changed": [f"{s} {p}" for p, s in sorted(changes.items())],
        "suspicious": flags,
        "cost": {"s": round((t.get("worker_s") or 0) + time.monotonic() - t0, 1), "tokens": t.get("tokens", 0),
                 "usd": round(t.get("usd", 0.0), 4), "usd_known": bool(t.get("cost_known", True)),
                 **({"credits": round(t["credits"], 4), "credits_known": bool(t.get("credits_known", True))}
                    if "credits" in t else {})},
        "env": "git-worktree",
        "sandbox": t.get("sandbox", "unknown"),
        "sandbox_network": t.get("sandbox_network", "unknown"),
        "branch": t.get("branch"),
    }
    if t.get("stopped"):
        report["stopped"] = t["stopped"]
    if t.get("reason") and t.get("worker_status") != "submitted":
        report["why"] = t["reason"][:160]
    if not baseline_ok:
        report["why"] = "baseline checks did not fail as required; acceptance cannot prove this task"
    if t.get("claim"):
        report["claim_untrusted"] = t["claim"][:120]
    if t.get("question"):
        report["question"] = t["question"]
        report["waiting"] = True
    code = t.get("error_code") or VERDICT_CODES.get(verdict)
    if code:
        report["error"] = {"code": code, "component": t.get("component") or "verifier",
                           "step": t.get("last_step", t.get("rounds", 0)), "log": f"agentkit logs {tid} --errors"}
    report, _retrospective = _write_evidence(root, t, wt, changes, checks, verdict, flags, report)
    atomic_write(tpath(root, tid) / "report.json", json.dumps(redact(report), ensure_ascii=False, indent=1).encode("utf-8"))
    return report


# ---------------------------------------------------------------- auto memory (M-21)


def learn(root: Path, t: dict) -> None:
    if t.get("learned"):
        return
    from . import memory

    ctx = Ctx(root=root, cwd=root)
    rep_p = tpath(root, t["id"]) / "report.json"
    rep = json.loads(rep_p.read_text()) if rep_p.exists() else {}
    if rep.get("suspicious"):
        update(root, t["id"], learned=True)
        return  # never learn from a run that looks like cheating
    stats_p = ctx.state("state") / "model-stats.json"
    try:
        stats = json.loads(stats_p.read_text())
    except (FileNotFoundError, ValueError):
        stats = {}
    passed = t.get("status") == "done" and t.get("verdict") == "pass"
    s = stats.setdefault(t["model"], {}).setdefault(t.get("type", "other"), {"pass": 0, "fail": 0})
    s["pass" if passed else "fail"] += 1
    atomic_write(stats_p, json.dumps(stats, indent=1).encode("utf-8"))
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
    update(root, t["id"], learned=True)


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
    if t.get("handoffs"):
        s["handoffs"] = t["handoffs"]
    if t.get("question"):
        s["waiting"] = True
        s["question"] = t["question"]["question"][:120]
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
    baseline = rep.get("baseline")
    if isinstance(baseline, dict):
        rep["baseline"] = {k: baseline[k] for k in ("required", "result") if k in baseline}
    base = {k: rep[k] for k in ("verdict", "changed", "suspicious", "cost", "baseline") if k in rep}
    base["checks"] = checks
    base = {k: base[k] for k in ("verdict", "checks", "baseline", "changed", "suspicious", "cost") if k in base}
    head = {k: rep[k] for k in ("task", "status", "stopped", "sandbox", "sandbox_network", "receipt") if rep.get(k)}
    tail = {k: str(rep[k])[:80] for k in ("why",) if rep.get(k)}
    if rep.get("claim_untrusted") and rep.get("verdict") != "pass":
        tail["claim_untrusted"] = str(rep["claim_untrusted"])[:80]
    if rep.get("error"):
        tail["error"] = rep["error"]
    if rep.get("question"):
        tail["question"] = rep["question"]
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
    try:
        learn(root, t)
    except Exception:
        pass
    rep["status"] = t["status"]
    out, trunc = compact_report(rep)
    nxt = ctx.next_cmd("read", ctx.rel(rp)) if trunc else (
        ctx.next_cmd("task", "resume", tid) if t["status"] in RESUMABLE and not t.get("cleaned") else None)
    if not nxt:
        # C-12: at most one cheap feedback hint, only when the run ended with a signal worth reporting
        from .diag import feedback_hint

        nxt = feedback_hint(root, tid, t=t, rep=rep)
    if nxt:
        out["_next"] = nxt
    return out, trunc


def task_diff(ctx: Ctx, tid: str, offset: int = 0) -> Result:
    t = load(ctx.root, tid)
    wt = ctx.root / t["worktree"]
    if not wt.exists():
        raise AKError("task worktree is gone", "it was cleaned", "agentkit task create ... to run again", code="AK-TASK-STATE")
    r = subprocess.run(["git", "-C", str(wt), "diff", "--no-ext-diff", t["base"], "--"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        raise AKError("git diff failed", r.stderr[-300:], "agentkit doctor", code="AK-GIT")
    lines = r.stdout.splitlines()
    untracked = subprocess.run(["git", "-C", str(wt), "ls-files", "--others", "--exclude-standard", "-z"],
                               capture_output=True, timeout=60).stdout.split(b"\0")
    for raw in untracked:
        if not raw:
            continue
        rel = os.fsdecode(raw)
        try:
            _src, real = V._inside_worktree(wt, rel)
        except AKError:
            continue
        if real.is_file():
            lines += [f"diff --git a/{rel} b/{rel}", "new file mode 100644", "--- /dev/null", f"+++ b/{rel}"]
            lines += ["+" + line for line in real.read_text(encoding="utf-8", errors="replace").splitlines()]
    sk = {"task": tid, "offset": offset, "diff": []}
    taken, trunc = fill_lines(lines[offset:], room_for(ctx, sk, reserve=220))
    sk["diff"] = taken
    nxt = ctx.next_cmd("task", "diff", tid, "--offset", str(offset + len(taken))) if trunc else None
    return Result(sk, trunc, nxt)


def steer(root: Path, tid: str, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise AKError("steer needs text", "", "agentkit task steer ID 'constraint'", code="AK-USAGE")
    with locked(root):
        t = load(root, tid)
        if t["status"] not in {"queued", "paused", "interrupted", "stopped"}:
            raise AKError(f"task {tid} is {t['status']}", "steer is allowed before start or while paused", None, code="AK-TASK-STATE")
        t["goal"] = t["goal"] + "\nSTEER: " + text
        t.setdefault("steering", []).append(text)
        save(root, t)
    return {"id": tid, "status": t["status"], "steering": t["steering"][-3:]}


def answer(ctx: Ctx, tid: str, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise AKError("answer needs text", "", f"agentkit task answer {tid} 'answer'", code="AK-USAGE")
    with locked(ctx.root):
        t = load(ctx.root, tid)
        if not t.get("question"):
            raise AKError(f"task {tid} is not waiting for an answer", "no pending worker question", None, code="AK-TASK-STATE")
        t["goal"] = t["goal"] + "\nANSWER: " + text
        t.setdefault("answers", []).append({"question": t["question"], "answer": text, "at": time.time()})
        t.pop("question", None)
        t.pop("waiting", None)
        save(ctx.root, t)
    a = SimpleNamespace(ids=[tid], model=None, budget_tokens=None, budget_usd=None, budget_minutes=None)
    return resume(ctx, a)


def decide(root: Path, tid: str, accept: bool, reason: str = "") -> dict:
    with locked(root):
        t = load(root, tid)
        if accept:
            if t.get("verdict") != "pass":
                raise AKError(f"task {tid} is not verified pass", "only a clean pass can be accepted", None, code="AK-TASK-STATE")
            t.update(accepted=True, accepted_at=time.time())
        else:
            t.update(rejected=True, rejected_at=time.time(), rejection_reason=(reason or "rejected by supervisor")[:500])
        save(root, t)
    return {"id": tid, "accepted": bool(t.get("accepted")), "rejected": bool(t.get("rejected")),
            "verdict": t.get("verdict"), "merged": False,
            "next": f"review task diff {tid}, then merge agentkit/{t['branch'].split('/', 1)[-1]} yourself"}


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
        if done:
            for task in ts:
                try:
                    learn(root, task)
                except Exception:
                    pass
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
    if sub == "handoff":
        out = resume(ctx, a, handoff=True)
        return Result(out, False, ctx.next_cmd("task", "wait", out["id"], "--timeout", "600"))
    if sub == "diff":
        if not a.ids:
            raise AKError("diff needs a task id", "", "agentkit task diff ID", code="AK-USAGE")
        return task_diff(ctx, a.ids[0], a.offset)
    if sub == "steer":
        if not a.ids:
            raise AKError("steer needs a task id", "", "agentkit task steer ID 'constraint'", code="AK-USAGE")
        return Result(steer(root, a.ids[0], a.text))
    if sub == "answer":
        if not a.ids:
            raise AKError("answer needs a task id", "", "agentkit task answer ID 'answer'", code="AK-USAGE")
        out = answer(ctx, a.ids[0], a.text)
        return Result(out, False, ctx.next_cmd("task", "wait", out["id"], "--timeout", "600"))
    if sub == "accept":
        if not a.ids:
            raise AKError("accept needs a task id", "", "agentkit task accept ID", code="AK-USAGE")
        return Result(decide(root, a.ids[0], True))
    if sub == "reject":
        if not a.ids:
            raise AKError("reject needs a task id", "", "agentkit task reject ID 'reason'", code="AK-USAGE")
        return Result(decide(root, a.ids[0], False, a.reason or ""))
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
            _set_sandbox_state(root, tid, "destroyed")
            cleaned.append(tid)
        return Result({"cleaned": cleaned, "branches_kept": bool(a.keep_branch)})
    raise AKError(f"unknown task subcommand '{sub}'", "use create|status|wait|report|resume|cancel|clean", "agentkit task --help")


def resume(ctx: Ctx, a, handoff: bool = False) -> dict:
    """Continue a paused/interrupted/stopped task, optionally switching model in place."""
    from . import models as M

    root = ctx.root
    command = "handoff" if handoff else "resume"
    try:
        from . import sandbox as S

        S.network_domains(ctx.config)
    except ValueError as e:
        raise AKError(str(e), "fix [sandbox] network allowlist", "agentkit doctor", code="AK-CONFIG")
    if not a.ids:
        raise AKError(f"{command} needs a task id", "", f"agentkit task {command} ID" + (" --model NAME" if handoff else ""))
    tid = a.ids[0]
    requested_model = getattr(a, "model", None)
    requested_backend = getattr(a, "backend", None)
    if handoff and not requested_model and not requested_backend:
        raise AKError("handoff needs --model or --backend", "choose another worker model or backend",
                      f"agentkit task handoff {tid} --backend codex|opencode|qoder|cursor", code="AK-USAGE")
    with locked(root):
        recover(root)
        t = load(root, tid)
        if t["status"] not in RESUMABLE:
            raise AKError(f"task {tid} is {t['status']}", "only paused, interrupted or stopped tasks can be resumed",
                          "agentkit task create ... for a new attempt" if t["status"] in TERMINAL else f"agentkit task wait {tid}")
        if t.get("cleaned") or not (root / t["worktree"]).exists():
            raise AKError("task worktree is gone", "it was cleaned; nothing to resume", "agentkit task create ... to run again")
        if t.get("question"):
            raise AKError("task is waiting for a supervisor answer", str(t["question"].get("question", ""))[:200],
                          f"agentkit task answer {tid} 'answer'", code="AK-TASK-WAITING")
        old_model = t["model"]
        old_backend = t.get("backend", "builtin")
        target_backend = requested_backend or old_backend
        if target_backend not in ("builtin", "codex", "opencode", "qoder", "cursor"):
            raise AKError("unknown worker backend", "use builtin, codex, opencode, qoder or cursor", "agentkit task resume --help", code="AK-USAGE")
        spec = None
        if target_backend in ("codex", "opencode", "qoder", "cursor"):
            from . import backends as B
            from . import sandbox as S

            label = target_backend.capitalize()
            wcfg = getattr(B, f"{target_backend}_config")(ctx.config)
            if not getattr(B, f"{target_backend}_available")(ctx.config):
                raise AKError(f"{target_backend} executable not found",
                              f"set workers.{target_backend}.command or install {label} CLI",
                              "agentkit doctor", code="AK-MODEL-NONE")
            if not wcfg.get("allow_unsandboxed", False) and (S.mode(ctx.config) == "off" or not S.available()):
                raise AKError(f"{target_backend} backend requires a working sandbox",
                              "install bubblewrap or set [sandbox] mode=required",
                              "agentkit doctor", code="AK-TASK-STATE")
            model_name = requested_model or (old_model if old_backend == target_backend else wcfg.get("model") or "")
            if target_backend == "opencode" and model_name and (len(model_name.split("/", 1)) != 2 or
                                                                 not all(model_name.split("/", 1))):
                raise AKError("OpenCode model must use provider/model format", "for example openai/gpt-5.1",
                              f"agentkit task {command} {tid} --model provider/model", code="AK-USAGE")
            if target_backend == "cursor" and wcfg.get("transport", "cli") == "sdk" and not model_name:
                raise AKError("Cursor SDK requires an explicit model", "set workers.cursor.model or pass --model",
                              f"agentkit task {command} {tid} --model MODEL", code="AK-USAGE")
            _check_credential_policy(ctx.config, target_backend, wcfg, root, command, tid)
            tier = ""
            try:
                getattr(B, f"{target_backend}_prices")(ctx.config)
                if target_backend == "opencode":
                    B.opencode_usd_budget_known(ctx.config)
                elif target_backend == "qoder":
                    B.qoder_usd_budget_known(ctx.config)
            except ValueError as e:
                raise AKError(str(e), f"fix [workers.{target_backend}]", "agentkit doctor", code="AK-CONFIG")
            key_env = str(wcfg.get("key_env") or ("CURSOR_API_KEY" if target_backend == "cursor" else ""))
            if key_env and not os.environ.get(key_env) and target_backend != "qoder":
                raise AKError(f"environment variable {key_env} is not set", f"{label} reads its key from it",
                              f"export {key_env}=...  then  agentkit task {command} {tid}", code="AK-MODEL-KEYENV")
        else:
            if old_backend in ("codex", "opencode", "qoder", "cursor") and not requested_model:
                raise AKError("switching to builtin needs --model", "choose a configured AgentKit model",
                              f"agentkit task {command} {tid} --model NAME", code="AK-USAGE")
            spec = M.pick(ctx.config, requested_model or old_model)
            model_name, tier = spec.name, spec.tier
            if spec.key_env and not os.environ.get(spec.key_env):
                raise AKError(f"environment variable {spec.key_env} is not set", f"model '{spec.name}' reads its key from it",
                              f"export {spec.key_env}=...  then  agentkit task {command} {tid}")
        switched = target_backend != old_backend or model_name != old_model
        if handoff and not switched:
            raise AKError(f"task {tid} already uses model '{old_model}' on {old_backend}", "handoff requires a different model or backend",
                          f"agentkit task handoff {tid} --backend codex")
        if switched:
            handoff_limit = max_handoffs(ctx.config)
            if int(t.get("handoffs") or 0) >= handoff_limit:
                raise AKError(f"task {tid} reached max_handoffs={handoff_limit}", "resume with the current worker or create a new task",
                              f"agentkit task resume {tid}")
        budget = dict(t.get("budget") or {})
        for k, v in (("tokens", a.budget_tokens), ("usd", a.budget_usd), ("minutes", a.budget_minutes)):
            if v:
                budget[k] = v
        if target_backend == "cursor" and wcfg.get("transport", "cli") == "cli" and budget["tokens"]:
            raise AKError("Cursor stream-json does not report token usage",
                          "use --budget-minutes; token budgets require a backend with usage events",
                          f"agentkit task {command} {tid}", code="AK-USAGE")
        if target_backend in ("codex", "opencode", "qoder", "cursor") and budget["usd"]:
            from . import backends as B

            known = (any(B.codex_prices(ctx.config)) if target_backend == "codex" else
                     B.opencode_usd_budget_known(ctx.config) if target_backend == "opencode" else
                     B.qoder_usd_budget_known(ctx.config) if target_backend == "qoder"
                     else B.cursor_usd_budget_known(ctx.config))
            if not known:
                raise AKError(f"{target_backend.capitalize()} cost is unknown",
                              f"configure workers.{target_backend}.price_in/price_out or remove --budget-usd",
                              "agentkit task resume --help", code="AK-USAGE")
        prior = {"tokens_in": t.get("tokens_in", t.get("tokens", 0)), "tokens_out": t.get("tokens_out", 0),
                 "usd": t.get("usd", 0.0), "rounds": t.get("rounds", 0), "worker_s": t.get("worker_s", 0),
                 "credits": t.get("credits", 0.0)}
        if t.get("stopped") == "budget" and budget == (t.get("budget") or {}):
            raise AKError(f"task {tid} is out of budget", "resuming with the same budget would stop at once",
                          f"agentkit task resume {tid} --budget-tokens N | --budget-usd X | --budget-minutes M")
        prev = t.get("reason") or t["status"]
        for k in ("verdict", "ended", "error_code", "component", "stopped", "claim", "worker_status", "pid", "started",
                  "stopped_at", "stop_requested_at", "last_step"):
            t.pop(k, None)
        t.update(status="queued", budget=budget, prior=prior, resumes=int(t.get("resumes") or 0) + 1,
                 resumed_from=prev, reason=None)
        if switched:
            history = list(t.get("model_history") or [])
            history.append({"from": old_model, "to": model_name, "reason": prev, "at": time.time()})
            backends = list(t.get("backend_history") or [])
            backends.append({"from": old_backend, "to": target_backend, "reason": prev, "at": time.time()})
            t.update(model=model_name, tier=tier, backend=target_backend,
                     handoffs=int(t.get("handoffs") or 0) + 1, model_history=history, backend_history=backends)
            if target_backend != old_backend:
                t.pop("backend_session", None)
                t.pop("backend_transport", None)
        d = tpath(root, tid)
        for f in ("report.json", "cancel"):
            (d / f).unlink(missing_ok=True)
        save(root, t)
        EventLog(root, tid, "task").emit("handoff_requested" if switched else "resume_requested",
                                         step=t.get("rounds", 0), cli_run=ctx.run_id or None,
                                         **({"from_model": old_model, "to_model": model_name,
                                             "from_backend": old_backend, "to_backend": target_backend} if switched else {}))
    schedule(root)
    t = load(root, tid)
    out = {"id": tid, "status": t["status"], "resumes": t["resumes"]}
    if switched:
        out.update({"handoff": True, "from_model": old_model, "to_model": model_name,
                    "from_backend": old_backend, "to_backend": target_backend, "handoffs": t["handoffs"]})
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
            if os.name == "posix":
                os.killpg(int(pid), signal.SIGKILL)
            else:
                os.kill(int(pid), signal.SIGTERM)
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
