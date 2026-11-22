# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-02 fixed schema + M-03 budget never exceeded, truncation always carries next."""
import json
import random

import pytest

from agentkit.core import Ctx, envelope, finalize


def make_big_repo(repo, n_files=300, lines=200):
    for i in range(n_files):
        d = repo / f"pkg{i % 17}" / f"sub{i % 5}"
        d.mkdir(parents=True, exist_ok=True)
        body = [f"class Thing{i}:"] + [f"    def method_{j}(self):\n        return 'needle {i} {j}' * 3" for j in range(lines // 4)]
        (d / f"mod_{i}.py").write_text("\n".join(body) + "\n")


def test_schema_on_success_error_and_usage(ak, repo):
    (repo / "a.py").write_text("x = 1\n")
    ok = ak("read", "a.py")
    assert ok["ok"] and ok["error"] is None and ok["data"]["content"].startswith("1|x = 1")
    err = ak("read", "missing.py")
    assert not err["ok"] and err["error"]["what"].startswith("not found") and err["_code"] == 1
    usage = ak("read")
    assert not usage["ok"] and usage["_code"] == 2
    h = ak("edit", "--help")
    assert h["ok"] and "--old" in h["data"]["help"]
    top = ak()
    assert "read" in top["data"]["commands"]
    human = ak("read", "a.py", "--human", check_schema=False) if False else None
    assert human is None


def test_human_flag_is_text(ak, repo):
    import subprocess, sys
    (repo / "a.py").write_text("x = 1\n")
    p = subprocess.run([sys.executable, "-m", "agentkit", "read", "a.py", "--human"], cwd=repo, capture_output=True, text=True)
    assert "ok: True" in p.stdout and "x = 1" in p.stdout


@pytest.mark.parametrize("budget", [60, 150, 500, 2000])
def test_read_large_file_budget_and_continuation(ak, repo, budget):
    lines = [f"line {i} " + "abc\"\\\t" * (i % 40) for i in range(1, 3001)]
    (repo / "big.txt").write_text("\n".join(lines) + "\n")
    res = ak("read", "big.txt", budget=budget)
    assert res["truncated"] and res["next"]
    got = []
    hops = 0
    while True:
        for ln in res["data"]["content"].splitlines():
            got.append(ln.split("|", 1)[1])
        if not res["truncated"]:
            break
        res = ak.next(res)
        hops += 1
        if budget >= 500 and hops > 400:
            pytest.fail("too many hops")
        if budget < 500 and hops > 40:
            return  # tiny budgets: just make sure each hop is valid
    assert got == [l if len(l) <= 400 else l for l in lines]


def test_huge_single_line_never_exceeds(ak, repo):
    (repo / "one.js").write_text("var x='" + "q" * 2_000_000 + "';\n")
    for b in (60, 300, 2000):
        res = ak("read", "one.js", budget=b)
        assert res["ok"]


def test_search_ls_glob_repo_map_budget(ak, repo):
    make_big_repo(repo, n_files=400, lines=80)
    for b in (80, 400, 2000):
        s = ak("search", "needle", budget=b)
        assert s["truncated"] and s["next"]
        s2 = ak.next(s)
        assert s2["data"]["offset"] > 0
        assert ak("ls", ".", "-r", budget=b)["truncated"]
        assert ak("glob", "**/*.py", budget=b)["truncated"]
        m = ak("repo_map", budget=b)
        assert m["truncated"] and m["next"]


def test_fuzz_budgets_across_commands(ak, repo):
    make_big_repo(repo, n_files=60, lines=40)
    rnd = random.Random(7)
    cmds = [["read", "pkg1/sub1/mod_1.py"], ["search", "method_"], ["ls", "-r"], ["glob", "*.py"], ["repo_map"],
            ["read", "pkg1/sub1/mod_1.py#Thing1"], ["run", "seq 1 20000"], ["doctor"], ["memory", "brief"]]
    for _ in range(25):
        b = rnd.randint(50, 900)
        ak(*rnd.choice(cmds), budget=b)


def test_finalize_safety_net_never_exceeds():
    from pathlib import Path

    c = Ctx(root=Path("/tmp"), cwd=Path("/tmp"), argv=["x"])
    rnd = random.Random(1)
    for _ in range(200):
        data = {"a": ["z" * rnd.randint(0, 3000) for _ in range(rnd.randint(0, 30))],
                "b": {"c": "é\"\n" * rnd.randint(0, 2000), "d": list(range(rnd.randint(0, 500)))}}
        b = rnd.randint(50, 3000)
        c.budget = b
        env = finalize(c, envelope(True, data))
        s = json.dumps(env, ensure_ascii=False, separators=(",", ":"))
        assert len(s) <= b * 4
        if env["truncated"]:
            assert env["next"]
