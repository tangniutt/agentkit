# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

KEYS = {"ok", "data", "error", "truncated", "next"}


class AK:
    def __init__(self, cwd: Path):
        self.cwd = Path(cwd)
        self.env = dict(os.environ)

    def __call__(self, *args, stdin=None, budget=None, cwd=None, check_schema=True, env=None):
        argv = [sys.executable, "-m", "agentkit", *map(str, args)]
        if budget is not None:
            argv += ["--budget", str(budget)]
        e = dict(self.env)
        if env:
            e.update(env)
        p = subprocess.run(argv, cwd=str(cwd or self.cwd), input=stdin, capture_output=True, text=True, env=e, timeout=300)
        out = p.stdout.strip()
        assert out.count("\n") == 0, f"stdout must be one JSON line: {p.stdout[:500]} / {p.stderr[-500:]}"
        res = json.loads(out)
        res["_raw"] = out
        res["_code"] = p.returncode
        if check_schema:
            assert set(k for k in res if not k.startswith("_")) == KEYS, res
            if not res["ok"]:
                assert set(res["error"]) == {"what", "why", "next"}, res
            if res["truncated"]:
                assert res["next"], f"truncated output must carry next: {out[:300]}"
            b = budget if budget is not None else 2000
            assert len(out) <= b * 4, f"budget exceeded: {len(out)} > {b * 4}"
        return res

    def next(self, res, budget=None):
        """Follow a `next` command string."""
        import shlex

        parts = shlex.split(res["next"])
        assert parts[0] == "agentkit"
        parts = parts[1:]
        if "--budget" in parts:
            i = parts.index("--budget")
            budget = int(parts[i + 1])
            del parts[i:i + 2]
        return self(*parts, budget=budget)


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    return r


@pytest.fixture
def ak(repo):
    return AK(repo)


@pytest.fixture
def gitrepo(repo):
    git(repo, "init", "-q", "-b", "main")
    return repo
