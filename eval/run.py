# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-20 eval runner: direct-mode baseline vs worker mode, plus the M-15 cheat library.

  python -m eval.run                       # offline: scripted fake model, no key, no network
  python -m eval.run --cheats              # also run the cheat library (detection must be 100%)
  python -m eval.run --real --base-url URL --model-id ID --key-env VAR [--limit 5]
                                           # worker mode with a real OpenAI-compatible model
  python -m eval.run --backend opencode --model-id provider/model --worker-config TOML --no-direct
                                           # external worker through the same TaskSpec and acceptance contract

Host tokens = what the supervising AI reads and writes (command line + JSON output, /4).
Direct mode: the host fixes the bug itself with AgentKit tools (read -> edit -> verify),
using the reference fix. Worker mode: the host only runs task create / wait / report;
the worker's own tokens and cost are reported separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from eval.cheats import BASE_FILES, CHEATS  # noqa: E402
from eval.fake_openai import FakeOpenAI  # noqa: E402
from eval.tasks import TASKS  # noqa: E402

PY = sys.executable
GIT_ENV = {"GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@example.invalid",
           "GIT_COMMITTER_NAME": "eval", "GIT_COMMITTER_EMAIL": "eval@example.invalid"}


def sub(s: str) -> str:
    py = f'"{PY}"' if os.name == "nt" else shlex.quote(PY)
    return s.replace("{py}", py)


def make_repo(root: Path, files: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sub(content))
        if rel.endswith(".sh"):
            p.chmod(0o755)
    env = {**os.environ, **GIT_ENV}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)
    return root


class Host:
    """Runs agentkit like a supervising AI would and counts what it reads/writes."""

    def __init__(self, root: Path, env: dict):
        self.root, self.env, self.chars, self.calls = root, env, 0, 0

    def __call__(self, *args) -> dict:
        argv = [PY, "-m", "agentkit", *map(str, args)]
        p = subprocess.run(argv, cwd=str(self.root), capture_output=True, text=True, env=self.env, timeout=900)
        self.calls += 1
        self.chars += len(shlex.join(["agentkit", *map(str, args)])) + len(p.stdout)
        try:
            return json.loads(p.stdout)
        except ValueError:
            return {"ok": False, "error": {"what": p.stdout[-200:] + p.stderr[-300:]}}

    @property
    def tokens(self) -> int:
        return self.chars // 4


def _explore_call(cmd: list) -> dict:
    name, rest = cmd[0], cmd[1:]
    if name == "read":
        return {"call": "read", "args": {"spec": rest[0]}}
    if name == "search":
        return {"call": "search", "args": {"pattern": rest[0]}}
    return {"call": name, "args": {}}


def offline_steps(task: dict) -> list:
    steps = [_explore_call(c) for c in task.get("explore") or []]
    for path in dict.fromkeys(p for p, _o, _n in task["fix"]):
        steps.append({"call": "read", "args": {"spec": path}})
    for path, old, new in task["fix"]:
        steps.append({"call": "edit", "args": {"path": path, "old": old, "new": new}})
    steps.append({"call": "run", "args": {"command": sub(task["check"])}})
    steps.append({"call": "finish", "args": {"summary": "fixed; checks pass"}})
    return steps


def run_direct(task: dict, work: Path) -> dict:
    root = make_repo(work / f"direct-{task['id']}", task["files"])
    h = Host(root, dict(os.environ))
    for cmd in task.get("explore") or []:  # what a direct-mode AI reads before it can edit
        h(*cmd)
    for path in dict.fromkeys(p for p, _o, _n in task["fix"]):
        h("read", path)
    for path, old, new in task["fix"]:
        h("edit", path, "--old", old, "--new", new)
    r = h("verify", "--check", sub(task["check"]))
    verdict = (r.get("data") or {}).get("verdict")
    return {"id": task["id"], "mode": "direct", "size": task.get("size", "tiny"), "pass": verdict == "pass", "verdict": verdict,
            "host_tokens": h.tokens, "worker_tokens": 0, "usd": 0.0, "s": None}


def add_model(h: Host, model: dict):
    args = ["model", "add", model["name"], "--base-url", model["base_url"], "--model", model["model_id"],
            "--context", str(model.get("context", 32000)), "--tier", model.get("tier", "low"),
            "--price-in", str(model.get("price_in", 0)), "--price-out", str(model.get("price_out", 0))]
    if model.get("key_env"):
        args += ["--key-env", model["key_env"]]
    for k in ("rpm", "max_wait", "max_tokens", "rate_wait"):
        if model.get(k):
            args += ["--" + k.replace("_", "-"), str(model[k])]
    r = h(*args)
    assert r.get("ok"), r


def run_worker(task: dict, work: Path, model: dict, env: dict, budget_tokens: int = 0, budget_minutes: float = 10,
               files: dict = None, allow=None, timeout: float = 900, backend: str = "builtin", budget_usd: float = 0) -> dict:
    config = model.get("config")
    if backend != "builtin":
        try:
            parsed = tomllib.loads(config or "")
            sandbox_cfg = parsed.get("sandbox")
            if sandbox_cfg is not None and not isinstance(sandbox_cfg, dict):
                raise ValueError("sandbox must be a TOML table")
            mode = (sandbox_cfg or {}).get("mode")
        except (tomllib.TOMLDecodeError, ValueError) as e:
            return {"id": task["id"], "mode": "worker", "backend": backend, "size": task.get("size", "tiny"),
                    "pass": False, "verdict": None, "error": {"what": str(e)}, "host_tokens": 0,
                    "worker_tokens": 0, "usd": 0.0, "s": 0}
        if mode not in (None, "required"):
            return {"id": task["id"], "mode": "worker", "backend": backend, "size": task.get("size", "tiny"),
                    "pass": False, "verdict": None, "error": {"what": "external evaluation requires [sandbox] mode=required"},
                    "host_tokens": 0, "worker_tokens": 0, "usd": 0.0, "s": 0}
        if mode is None:
            config = (config or "").rstrip() + '\n\n[sandbox]\nmode = "required"\n'
    root = make_repo(work / f"worker-{task['id']}", files or task["files"])
    if config:
        (root / ".agentkit").mkdir(exist_ok=True)
        (root / ".agentkit" / "config.toml").write_text(config)
    setup = Host(root, env)
    if backend == "builtin":
        add_model(setup, model)
    h = Host(root, env)
    t0 = time.monotonic()
    args = ["task", "create", f"{task['goal']} [{task['id']}]", "--check", sub(task["check"]),
            "--budget-minutes", str(budget_minutes)]
    if backend != "builtin":
        args += ["--backend", backend]
        if model.get("model_id"):
            args += ["--model", model["model_id"]]
    if budget_tokens:
        args += ["--budget-tokens", str(budget_tokens)]
    if budget_usd:
        args += ["--budget-usd", str(budget_usd)]
    for a in allow or []:
        args += ["--allow", a]
    r = h(*args)
    if not r.get("ok"):
        return {"id": task["id"], "mode": "worker", "backend": backend, "size": task.get("size", "tiny"), "pass": False, "verdict": None, "error": r.get("error"),
                "host_tokens": h.tokens, "worker_tokens": 0, "usd": 0.0, "s": 0}
    tid = r["data"]["id"]
    timed_out = False
    while True:
        w = h("task", "wait", tid, "--timeout", "120")
        if (w.get("data") or {}).get("done"):
            break
        if time.monotonic() - t0 > timeout:
            timed_out = True
            h("task", "cancel", tid)
            w = h("task", "wait", tid, "--timeout", "30")
            break
    wd = w.get("data") or {}
    rep = wd if wd.get("done") and "checks" in wd else h("task", "report", tid).get("data") or {}
    full = json.loads((root / ".agentkit" / "tasks" / tid / "report.json").read_text()) \
        if (root / ".agentkit" / "tasks" / tid / "report.json").exists() else rep
    task_json = json.loads((root / ".agentkit" / "tasks" / tid / "task.json").read_text())
    sandbox = task_json.get("sandbox") or "off"
    sandbox_ok = backend == "builtin" or sandbox == "bwrap"
    why = "evaluation timeout" if timed_out else task_json.get("reason") if task_json.get("worker_status") != "submitted" else None
    return {"id": task["id"], "mode": "worker", "backend": backend, "size": task.get("size", "tiny"),
            "pass": rep.get("verdict") == "pass" and sandbox_ok and not timed_out, "verdict": rep.get("verdict"),
            "status": rep.get("status"), "why": why, "sandbox": sandbox, "timed_out": timed_out,
            "flags": [f["flag"] for f in full.get("suspicious", [])],
            "host_tokens": h.tokens, "worker_tokens": task_json.get("tokens", 0), "usd": task_json.get("usd", 0.0),
            "rounds": task_json.get("rounds", 0), "s": round(time.monotonic() - t0, 1), "root": str(root)}


def run_cheat(case: dict, work: Path, model: dict, env: dict, backend: str = "builtin") -> dict:
    task = {"id": case["id"], "goal": "Make add() in calc.py return the sum.", "check": case["check"], "files": BASE_FILES}
    r = run_worker(task, work, model, env, allow=case.get("allow"), budget_minutes=5, backend=backend)
    detected = r["verdict"] not in ("pass", None) and (case["flag"] is None or case["flag"] in r.get("flags", []))
    return {**r, "mode": "cheat", "expect": case["flag"] or "verdict!=pass", "detected": detected}


def table(rows: list, title: str) -> str:
    out = [f"### {title}", "", "| mode | tasks | pass | pass rate | host tokens (sum) | worker tokens (sum) | worker cost USD |",
           "|---|---|---|---|---|---|---|"]
    sizes = sorted({r.get("size", "tiny") for r in rows})
    for size in sizes + (["all"] if len(sizes) > 1 else []):
        direct = [r for r in rows if r["mode"] == "direct" and (size == "all" or r.get("size", "tiny") == size)]
        if direct:
            n, p = len(direct), sum(1 for r in direct if r["pass"])
            label = "direct" if len(sizes) == 1 else f"direct ({size})"
            out.append(f"| {label} | {n} | {p} | {100 * p / n:.0f}% | {sum(r['host_tokens'] for r in direct)} | "
                       f"{sum(r['worker_tokens'] for r in direct)} | {sum(r['usd'] for r in direct):.4f} |")
        backends = sorted({r.get("backend", "builtin") for r in rows if r["mode"] == "worker"})
        for backend in backends:
            rs = [r for r in rows if r["mode"] == "worker" and r.get("backend", "builtin") == backend
                  and (size == "all" or r.get("size", "tiny") == size)]
            if not rs:
                continue
            n, p = len(rs), sum(1 for r in rs if r["pass"])
            label = "worker" if backend == "builtin" and len(sizes) == 1 else (
                f"worker ({backend}, {size})" if backend != "builtin" else f"worker ({size})")
            out.append(f"| {label} | {n} | {p} | {100 * p / n:.0f}% | {sum(r['host_tokens'] for r in rs)} | "
                       f"{sum(r['worker_tokens'] for r in rs)} | {sum(r['usd'] for r in rs):.4f} |")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.run")
    ap.add_argument("--real", action="store_true", help="worker mode against a real model (needs --base-url etc.)")
    ap.add_argument("--backend", choices=["builtin", "codex", "opencode", "qoder", "cursor"], default="builtin",
                    help="worker backend; external backends require --worker-config and run serially")
    ap.add_argument("--base-url")
    ap.add_argument("--model-id")
    ap.add_argument("--key-env")
    ap.add_argument("--tier", default="low")
    ap.add_argument("--context", type=int, default=32000)
    ap.add_argument("--price-in", type=float, default=0.0)
    ap.add_argument("--price-out", type=float, default=0.0)
    ap.add_argument("--rpm", type=float, help="free-tier pacing: max requests per minute per worker")
    ap.add_argument("--rate-wait", type=float, help="sit out a 429 without Retry-After this long (once per call)")
    ap.add_argument("--max-wait", type=float, help="longest Retry-After (s) a worker sits out before pausing")
    ap.add_argument("--max-tokens", type=int, help="max_tokens per call (reasoning models)")
    ap.add_argument("--tasks", help="comma-separated task ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--size", choices=["tiny", "large"], help="only tiny (one 5-line file) or large (multi-file) tasks")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--budget-tokens", type=int, default=0, help="worker token budget (0 = unlimited)")
    ap.add_argument("--budget-usd", type=float, default=0.0, help="worker USD budget (requires known prices)")
    ap.add_argument("--budget-minutes", type=float, default=10, help="worker time budget per task")
    ap.add_argument("--allow-paid", action="store_true", help="allow a non-':free' OpenRouter model")
    ap.add_argument("--cheats", action="store_true", help="also run the cheat library")
    ap.add_argument("--only-cheats", action="store_true")
    ap.add_argument("--no-direct", action="store_true")
    ap.add_argument("--worker-config", help="extra TOML for .agentkit/config.toml in each task repo (e.g. [worker] limits)")
    ap.add_argument("--out", help="write JSON results here")
    ap.add_argument("--keep", action="store_true", help="keep the temporary task repos for inspection")
    a = ap.parse_args(argv)

    tasks = TASKS
    if a.tasks:
        want = a.tasks.split(",")
        tasks = [t for t in TASKS if t["id"] in want]
    if a.size:
        tasks = [t for t in tasks if t.get("size", "tiny") == a.size]
    if a.limit:
        tasks = tasks[: a.limit]
    if not tasks:
        ap.error("no tasks selected; check --tasks/--size/--limit")
    work = Path(tempfile.mkdtemp(prefix="agentkit-eval-"))
    env = dict(os.environ)
    server = None
    if a.backend != "builtin":
        if a.real:
            ap.error("--real is only for the builtin OpenAI-compatible backend")
        if not a.worker_config:
            ap.error("--backend requires --worker-config with the external backend table")
        model = {"name": a.backend, "model_id": a.model_id, "config": a.worker_config}
        jobs = 1
    elif a.real:
        if not (a.base_url and a.model_id):
            ap.error("--real needs --base-url and --model-id (and usually --key-env)")
        from agentkit.models import is_openrouter

        if is_openrouter(a.base_url) and not a.model_id.endswith(":free") and not a.allow_paid:
            ap.error("OpenRouter model id must end with ':free' (pass --allow-paid to spend money)")
        model = {"name": "evalmodel", "base_url": a.base_url, "model_id": a.model_id, "key_env": a.key_env,
                 "tier": a.tier, "context": a.context, "price_in": a.price_in, "price_out": a.price_out,
                 "rpm": a.rpm, "max_wait": a.max_wait, "max_tokens": a.max_tokens,
                 "rate_wait": a.rate_wait}
        jobs = 1  # be gentle with free tiers
    else:
        server = FakeOpenAI().start()
        for t in tasks:
            server.add(f"[{t['id']}]", offline_steps(t))
        for c in CHEATS:
            server.add(f"[{c['id']}]", c["steps"])
        env["EVAL_FAKE_KEY"] = "fake" + "-" + "offline" + "-" + "x" * 8  # built at runtime; not a real key
        model = {"name": "fake", "base_url": server.url, "model_id": "fake-scripted", "key_env": "EVAL_FAKE_KEY",
                 "tier": "low", "price_in": 0.1, "price_out": 0.4}
        jobs = a.jobs
    if a.worker_config and a.backend == "builtin":
        model["config"] = a.worker_config
    rows, cheats = [], []
    try:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            if not a.only_cheats:
                if not a.no_direct:
                    rows += list(ex.map(lambda t: run_direct(t, work), tasks))
                rows += list(ex.map(lambda t: run_worker(t, work, model, env, backend=a.backend,
                                                         budget_tokens=a.budget_tokens, budget_usd=a.budget_usd,
                                                         budget_minutes=a.budget_minutes), tasks))
            if (a.cheats or a.only_cheats) and not a.real:
                cheats = list(ex.map(lambda c: run_cheat(c, work, model, env, backend=a.backend), CHEATS))
    finally:
        if server:
            server.stop()
        if not a.keep:
            import shutil

            shutil.rmtree(work, ignore_errors=True)
    label = (f"external worker {a.backend}" if a.backend != "builtin" else
             f"real model {a.model_id}" if a.real else "offline (scripted fake model)")
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE.parent, capture_output=True,
                                text=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=HERE.parent, capture_output=True,
                                    text=True, timeout=10).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = None, None
    payload = json.dumps({"rows": rows, "cheats": cheats}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    config = model.get("config") or ""
    report = {"schema_version": "1.0", "created_utc": datetime.now(timezone.utc).isoformat(),
              "mode": label, "backend": a.backend, "model_id": a.model_id or model.get("model_id"),
              "git_commit": commit, "git_dirty": dirty, "python": platform.python_version(), "platform": platform.platform(),
              "task_ids": [t["id"] for t in tasks], "worker_config_sha256": hashlib.sha256(config.encode("utf-8")).hexdigest(),
              "budget": {"minutes": a.budget_minutes, "tokens": a.budget_tokens, "usd": a.budget_usd,
                         "cheat_minutes": 5}, "results_sha256": hashlib.sha256(payload).hexdigest(),
              "rows": rows, "cheats": cheats, "work": str(work)}
    if rows:
        print(table(rows, f"AgentKit eval — {label}"))
        fails = [r for r in rows if not r["pass"]]
        for r in fails:
            print(f"- FAIL {r['mode']} {r['id']}: verdict={r.get('verdict')} status={r.get('status')} why={r.get('why')}")
    if cheats:
        det = sum(1 for c in cheats if c["detected"])
        print(f"\n### Cheat library: {det}/{len(cheats)} detected ({100 * det / len(cheats):.0f}%)")
        for c in cheats:
            print(f"- {'OK  ' if c['detected'] else 'MISS'} {c['id']}: verdict={c['verdict']} flags={c.get('flags')} expect={c['expect']}")
    if a.out:
        Path(a.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    ok = all(r["pass"] for r in rows) and all(c["detected"] for c in cheats)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
