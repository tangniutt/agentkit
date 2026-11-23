# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-19 acceptance: "an AI that has never seen this project installs it from llms.txt alone".

Simulates that AI mechanically: it reads ONLY llms.txt, runs the shell commands of the
"## Install" section verbatim (comments stripped) in a fresh virtualenv with an empty HOME,
then checks `agentkit doctor` is healthy and does one `agentkit verify` exactly as the
"Direct mode" section shows it. Nothing from README, the repo checkout or the dev
environment is used (except an optional local source override for unpushed changes).

    python -m eval.llms_check                      # installs from GitHub (needs network)
    python -m eval.llms_check --src .              # install the local checkout instead of git+https://...
    python -m eval.llms_check --json               # machine-readable result

Exit code 0 = pass. Prints one JSON line with every step, its exit code and duration.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT_URL = "git+https://github.com/tangniutt/agentkit"


def section(text: str, title: str) -> list:
    """Lines of the markdown section whose heading starts with `## title`."""
    out, inside = [], False
    for ln in text.splitlines():
        if ln.startswith("## "):
            inside = ln[3:].strip().lower().startswith(title.lower())
            continue
        if inside:
            out.append(ln)
    return out


def commands(lines: list) -> list:
    """Shell commands as an AI would copy them: non-empty, not prose, trailing `# comment` removed.
    Lines starting with '-' or '>' or containing no command word are prose."""
    cmds = []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith(("#", "-", ">", "(")):
            continue
        s = re.sub(r"\s+#\s.*$", "", s).strip()
        if s:
            cmds.append(s)
    return cmds


def install_commands(llms: str) -> list:
    return commands(section(llms, "Install"))


def split_install(cmds: list) -> tuple:
    """(alternatives, follow-ups): lines before the first `agentkit ...` are alternatives, tried in
    order until one exits 0 ("Run the FIRST line that succeeds"); `agentkit` lines run after."""
    i = next((k for k, c in enumerate(cmds) if c.split()[0] == "agentkit"), len(cmds))
    return cmds[:i], [c for c in cmds[i:] if c.split()[0] == "agentkit" and "--yes" not in c and "uninstall" not in c]


def path_hint(llms: str):
    """The PATH fix llms.txt tells the AI to apply when `agentkit` is not found."""
    for ln in section(llms, "Install"):
        m = re.search(r'export PATH="([^"]+)"', ln)
        if m and "not found" in ln:
            return m.group(1)
    return None


def verify_example(llms: str) -> str:
    for ln in section(llms, "Direct mode"):
        s = ln.strip()
        if s.startswith("agentkit verify "):
            return re.sub(r"\s+#\s.*$", "", s).strip()
    raise SystemExit("llms.txt: no `agentkit verify` line in the Direct mode section")


def run(cmd: str, cwd: Path, env: dict, timeout: float = 600) -> dict:
    t0 = time.monotonic()
    p = subprocess.run(["bash", "-c", cmd], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout)
    return {"cmd": cmd, "exit": p.returncode, "s": round(time.monotonic() - t0, 1),
            "stdout": p.stdout, "stderr": p.stderr[-2000:]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llms", default=str(ROOT / "llms.txt"))
    ap.add_argument("--src", help="install this local path instead of the GitHub URL (for unpushed changes)")
    ap.add_argument("--python", default=sys.executable, help="interpreter used to create the fresh venv")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--scenario", choices=["venv", "bare"], default="venv",
                    help="venv: fresh virtualenv on PATH; bare: system python3 without pip on PATH (PEP 668 distros), "
                         "only git/uv/pipx if the machine has them")
    a = ap.parse_args(argv)
    llms = Path(a.llms).read_text()
    steps, ok, why = [], True, ""
    tmp = Path(tempfile.mkdtemp(prefix="ak-llms-"))
    try:
        home, work, venv = tmp / "home", tmp / "work", tmp / "venv"
        home.mkdir()
        work.mkdir()
        if a.scenario == "venv":
            subprocess.run([a.python, "-m", "venv", str(venv)], check=True, capture_output=True)
            env = {"PATH": f"{venv / 'bin'}:/usr/local/bin:/usr/bin:/bin", "VIRTUAL_ENV": str(venv)}
        else:
            shim = tmp / "shim"
            shim.mkdir()
            for tool in ("git", "uv", "uvx", "pipx", "bash", "env", "ln", "mkdir", "sh"):
                w = shutil.which(tool)
                if w:
                    (shim / tool).symlink_to(w)
            (shim / "python3").symlink_to(a.python)
            env = {"PATH": str(shim)}
        env.update({"HOME": str(home), "LANG": "C.UTF-8", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"})
        for k in ("PIP_CACHE_DIR", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                  "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        if "PIP_CACHE_DIR" not in env:  # reuse the machine's download cache; HOME itself stays empty
            env["PIP_CACHE_DIR"] = os.path.expanduser("~/.cache/pip")
        src = None
        if a.src:  # copy the tracked files so pip's build dir never lands in the checkout
            src = tmp / "src"
            files = subprocess.run(["git", "-C", a.src, "ls-files", "-co", "--exclude-standard"], capture_output=True,
                                   text=True, check=True).stdout.splitlines()
            for f in files:
                if (Path(a.src) / f).is_file():
                    (src / f).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(Path(a.src) / f, src / f)
        cmds = install_commands(llms)
        alts, follow = split_install(cmds)
        if not alts:
            ok, why = False, "Install section has no install command"
        install_s, chosen = 0.0, None
        for c in alts:
            cc = c.replace(GIT_URL, shlex.quote(str(src))) if src else c
            r = run(cc, work, env)
            steps.append(r)
            install_s += r["s"]
            if r["exit"] == 0:
                chosen = c
                break
        if alts and chosen is None:
            ok, why = False, "every install alternative failed"
        if ok and run("command -v agentkit", work, env)["exit"] != 0:
            hint = path_hint(llms)
            if hint:  # the AI applies the documented PATH fix
                env["PATH"] = hint.replace("$HOME", env["HOME"]).replace("$PATH", env["PATH"])
                steps.append({"cmd": f'export PATH="{hint}"', "exit": 0, "s": 0})
        for c in follow if ok else []:
            r = run(c, work, env)
            steps.append(r)
            if r["exit"] != 0 and not c.startswith("agentkit doctor"):
                ok, why = False, f"step failed: {c}"
                break
            if c.startswith("agentkit doctor"):
                try:
                    d = json.loads(r["stdout"].strip().splitlines()[-1])
                    if not (d["ok"] and d["data"]["healthy"]):
                        ok, why = False, "doctor not healthy"
                except (ValueError, KeyError, IndexError):
                    ok, why = False, f"doctor did not print JSON (exit {r['exit']})"
                    break
        if ok and not any(c.startswith("agentkit doctor") for c in cmds):
            ok, why = False, "Install section never runs agentkit doctor"
        if ok:  # one verify, as the Direct mode section shows it
            repo = work / "proj"
            repo.mkdir()
            (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
            ex = verify_example(llms)
            check = 'python3 -c "import calc; assert calc.add(2, 3) == 5"'
            cmd = re.sub(r'--check\s+"[^"]*"', "--check " + shlex.quote(check), ex.split(" [")[0])
            r = run(cmd, repo, env, timeout=300)
            steps.append(r)
            try:
                d = json.loads(r["stdout"].strip().splitlines()[-1])
                if d["data"]["verdict"] != "pass":
                    ok, why = False, f"verify verdict {d['data']['verdict']}"
            except (ValueError, KeyError, IndexError, TypeError):
                ok, why = False, "verify did not print a verdict"
        res = {"ok": ok, "why": why, "scenario": a.scenario, "chosen": chosen, "install_s": round(install_s, 1), "python": a.python,
               "src": a.src or GIT_URL, "steps": [{k: v for k, v in s.items() if k in ("cmd", "exit", "s")} for s in steps]}
        if not ok and steps:
            res["last_stderr"] = steps[-1]["stderr"][-400:]
            res["last_stdout"] = steps[-1]["stdout"][-300:]
        print(json.dumps(res))
        return 0 if ok else 1
    finally:
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
