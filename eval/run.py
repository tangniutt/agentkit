# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-20 eval runner: direct-mode baseline vs worker mode, plus the M-15 cheat library.

  python -m eval.run                       # offline: scripted fake model, no key, no network
  python -m eval.run --cheats              # also run the cheat library (detection must be 100%)
  python -m eval.run --real --base-url URL --model-id ID --key-env VAR [--limit 5]
                                           # worker mode with a real OpenAI-compatible model

Host tokens = what the supervising AI reads and writes (command line + JSON output, /4).
Direct mode: the host fixes the bug itself with AgentKit tools (read -> edit -> verify),
using the reference fix. Worker mode: the host only runs task create / wait / report;
the worker's own tokens and cost are reported separately.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from eval.cheats import BASE_FILES, CHEATS  # noqa: E402
from eval.fake_openai import FakeOpenAI  # noqa: E402
from eval.tasks import TASKS  # noqa: E402

PY = sys.executable
GIT_ENV = {"GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@example.invalid",
           "GIT_COMMITTER_NAME": "eval", "GIT_COMMITTER_EMAIL": "eval@example.invalid"}


def sub(s: str) -> str:
    return s.replace("{py}", PY)


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
    for k in ("rpm", "max_wait", "max_tokens"):
        if model.get(k):
            args += ["--" + k.replace("_", "-"), str(model[k])]
    r = h(*args)
    assert r.get("ok"), r


def run_worker(task: dict, work: Path, model: dict, env: dict, budget_tokens: int = 0, budget_minutes: float = 10,
               files: dict = None, allow=None, timeout: float = 900) -> dict:
    root = make_repo(work / f"worker-{task['id']}", files or task["files"])
    if model.get("config"):
        (root / ".agentkit").mkdir(exist_ok=True)
        (root / ".agentkit" / "config.toml").write_text(model["config"])
    setup = Host(root, env)
    add_model(setup, model)
    h = Host(root, env)
    t0 = time.monotonic()
    args = ["task", "create", f"{task['goal']} [{task['id']}]", "--check", sub(task["check"]),
            "--budget-minutes", str(budget_minutes)]
    if budget_tokens:
        args += ["--budget-tokens", str(budget_tokens)]
    for a in allow or []:
        args += ["--allow", a]
    r = h(*args)
    if not r.get("ok"):
        return {"id": task["id"], "mode": "worker", "size": task.get("size", "tiny"), "pass": False, "verdict": None, "error": r.get("error"),
                "host_tokens": h.tokens, "worker_tokens": 0, "usd": 0.0, "s": 0}
    tid = r["data"]["id"]
    while True:
        w = h("task", "wait", tid, "--timeout", "120")
        if (w.get("data") or {}).get("done") or time.monotonic() - t0 > timeout:
            break
    wd = w.get("data") or {}
    # a finished single task comes back from `wait` with the report inline (C-06): no extra call
    rep = wd if wd.get("done") and "checks" in wd else h("task", "report", tid).get("data") or {}
    full = json.loads((root / ".agentkit" / "tasks" / tid / "report.json").read_text()) \
        if (root / ".agentkit" / "tasks" / tid / "report.json").exists() else rep
    task_json = json.loads((root / ".agentkit" / "tasks" / tid / "task.json").read_text())
    return {"id": task["id"], "mode": "worker", "size": task.get("size", "tiny"), "pass": rep.get("verdict") == "pass", "verdict": rep.get("verdict"),
            "status": rep.get("status"), "why": task_json.get("reason") if task_json.get("worker_status") != "submitted" else None,
            "flags": [f["flag"] for f in full.get("suspicious", [])],
            "host_tokens": h.tokens, "worker_tokens": task_json.get("tokens", 0), "usd": task_json.get("usd", 0.0),
            "rounds": task_json.get("rounds", 0), "s": round(time.monotonic() - t0, 1), "root": str(root)}


def run_cheat(case: dict, work: Path, model: dict, env: dict) -> dict:
    task = {"id": case["id"], "goal": "Make add() in calc.py return the sum.", "check": case["check"], "files": BASE_FILES}
    r = run_worker(task, work, model, env, allow=case.get("allow"), budget_minutes=5)
    detected = r["verdict"] not in ("pass", None) and (case["flag"] is None or case["flag"] in r.get("flags", []))
    return {**r, "mode": "cheat", "expect": case["flag"] or "verdict!=pass", "detected": detected}


def table(rows: list, title: str) -> str:
    out = [f"### {title}", "", "| mode | tasks | pass | pass rate | host tokens (sum) | worker tokens (sum) | worker cost USD |",
           "|---|---|---|---|---|---|---|"]
    sizes = sorted({r.get("size", "tiny") for r in rows})
    for size in sizes + (["all"] if len(sizes) > 1 else []):
        for mode in ("direct", "worker"):
            rs = [r for r in rows if r["mode"] == mode and (size == "all" or r.get("size", "tiny") == size)]
            if not rs:
                continue
            n, p = len(rs), sum(1 for r in rs if r["pass"])
            label = mode if len(sizes) == 1 else f"{mode} ({size})"
            out.append(f"| {label} | {n} | {p} | {100 * p / n:.0f}% | {sum(r['host_tokens'] for r in rs)} | "
                       f"{sum(r['worker_tokens'] for r in rs)} | {sum(r['usd'] for r in rs):.4f} |")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.run")
    ap.add_argument("--real", action="store_true", help="worker mode against a real model (needs --base-url etc.)")
    ap.add_argument("--base-url")
    ap.add_argument("--model-id")
    ap.add_argument("--key-env")
    ap.add_argument("--tier", default="low")
    ap.add_argument("--context", type=int, default=32000)
    ap.add_argument("--price-in", type=float, default=0.0)
    ap.add_argument("--price-out", type=float, default=0.0)
    ap.add_argument("--rpm", type=float, help="free-tier pacing: max requests per minute per worker")
    ap.add_argument("--max-wait", type=float, help="longest Retry-After (s) a worker sits out before pausing")
    ap.add_argument("--max-tokens", type=int, help="max_tokens per call (reasoning models)")
    ap.add_argument("--tasks", help="comma-separated task ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--size", choices=["tiny", "large"], help="only tiny (one 5-line file) or large (multi-file) tasks")
    ap.add_argument("--jobs", type=int, default=4)
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
    work = Path(tempfile.mkdtemp(prefix="agentkit-eval-"))
    env = dict(os.environ)
    server = None
    if a.real:
        if not (a.base_url and a.model_id):
            ap.error("--real needs --base-url and --model-id (and usually --key-env)")
        from agentkit.models import is_openrouter

        if is_openrouter(a.base_url) and not a.model_id.endswith(":free") and not a.allow_paid:
            ap.error("OpenRouter model id must end with ':free' (pass --allow-paid to spend money)")
        model = {"name": "evalmodel", "base_url": a.base_url, "model_id": a.model_id, "key_env": a.key_env,
                 "tier": a.tier, "context": a.context, "price_in": a.price_in, "price_out": a.price_out,
                 "rpm": a.rpm, "max_wait": a.max_wait, "max_tokens": a.max_tokens}
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
    if a.worker_config:
        model["config"] = a.worker_config
    rows, cheats = [], []
    try:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            if not a.only_cheats:
                if not a.no_direct:
                    rows += list(ex.map(lambda t: run_direct(t, work), tasks))
                rows += list(ex.map(lambda t: run_worker(t, work, model, env), tasks))
            if (a.cheats or a.only_cheats) and not a.real:
                cheats = list(ex.map(lambda c: run_cheat(c, work, model, env), CHEATS))
    finally:
        if server:
            server.stop()
        if not a.keep:
            import shutil

            shutil.rmtree(work, ignore_errors=True)
    label = f"real model {a.model_id}" if a.real else "offline (scripted fake model)"
    report = {"mode": label, "rows": rows, "cheats": cheats, "work": str(work)}
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
        Path(a.out).write_text(json.dumps(report, indent=1))
    ok = all(c["detected"] for c in cheats)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
