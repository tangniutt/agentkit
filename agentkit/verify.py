# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-10 verify: run checks in a clean copy, ≤300-token report with suspicious flags.

"The verifier assumes the worker lies" (LooppooL, MIT) and "verify the diff in an
independent worktree" (worker-bridge, MIT) are the design inspirations; code is our own.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import checkpoint, permissions
from .core import AKError, Ctx, Result, dumps, sha256_bytes
from .fsutil import list_files
from .run import execute

REPORT_TOKENS = 300
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|testing)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|\.(test|spec)\.[jt]sx?$|(^|/)conftest\.py$|Test\.java$|Tests?\.cs$"
)
_TEST_CONFIG = re.compile(
    r"(^|/)(pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|package\.json|jest\.config\.[jt]s|vitest\.config\.[jt]s|Makefile|\.github/workflows/[^/]+|noxfile\.py|go\.mod|Cargo\.toml)$"
)
_NOT_FOUND = re.compile(r"command not found|No such file or directory|not recognized as an internal|can't open file|No module named")


def _git(root: Path, *args, check=True) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise AKError(f"git {' '.join(args[:2])} failed", r.stderr.strip()[-300:], "agentkit doctor")
    return r.stdout


def _git_changes(root: Path, base: str) -> dict:
    """path -> status (A/M/D) for working tree vs base, including untracked files."""
    out = {}
    for line in _git(root, "diff", "--name-status", "--no-renames", base, "--").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out[parts[-1]] = parts[0][0]
    for f in _git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        if f:
            out.setdefault(f, "A")
    return {k: v for k, v in out.items() if not k.startswith(".agentkit/")}


def _snapshot_changes(ctx: Ctx) -> dict:
    """Non-git: compare current files to the earliest checkpointed version of each path."""
    first: dict = {}
    for cid in checkpoint.all_ids(ctx):
        for f in checkpoint.load(ctx, cid)["files"]:
            first.setdefault(f["path"], f)
    out = {}
    for rel, f in first.items():
        p = ctx.root / rel
        if f["exists"] and not p.exists():
            out[rel] = "D"
        elif not f["exists"] and p.exists():
            out[rel] = "A"
        elif f["exists"] and p.exists() and sha256_bytes(p.read_bytes()) != f["sha"]:
            out[rel] = "M"
    return out


def _prepare_copy(ctx: Ctx, tmp: Path, base: str) -> tuple[Path, dict, str]:
    if ctx.is_git and shutil.which("git"):
        head_ok = subprocess.run(["git", "-C", str(ctx.root), "rev-parse", "--verify", base], capture_output=True).returncode == 0
        if head_ok:
            wt = tmp / "wt"
            _git(ctx.root, "worktree", "add", "--detach", "--force", str(wt), base)
            changes = _git_changes(ctx.root, base)
            for rel, st in changes.items():
                src, dst = ctx.root / rel, wt / rel
                if st == "D":
                    if dst.exists() or dst.is_symlink():
                        dst.unlink()
                elif src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            return wt, changes, "git-worktree"
    # non-git (or no commits yet): copy non-ignored files
    cp = tmp / "copy"
    for rel in list_files(ctx, ctx.root):
        src = ctx.root / rel
        dst = cp / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except OSError:
            pass
    cp.mkdir(exist_ok=True)
    return cp, _snapshot_changes(ctx), "temp-copy"


def _cleanup(ctx: Ctx, where: Path, mode: str):
    if mode == "git-worktree":
        subprocess.run(["git", "-C", str(ctx.root), "worktree", "remove", "--force", str(where)], capture_output=True)
        subprocess.run(["git", "-C", str(ctx.root), "worktree", "prune"], capture_output=True)


_INTERP = {"sh", "bash", "zsh", "dash", "python", "python3", "node", "ruby", "perl", "deno", "bun", "pwsh"}


def _check_paths(cmd: str, root: Path) -> set:
    """Repo files a check command depends on: the script it executes, and test files/dirs it names.

    A plain source path given as an argument (e.g. `mypy src/`) is the thing under check,
    not part of the check, so it is not counted (dogfood M1 found that false positive).
    """
    paths = set()
    rroot = root.resolve()
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    i = 0
    while i < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i]):
        i += 1
    toks = toks[i:]
    if not toks:
        return paths

    def rel(t):
        p = (root / t.split("::")[0])
        try:
            r = p.resolve().relative_to(rroot).as_posix()
        except ValueError:
            return None
        if p.is_dir():
            return r.rstrip("/") + "/"
        return r if p.is_file() else None

    head = toks[0].rsplit("/", 1)[-1]
    if "/" in toks[0] or (root / toks[0]).is_file():
        r = rel(toks[0])
        if r:
            paths.add(r)  # ./check.sh
    if re.match(r"python[0-9.]*$", head) or head in _INTERP:
        j = 1
        while j < len(toks) and toks[j].startswith("-") and toks[j] != "-m":
            j += 1
        if j < len(toks) and toks[j] != "-m":
            r = rel(toks[j])
            if r:
                paths.add(r)  # sh check.sh / python run_tests.py
    for t in toks[1:]:
        if t.startswith("-"):
            continue
        r = rel(t)
        if r and _TEST_PATH.search(r if not r.endswith("/") else r + "x"):
            paths.add(r)
    if head == "make":
        paths.add("Makefile")
    if head in ("npm", "yarn", "pnpm"):
        paths.add("package.json")
    return paths


def _missing_command(cmd: str, cwd: Path) -> bool:
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return False
    i = 0
    while i < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i]):
        i += 1
    if i >= len(toks):
        return False
    head = toks[i]
    if head in ("cd", "export", "source", ".", "test", "[", "true", "false", "echo", "exit", "set", "if", "for", "while", "!"):
        return False
    if "/" in head:
        return not (cwd / head).exists() and not Path(head).exists()
    return shutil.which(head) is None


def run_checks(cmds: list, where: Path, timeout: float, log_dir: Path, env=None) -> list:
    """Run each check in `where`; classify pass / fail / invalid (shared by verify and task verification)."""
    checks = []
    for c in cmds:
        if _missing_command(c, where):
            checks.append({"cmd": c, "result": "invalid", "why": "command not found"})
            continue
        r = execute(c, where, timeout, log_dir, env=env)
        err = r["stderr_log"].read_text(errors="replace")
        out = r["stdout_log"].read_text(errors="replace")
        if r["timed_out"]:
            res = "fail"
        elif r["exit_code"] == 0:
            res = "pass"
        elif r["exit_code"] in (126, 127) or (r["exit_code"] != 0 and _NOT_FOUND.search(err[-400:]) and not out.strip() and len(err) < 600):
            res = "invalid"
        else:
            res = "fail"
        item = {"cmd": c, "result": res, "exit": r["exit_code"], "s": r["duration_s"]}
        if r["timed_out"]:
            item["why"] = f"timeout {timeout:g}s"
        if r["exit_code"] == 5 and re.search(r"\bpytest\b", c):
            item["why"] = "no tests collected"
        if res != "pass":
            last = [l for l in (err.strip() or out.strip()).splitlines() if l.strip()][-1:] or [""]
            item.setdefault("why", last[0][-140:])
        item["log"] = r["stderr_log"].name.replace(".stderr.log", "")
        checks.append(item)
    return checks


def base_flags(changes: dict, cmds: list, checks: list, verdict: str, dep_roots: list) -> list:
    """Suspicious-change flags shared by verify (M-10) and task reports (M-15)."""
    flags = []
    deleted_tests = [p for p, s in changes.items() if s == "D" and _TEST_PATH.search(p)]
    modified_tests = [p for p, s in changes.items() if (s == "M" and _TEST_PATH.search(p))
                      or (s == "A" and p.rsplit("/", 1)[-1] == "conftest.py")]
    if deleted_tests:
        flags.append({"flag": "tests_deleted", "paths": deleted_tests[:3]})
    if modified_tests:
        flags.append({"flag": "tests_modified", "paths": modified_tests[:3]})
    deps = set()
    for c in cmds:
        for r in dep_roots:
            deps |= _check_paths(c, r)
    touched_check = sorted(p for p in changes if p in deps or any(d.endswith("/") and p.startswith(d) for d in deps))
    if touched_check:
        flags.append({"flag": "check_script_modified", "paths": touched_check[:3]})
    cfg = [p for p in changes if _TEST_CONFIG.search(p)]
    if cfg:
        flags.append({"flag": "test_config_modified", "paths": cfg[:3]})
    if verdict == "pass" and any(c.get("why") == "no tests collected" for c in checks):
        flags.append({"flag": "no_tests_ran"})
    return flags


def cmd_verify(ctx: Ctx, a) -> Result:
    if not a.check:
        raise AKError("no --check given", "verify needs at least one check command", 'agentkit verify --check "pytest -q"')
    for c in a.check:
        v = permissions.check(c, ctx.config)
        if not v.allowed:
            raise AKError(f"check refused by permission rule '{v.rule}'", v.why or "", "use a check command that is allowed")
    timeout = a.timeout or float(ctx.config.get("run", {}).get("timeout", 120))
    t0 = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix="agentkit-verify-"))
    where = tmp
    mode = "?"
    try:
        where, changes, mode = _prepare_copy(ctx, tmp, a.base)
        checks = run_checks(a.check, where, timeout, ctx.state("logs"))
    finally:
        _cleanup(ctx, where, mode)
        shutil.rmtree(tmp, ignore_errors=True)

    if any(c["result"] == "invalid" for c in checks):
        verdict = "invalid"
    elif all(c["result"] == "pass" for c in checks):
        verdict = "pass"
    else:
        verdict = "fail"

    flags = base_flags(changes, a.check, checks, verdict, [ctx.root])

    report = {
        "verdict": verdict,
        "checks": checks,
        "changed": [f"{s} {p}" for p, s in sorted(changes.items())],
        "suspicious": flags,
        "cost": {"s": round(time.monotonic() - t0, 1), "tokens": 0, "usd": 0},
        "env": mode,
    }
    if verdict == "invalid":
        report["hint"] = "a check command is missing or broken; fix the check, this is not a code failure"
    code = {"fail": "AK-VERIFY-FAIL", "invalid": "AK-VERIFY-INVALID"}.get(verdict) or ("AK-VERIFY-SUSPICIOUS" if flags else None)
    if code:
        report["code"] = code
        report["run"] = ctx.run_id
    if ctx.run_id:
        from .events import EventLog

        ev = EventLog(ctx.root, ctx.run_id, "verifier")
        for i, c in enumerate(checks):
            ev.emit("check", step=i + 1, duration=c.get("s"), result=c["result"],
                    error_code={"fail": "AK-VERIFY-FAIL", "invalid": "AK-VERIFY-INVALID"}.get(c["result"]),
                    cmd=c["cmd"][:120], log=c.get("log"), why=c.get("why"))
    # full report on disk, compact one on stdout
    vdir = ctx.state("verify")
    rid = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    full_p = vdir / f"verify-{rid}.json"
    full_p.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    if verdict == "pass" and not flags:
        _remember_checks(ctx, a.check)  # never learn from a run that looks like cheating

    compact, trunc = _compact(report, REPORT_TOKENS * 4 - 120 - len(ctx.rel(full_p)))
    nxt = ctx.next_cmd("read", ctx.rel(full_p)) if trunc else None
    return Result(compact, trunc, nxt, budget_chars=REPORT_TOKENS * 4)


def _compact(report: dict, room: int) -> tuple[dict, bool]:
    rep = json.loads(json.dumps(report))
    trunc = False
    steps = [
        lambda r: r["checks"] and [c.pop("log", None) for c in r["checks"]],
        lambda r: r.update(changed=r["changed"][:8] + ([f"+{len(r['changed']) - 8} more"] if len(r["changed"]) > 8 else [])),
        lambda r: [c.update(why=str(c.get("why", ""))[:60]) for c in r["checks"] if c.get("why")],
        lambda r: r.update(changed=r["changed"][:3] + ([f"+{len(report['changed']) - 3} more"] if len(report["changed"]) > 3 else [])),
        lambda r: [c.update(cmd=c["cmd"][:40]) for c in r["checks"] if "cmd" in c],
        lambda r: r.update(checks=r["checks"][:4]),
        lambda r: r.pop("hint", None),
        lambda r: r.update(changed=[f"{len(report['changed'])} files"]),
        lambda r: r.update(checks=r["checks"][:1]),
    ]
    for step in steps:
        if len(dumps(rep)) <= room:
            break
        step(rep)
        trunc = True
    if len(dumps(rep)) > room:
        rep = {"verdict": rep["verdict"], "suspicious": [f["flag"] for f in rep["suspicious"]],
               **{k: rep[k] for k in ("code", "run", "error") if rep.get(k)}}
        trunc = True
    return rep, trunc


def _remember_checks(ctx: Ctx, checks: list):
    """Deterministic auto-memory: remember check commands that passed (M-21)."""
    try:
        from . import memory

        known: list = []
        try:
            body = memory.get(ctx, "verified-checks")["body"]
            known = re.findall(r"^- `(.*)`$", body, flags=re.M)
        except Exception:
            pass
        merged = known + [c for c in checks if c not in known]
        memory.upsert_auto(ctx, "verified-checks", "Check commands that passed verify", "project-info",
                           "\n".join(f"- `{c}`" for c in merged[-8:]))
    except Exception:
        pass
