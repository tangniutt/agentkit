# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-10 verify: run checks in a clean copy, ≤300-token report with suspicious flags.

"The verifier assumes the worker lies" (LooppooL, MIT) and "verify the diff in an
independent worktree" (worker-bridge, MIT) are the design inspirations; code is our own.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import checkpoint, permissions
from .core import AKError, Ctx, Result, atomic_write, dumps, sha256_bytes
from .fsutil import list_files
from .protocol import EvidenceReceipt, canonical_json, sha256
from .run import execute

REPORT_TOKENS = 300
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|testing)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|\.(test|spec)\.[jt]sx?$|(^|/)conftest\.py$|Test\.java$|Tests?\.cs$"
)
_TEST_CONFIG = re.compile(
    r"(^|/)(pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|package\.json|jest\.config\.[jt]s|vitest\.config\.[jt]s|Makefile|\.github/workflows/[^/]+|noxfile\.py|go\.mod|Cargo\.toml)$"
)
_NOT_FOUND = re.compile(r"command not found|No such file or directory|not recognized as an internal|can't open file|No module named")
_JUNK = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache)/|(^|/)\.pyc$")


def _git(root: Path, *args, check=True, raw=False) -> str | bytes:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=not raw, timeout=120)
    if check and r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else r.stderr
        raise AKError(f"git {' '.join(args[:2])} failed", err.strip()[-300:], "agentkit doctor")
    return r.stdout


def _git_changes(root: Path, base: str) -> dict:
    """path -> status for working tree vs base, including untracked files.

    NUL-delimited bytes avoid Git's C-style path quoting. Renames keep the
    historical report/copy behavior: D for the old path and A for the new one.
    """
    out = {}
    fields = _git(root, "diff", "--name-status", "-z", "--find-renames", base, "--", raw=True).split(b"\0")
    i = 0
    while i < len(fields):
        status = fields[i]
        i += 1
        if not status:
            continue
        code = status[:1]
        count = 2 if code in (b"R", b"C") else 1
        paths = fields[i:i + count]
        i += count
        if len(paths) != count or any(not p for p in paths):
            raise AKError("git diff returned malformed -z output", "incomplete name-status record", "agentkit doctor")
        names = [os.fsdecode(p) for p in paths]
        if code == b"R":
            out[names[0]] = "D"
        out[names[-1]] = "A" if code in (b"R", b"C") else code.decode("ascii")
    for path in _git(root, "ls-files", "--others", "--exclude-standard", "-z", raw=True).split(b"\0"):
        if path:
            out.setdefault(os.fsdecode(path), "A")
    return {k: v for k, v in out.items() if not k.startswith(".agentkit/") and not _JUNK.search(k)}


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


def _inside_worktree(root: Path, rel: str) -> tuple[Path, Path]:
    """Return a path and its resolved target, refusing symlink/junction escapes."""
    path = root / rel
    try:
        real = path.resolve()
        real.relative_to(root.resolve())
    except ValueError as e:
        raise AKError("path outside worktree", f"{rel} resolves outside {root}",
                      "remove the escaping symlink/junction and retry") from e
    except (OSError, RuntimeError) as e:
        raise AKError("path outside worktree", f"cannot safely resolve {rel}: {e}",
                      "remove the escaping symlink/junction and retry") from e
    return path, real


def _prepare_copy(ctx: Ctx, tmp: Path, base: str) -> tuple[Path, dict, str]:
    if ctx.is_git and shutil.which("git"):
        head_ok = subprocess.run(["git", "-C", str(ctx.root), "rev-parse", "--verify", base], capture_output=True).returncode == 0
        if head_ok:
            wt = tmp / "wt"
            _git(ctx.root, "worktree", "add", "--detach", "--force", str(wt), base)
            try:
                changes = _git_changes(ctx.root, base)
                for rel, st in changes.items():
                    _, src_real = _inside_worktree(ctx.root, rel)
                    dst, _ = _inside_worktree(wt, rel)
                    if st == "D":
                        if dst.exists() or dst.is_symlink():
                            dst.unlink()
                    elif src_real.is_file():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        if dst.is_symlink():
                            dst.unlink()
                        shutil.copy2(src_real, dst)
            except Exception:
                # cmd_verify cannot clean up a worktree that _prepare_copy never returns.
                _cleanup(ctx, wt, "git-worktree")
                raise
            return wt, changes, "git-worktree"
    # non-git (or no commits yet): copy non-ignored files
    cp = tmp / "copy"
    for rel in list_files(ctx, ctx.root):
        _, src_real = _inside_worktree(ctx.root, rel)
        dst, _ = _inside_worktree(cp, rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dst.is_symlink():
                dst.unlink()
            shutil.copy2(src_real, dst)
        except OSError:
            pass
    cp.mkdir(exist_ok=True)
    return cp, _snapshot_changes(ctx), "temp-copy"


def _cleanup(ctx: Ctx, where: Path, mode: str):
    if mode == "git-worktree":
        subprocess.run(["git", "-C", str(ctx.root), "worktree", "remove", "--force", str(where)], capture_output=True)
        subprocess.run(["git", "-C", str(ctx.root), "worktree", "prune"], capture_output=True)


_INTERP = {"sh", "bash", "zsh", "dash", "python", "python3", "node", "ruby", "perl", "deno", "bun", "pwsh"}


def _shell_tokens(cmd: str) -> list:
    try:
        toks = shlex.split(cmd, posix=os.name != "nt")
    except ValueError:
        toks = cmd.split()
    if os.name == "nt":
        toks = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"') else t for t in toks]
    return toks


def _check_paths(cmd: str, root: Path) -> set:
    """Repo files a check command depends on: the script it executes, and test files/dirs it names.

    A plain source path given as an argument (e.g. `mypy src/`) is the thing under check,
    not part of the check, so it is not counted (dogfood M1 found that false positive).
    """
    paths = set()
    rroot = root.resolve()
    toks = _shell_tokens(cmd)
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
    toks = _shell_tokens(cmd)
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
            from .compress import best_why  # C-08: the summary / diagnostic line, not trailing noise

            item.setdefault("why", best_why(err if err.strip() else out))
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


def _prepare_base(ctx: Ctx, tmp: Path, base: str) -> Path:
    wt = tmp / "base"
    _git(ctx.root, "worktree", "add", "--detach", "--force", str(wt), base)
    return wt


def _write_direct_receipt(ctx: Ctx, base_commit: str, changes: dict, checks: list, baseline_checks: list,
                          verdict: str, flags: list, cost: dict, mode: str, run_id: str) -> dict:
    from . import __version__

    diff = hashlib.sha256()
    for rel, status in sorted(changes.items()):
        diff.update(rel.encode("utf-8", "surrogateescape"))
        diff.update(b"\0" + status.encode() + b"\0")
        path = ctx.root / rel
        if status != "D" and path.is_file():
            diff.update(path.read_bytes())
        diff.update(b"\0")
    lockfiles = {}
    for name in ("pyproject.toml", "requirements.txt", "package-lock.json", "pnpm-lock.yaml", "uv.lock",
                 "poetry.lock", "Cargo.lock", "go.sum"):
        path = ctx.root / name
        if path.is_file():
            lockfiles[name] = sha256_bytes(path.read_bytes())
    acceptance = {"checks": [c["cmd"] for c in checks], "baseline_must_fail": True, "base": base_commit}
    receipt_checks = ([{"phase": "baseline", **item} for item in baseline_checks] +
                      [{"phase": "acceptance", **item} for item in checks])
    receipt_id = "er_" + sha256({"subject": run_id, "base": base_commit, "diff": diff.hexdigest(),
                                 "acceptance": acceptance, "checks": receipt_checks, "verdict": verdict})[:24]
    receipt = EvidenceReceipt(
        receipt_id=receipt_id, subject={"kind": "verify", "id": run_id}, base_commit=base_commit,
        diff_sha256=diff.hexdigest(), acceptance_sha256=sha256(acceptance),
        environment_sha256=sha256({"python": sys.version, "platform": platform.platform(),
                                   "mode": mode, "lockfiles": lockfiles}),
        checks=receipt_checks, verdict=verdict, verifier_version=__version__,
        worker={"backend": "direct", "model": None, "sandbox": mode}, cost=cost, suspicious=flags,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    ).validate()
    path = ctx.state("evidence") / f"{receipt_id}.json"
    data = (canonical_json(receipt.to_dict()) + "\n").encode("utf-8")
    atomic_write(path, data, mode=0o600)
    return {"id": receipt_id, "sha256": hashlib.sha256(data).hexdigest(), "path": ctx.rel(path)}


def cmd_verify(ctx: Ctx, a) -> Result:
    if not a.check:
        raise AKError("no --check given", "verify needs at least one check command", 'agentkit verify --check "pytest -q"')
    for c in a.check:
        v = permissions.check(c, ctx.config)
        if not v.allowed:
            raise AKError(f"check refused by permission rule '{v.rule}'", v.why or "", "use a check command that is allowed")
    timeout = a.timeout or float(ctx.config.get("run", {}).get("timeout", 120))
    want_receipt = bool(getattr(a, "receipt", False))
    base_commit = _git(ctx.root, "rev-parse", "--verify", a.base, check=False).strip()
    if want_receipt and not base_commit:
        raise AKError("--receipt requires a valid git base commit", "use a git repository and a valid --base ref",
                      "agentkit verify --check 'pytest -q' --receipt")
    t0 = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix="agentkit-verify-"))
    where = tmp
    base_where = None
    mode = "?"
    baseline_checks = []
    try:
        if want_receipt:
            base_where = _prepare_base(ctx, tmp, a.base)
            baseline_checks = run_checks(a.check, base_where, timeout, ctx.state("logs"))
        where, changes, mode = _prepare_copy(ctx, tmp, a.base)
        checks = run_checks(a.check, where, timeout, ctx.state("logs"))
    finally:
        if base_where is not None:
            _cleanup(ctx, base_where, "git-worktree")
        _cleanup(ctx, where, mode)
        shutil.rmtree(tmp, ignore_errors=True)

    if any(c["result"] == "invalid" for c in checks):
        verdict = "invalid"
    elif all(c["result"] == "pass" for c in checks):
        verdict = "pass"
    else:
        verdict = "fail"
    if want_receipt:
        baseline_invalid = any(c["result"] == "invalid" for c in baseline_checks)
        baseline_failed = any(c["result"] == "fail" for c in baseline_checks)
        if baseline_invalid or not baseline_failed:
            verdict = "invalid"

    flags = base_flags(changes, a.check, checks, verdict, [ctx.root])
    if verdict == "pass" and flags:
        verdict = "needs_review"

    report = {
        "verdict": verdict,
        "checks": checks,
        "changed": [f"{s} {p}" for p, s in sorted(changes.items())],
        "suspicious": flags,
        "cost": {"s": round(time.monotonic() - t0, 1), "tokens": 0, "usd": 0},
        "env": mode,
    }
    if want_receipt:
        baseline_result = ("invalid" if any(c["result"] == "invalid" for c in baseline_checks) else
                           "fail" if any(c["result"] == "fail" for c in baseline_checks) else "pass")
        report["baseline"] = {"required": True, "result": baseline_result, "checks": baseline_checks}
        report["receipt"] = _write_direct_receipt(ctx, base_commit, changes, checks, baseline_checks,
                                                  verdict, flags, report["cost"], mode,
                                                  ctx.run_id or f"verify-{time.time_ns()}")
    if verdict == "invalid":
        if want_receipt and report.get("baseline", {}).get("result") == "pass":
            report["hint"] = "baseline checks did not fail; fix the baseline or remove --receipt"
        else:
            report["hint"] = "a check command is missing or broken; fix the check, this is not a code failure"
    code = {"fail": "AK-VERIFY-FAIL", "invalid": "AK-VERIFY-INVALID",
            "needs_review": "AK-VERIFY-SUSPICIOUS"}.get(verdict)
    if code:
        report["code"] = code
        report["run"] = ctx.run_id
    if ctx.run_id:
        from .events import EventLog

        ev = EventLog(ctx.root, ctx.run_id, "verifier")
        for i, c in enumerate(baseline_checks):
            ev.emit("check", step=i + 1, duration=c.get("s"), result=c["result"], phase="baseline",
                    error_code={"fail": "AK-VERIFY-FAIL", "invalid": "AK-VERIFY-INVALID"}.get(c["result"]),
                    cmd=c["cmd"][:120], log=c.get("log"), why=c.get("why"))
        for i, c in enumerate(checks):
            ev.emit("check", step=i + 1, duration=c.get("s"), result=c["result"], phase="acceptance",
                    error_code={"fail": "AK-VERIFY-FAIL", "invalid": "AK-VERIFY-INVALID"}.get(c["result"]),
                    cmd=c["cmd"][:120], log=c.get("log"), why=c.get("why"))
    # full report on disk, compact one on stdout
    vdir = ctx.state("verify")
    rid = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    full_p = vdir / f"verify-{rid}.json"
    full_p.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if verdict == "pass" and not flags:
        _remember_checks(ctx, a.check)  # never learn from a run that looks like cheating

    compact, trunc = _compact(report, REPORT_TOKENS * 4 - 120 - len(ctx.rel(full_p)))
    nxt = ctx.next_cmd("read", ctx.rel(full_p)) if trunc else None
    if not nxt and verdict == "needs_review" and ctx.run_id:
        # C-12: suspicious flags may be a false alarm the host can judge; one cheap hint, never on clean runs
        from .diag import feedback_hint

        nxt = feedback_hint(ctx.root, ctx.run_id, t={}, rep={"verdict": "needs_review", "suspicious": flags}, evs=[])
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
