# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-10 verify, M-21 memory, M-18 doctor, M-11 direct mode."""
import json
import subprocess
import sys

import pytest

from conftest import git

PY = sys.executable


def _project(repo, use_git=True):
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (repo / "check.sh").write_text(f"#!/bin/sh\n{PY} -m pytest -q tests\n")
    (repo / ".gitignore").write_text("junk/\n")
    if use_git:
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "init")


CHECK = f"{PY} -m pytest -q -p no:cacheprovider tests"


def _worktrees(repo):
    return subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout.count("\n")


def test_verify_pass_and_report_size(ak, repo):
    _project(repo)
    ak("edit", "calc.py", "--old", "return a + b", "--new", "return b + a")
    r = ak("verify", "--check", CHECK, budget=2000)
    assert r["data"]["verdict"] == "pass", r
    assert len(r["_raw"]) <= 1200
    assert r["data"]["changed"] == ["M calc.py"] and r["data"]["suspicious"] == []
    assert _worktrees(repo) == 1, "worktree must be cleaned up"
    assert set(r["data"]) >= {"verdict", "checks", "changed", "suspicious", "cost"}


def test_verify_ignores_generated_bytecode_noise(ak, repo):
    _project(repo)
    cache = repo / "__pycache__"
    cache.mkdir()
    (cache / "noise.pyc").write_bytes(b"generated")
    r = ak("verify", "--check", CHECK)
    assert r["data"]["verdict"] == "pass"
    assert not any("__pycache__" in item or item.endswith(".pyc") for item in r["data"]["changed"])


def test_verify_receipt_reruns_baseline_and_writes_evidence(ak, repo):
    _project(repo)
    ak("edit", "calc.py", "--old", "return a + b", "--new", "return a - b")
    git(repo, "add", "calc.py")
    git(repo, "commit", "-qm", "broken baseline")
    ak("edit", "calc.py", "--old", "return a - b", "--new", "return a + b")
    r = ak("verify", "--check", CHECK, "--receipt")
    assert r["data"]["verdict"] == "pass" and r["data"]["baseline"]["result"] == "fail"
    summary = r["data"]["receipt"]
    receipt = json.loads((repo / summary["path"]).read_text(encoding="utf-8"))
    assert receipt["receipt_id"] == summary["id"] and receipt["verdict"] == "pass"
    assert {c["phase"] for c in receipt["checks"]} == {"baseline", "acceptance"}
    assert receipt["base_commit"] and receipt["diff_sha256"] and receipt["environment_sha256"]


def test_verify_receipt_rejects_baseline_pass(ak, repo):
    _project(repo)
    r = ak("verify", "--check", CHECK, "--receipt")
    assert r["data"]["verdict"] == "invalid" and r["data"]["baseline"]["result"] == "pass"
    assert "baseline checks did not fail" in r["data"]["hint"]
    assert r["data"]["receipt"]["id"].startswith("er_")


def test_verify_receipt_requires_git_base(repo):
    _project(repo, use_git=False)
    from agentkit.core import AKError, Ctx
    from agentkit.verify import cmd_verify
    from types import SimpleNamespace

    with pytest.raises(AKError, match="valid git base"):
        cmd_verify(Ctx(root=repo, cwd=repo), SimpleNamespace(check=[CHECK], timeout=None, base="HEAD", receipt=True))


def test_verify_fail(ak, repo):
    _project(repo)
    ak("edit", "calc.py", "--old", "return a + b", "--new", "return a - b")
    r = ak("verify", "--check", CHECK)
    assert r["data"]["verdict"] == "fail" and r["data"]["checks"][0]["exit"] != 0


def test_verify_invalid_when_check_missing(ak, repo):
    _project(repo)
    r = ak("verify", "--check", "definitely-not-a-command-xyz --run")
    assert r["data"]["verdict"] == "invalid"
    r2 = ak("verify", "--check", "./nope.sh")
    assert r2["data"]["verdict"] == "invalid"
    r3 = ak("verify", "--check", "sh -c 'exit 127'")
    assert r3["data"]["verdict"] == "invalid"


def test_verify_flags_modified_test(ak, repo):
    _project(repo)
    ak("edit", "tests/test_calc.py", "--old", "assert add(2, 3) == 5", "--new", "assert True")
    r = ak("verify", "--check", CHECK)
    flags = {f["flag"] for f in r["data"]["suspicious"]}
    assert "tests_modified" in flags and "check_script_modified" in flags
    assert r["data"]["verdict"] == "needs_review"
    assert r["data"]["code"] == "AK-VERIFY-SUSPICIOUS"


def test_verify_flags_deleted_test_and_check_script(ak, repo):
    _project(repo)
    ak("delete", "tests/test_calc.py")
    ak("edit", "check.sh", "--old", "pytest -q tests", "--new", "pytest -q tests || true")
    r = ak("verify", "--check", "sh check.sh")
    flags = {f["flag"]: f for f in r["data"]["suspicious"]}
    assert "tests_deleted" in flags and flags["check_script_modified"]["paths"] == ["check.sh"]


def test_verify_runs_in_clean_copy(ak, repo):
    _project(repo)
    (repo / "junk").mkdir()
    (repo / "junk" / "marker").write_text("x")
    r = ak("verify", "--check", "test ! -e junk/marker")
    assert r["data"]["verdict"] == "pass" and r["data"]["env"] == "git-worktree"


def test_verify_without_git(ak, repo):
    _project(repo, use_git=False)
    ak("edit", "tests/test_calc.py", "--old", "== 5", "--new", "== 5  # same")
    r = ak("verify", "--check", CHECK)
    assert r["data"]["env"] == "temp-copy" and r["data"]["verdict"] == "needs_review"
    assert any(f["flag"] == "tests_modified" for f in r["data"]["suspicious"])


def test_verify_report_capped_with_many_changes(ak, repo):
    _project(repo)
    for i in range(80):
        (repo / f"file_with_a_rather_long_name_{i:03d}.py").write_text("x = 1\n")
    checks = []
    for i in range(6):
        checks += ["--check", f"sh -c 'echo {'e' * 200} >&2; exit {i % 2}'"]
    r = ak("verify", *checks)
    assert len(r["_raw"]) <= 1200 and r["truncated"] and r["next"].startswith("agentkit read .agentkit/verify/")


def test_verify_auto_memory_on_pass(ak, repo):
    _project(repo)
    ak("verify", "--check", CHECK)
    b = ak("memory", "brief")
    assert any("pytest" in c for c in b["data"]["core"])


# ---------------------------------------------------------------- memory


def test_memory_crud_and_search(ak, repo):
    a = ak("memory", "add", "How to run tests", "Run `pytest -q` from repo root; needs PYTHONPATH=src",
           "-c", "project-info", "--pin")
    assert a["ok"] and a["data"]["id"] == "how-to-run-tests"
    ak("memory", "add", "Flaky network test", "test_download is flaky offline; skip with -k 'not download'",
       "-c", "lessons", "--related", "how-to-run-tests")
    bad = ak("memory", "add", "x", "y", "-c", "nonsense")
    assert not bad["ok"]
    lst = ak("memory", "list")
    assert lst["data"]["total"] == 2
    s = ak("memory", "search", "flaky download")
    assert s["data"]["engine"] == "fts5" and s["data"]["results"][0].startswith("flaky-network-test")
    assert "related: how-to-run-tests" in s["data"]["results"][0]
    sh = ak("memory", "show", "flaky-network-test")
    assert "flaky offline" in sh["data"]["body"] and sh["data"]["related_titles"]
    md = (repo / ".agentkit" / "memory" / "how-to-run-tests.md").read_text()
    assert md.startswith("---\nid: ") and 'category: "project-info"' in md
    assert ak("memory", "forget", "flaky-network-test")["ok"]
    assert ak("memory", "list")["data"]["total"] == 1


def test_memory_search_grep_fallback(repo, monkeypatch):
    from agentkit import memory
    from agentkit.core import Ctx

    ctx = Ctx(root=repo, cwd=repo)
    memory.add(ctx, "Build", "use make build", "tools-models")
    monkeypatch.setattr(memory, "_fts_search", lambda *a: None)

    class A:
        mem_cmd, query, offset = "search", "make", 0
    r = memory.cmd_memory(ctx, A())
    assert r.data["engine"] == "grep" and r.data["results"]


def test_memory_brief_within_500_tokens(ak, repo):
    (repo / "AGENTS.md").write_text("# Rules\n" + "Always run the tests before committing. " * 300)
    for i in range(40):
        ak("memory", "add", f"Pinned rule number {i}", "x" * 500, "-c", "conventions", *(["--pin"] if i % 2 else []))
    b = ak("memory", "brief")
    assert len(b["_raw"]) <= 2000 and b["truncated"] and b["next"]
    small = ak("memory", "brief", budget=100)
    assert len(small["_raw"]) <= 400


def test_memory_brief_reads_agents_md(ak, repo):
    (repo / "AGENTS.md").write_text("# Agents\nTests: pytest -q\n")
    ak("memory", "add", "Core", "never edit generated/", "-c", "conventions", "--pin")
    ak("memory", "add", "Other", "details", "-c", "lessons")
    b = ak("memory", "brief")
    d = b["data"]
    assert d["agents_md"]["path"] == "AGENTS.md" and "pytest -q" in d["agents_md"]["head"]
    assert d["core"][0].startswith("Core:") and d["index"][0].startswith("other: Other")
    assert not b["truncated"]


def test_memory_anchor_stale_flag(ak, repo):
    (repo / "svc.py").write_text("def handler(x):\n    return x\n\ndef other():\n    return 1\n")
    (repo / "cfg.toml").write_text("a = 1\n")
    ak("memory", "add", "Handler contract", "handler returns its input", "-c", "project-info", "--anchor", "svc.py#handler")
    ak("memory", "add", "Config", "a must be 1", "-c", "project-info", "--anchor", "cfg.toml")
    assert "maybe_stale" not in str(ak("memory", "list")["data"])
    # change unrelated symbol -> symbol anchor stays fresh
    ak("edit", "svc.py", "--old", "return 1", "--new", "return 2")
    assert ak("memory", "show", "handler-contract")["data"]["maybe_stale"] is False
    # change anchored symbol -> stale
    ak("edit", "svc.py", "--old", "return x", "--new", "return x * 2")
    sh = ak("memory", "show", "handler-contract")
    assert sh["data"]["maybe_stale"] is True and sh["data"]["stale_anchors"] == ["svc.py#handler"]
    (repo / "cfg.toml").write_text("a = 2\n")
    lst = ak("memory", "list")["data"]["memories"]
    assert all("maybe_stale" in m for m in lst)
    (repo / "cfg.toml").unlink()
    assert "(missing)" in str(ak("memory", "show", "config")["data"]["stale_anchors"])


# ---------------------------------------------------------------- doctor / direct mode


def test_doctor(ak, repo):
    r = ak("doctor")
    names = {c["name"] for c in r["data"]["checks"]}
    assert {"version", "git", "write_permission", "disk_space", "model_config", "sandbox_network_policy"} <= names
    assert r["data"]["mode"] == "direct"
    for c in r["data"]["checks"]:
        if not c["ok"]:
            assert c.get("fix"), c


def test_doctor_rejects_invalid_network_allowlist(ak, repo):
    cfg = repo / ".agentkit" / "config.toml"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text('[sandbox]\nnetwork = ["https://example.com"]\n', encoding="utf-8")
    result = ak("doctor")
    assert result["data"]["healthy"] is False
    check = next(c for c in result["data"]["checks"] if c["name"] == "sandbox_network_policy")
    assert check["ok"] is False and check["fix"]


def test_task_create_direct_mode(ak, repo):
    r = ak("task", "create", "fix the bug")
    assert not r["ok"] and r["_code"] == 1
    assert "direct mode" in r["error"]["what"] and "agentkit" in r["error"]["next"]
    assert r["data"]["task_created"] is False


def test_all_tools_work_without_model(ak, repo, monkeypatch):
    (repo / "a.py").write_text("A = 1\n")
    for args in (["repo_map"], ["search", "A"], ["read", "a.py"], ["ls"], ["glob", "*.py"],
                 ["edit", "a.py", "--old", "A = 1", "--new", "A = 2"], ["run", "true"], ["memory", "brief"],
                 ["checkpoint", "list"], ["verify", "--check", "true"]):
        assert ak(*args)["ok"], args


def test_verify_source_dir_arg_is_not_check_script(ak, repo):
    """Dogfood M1 regression: `python -m tool textstats` must not flag textstats/ as the check script."""
    _project(repo)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "m.py").write_text("X = 1\n")
    ak("edit", "calc.py", "--old", "return a + b", "--new", "return b + a")
    r = ak("verify", "--check", f"{PY} -m compileall -q calc.py", "--check", f"{PY} -m compileall -q pkg")
    assert r["data"]["verdict"] == "pass" and r["data"]["suspicious"] == [], r
    from agentkit.verify import _check_paths
    assert _check_paths("sh check.sh", repo) == {"check.sh"}
    assert _check_paths(f"{PY} -m pytest tests/test_calc.py", repo) == {"tests/test_calc.py"}


def test_verify_auto_memory_skips_suspicious_runs(ak, repo):
    _project(repo)
    ak("verify", "--check", CHECK)
    ak("edit", "tests/test_calc.py", "--old", "assert add(2, 3) == 5", "--new", "assert True")
    ak("verify", "--check", "true")
    body = ak("memory", "show", "verified-checks")["data"]["body"]
    assert "pytest" in body and "`true`" not in body
