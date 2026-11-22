# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-07 edit/patch/create/delete/rename + syntax check, M-09 checkpoints."""
import hashlib
import json

import pytest

from agentkit import edit as edit_mod
from agentkit.core import AKError, Ctx


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_unique_replace_and_checkpoint(ak, repo):
    (repo / "a.py").write_text("def f():\n    return 1\n")
    r = ak("edit", "a.py", "--old", "return 1", "--new", "return 2")
    assert r["ok"] and r["data"]["files"][0]["action"] == "modified"
    assert (repo / "a.py").read_text() == "def f():\n    return 2\n"
    assert r["data"]["checkpoint"].startswith("cp-")
    assert ak("checkpoint", "list")["data"]["total"] == 1


def test_zero_match_returns_3_closest(ak, repo):
    body = "".join(f"def func_{i}(a, b):\n    return a + b + {i}\n\n" for i in range(30))
    (repo / "a.py").write_text(body)
    r = ak("edit", "a.py", "--old", "def func_7(a, c):\n    return a + c + 7", "--new", "x")
    assert not r["ok"] and r["data"]["matches"] == 0
    cands = r["data"]["candidates"]
    assert len(cands) == 3
    assert cands[0]["line"] == 22 and "func_7" in cands[0]["text"]
    assert (repo / "a.py").read_text() == body


def test_multi_match_returns_candidates(ak, repo):
    (repo / "a.py").write_text("x = 1\ny = 2\nx = 1\nz = 3\nx = 1\nx = 1\n")
    r = ak("edit", "a.py", "--old", "x = 1", "--new", "x = 9")
    assert not r["ok"] and r["data"]["matches"] == 4
    assert [c["line"] for c in r["data"]["candidates"]] == [1, 3, 5]
    r2 = ak("edit", "a.py", "--old", "x = 1", "--new", "x = 9", "--all")
    assert r2["ok"] and (repo / "a.py").read_text().count("x = 9") == 4


def test_patch_atomic_on_validation_failure(ak, repo):
    (repo / "a.py").write_text("A = 1\n")
    (repo / "b.py").write_text("B = 1\n")
    before = (sha(repo / "a.py"), sha(repo / "b.py"))
    ops = {"ops": [{"op": "replace", "path": "a.py", "old": "A = 1", "new": "A = 2"},
                   {"op": "create", "path": "c.py", "content": "C = 1\n"},
                   {"op": "replace", "path": "b.py", "old": "NOT THERE", "new": "B = 2"}]}
    r = ak("patch", stdin=json.dumps(ops))
    assert not r["ok"] and "op[2]" in r["error"]["what"]
    assert (sha(repo / "a.py"), sha(repo / "b.py")) == before and not (repo / "c.py").exists()


def test_patch_atomic_on_write_failure(repo, monkeypatch):
    (repo / "a.py").write_text("A = 1\n")
    (repo / "b.py").write_text("B = 1\n")
    before = sha(repo / "a.py")
    ctx = Ctx(root=repo, cwd=repo)
    real = edit_mod._write
    calls = {"n": 0}

    def flaky(p, text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full (simulated)")
        real(p, text)

    monkeypatch.setattr(edit_mod, "_write", flaky)
    ops = [{"op": "replace", "path": "a.py", "old": "A = 1", "new": "A = 2"},
           {"op": "replace", "path": "b.py", "old": "B = 1", "new": "B = 2"},
           {"op": "create", "path": "new.py", "content": "N = 1\n"}]
    with pytest.raises(AKError) as ei:
        edit_mod.apply(ctx, ops, "test")
    assert "restored" in ei.value.what
    assert sha(repo / "a.py") == before and (repo / "b.py").read_text() == "B = 1\n" and not (repo / "new.py").exists()


def test_multi_file_patch_success(ak, repo):
    (repo / "a.py").write_text("A = 1\n")
    (repo / "old.py").write_text("O = 1\n")
    (repo / "gone.py").write_text("G = 1\n")
    ops = [{"op": "replace", "path": "a.py", "old": "A = 1", "new": "A = 2"},
           {"op": "rename", "path": "old.py", "to": "pkg/new.py"},
           {"op": "delete", "path": "gone.py"},
           {"op": "create", "path": "c.py", "content": "C = 1\n"}]
    r = ak("patch", stdin=json.dumps(ops))
    assert r["ok"], r
    assert (repo / "pkg/new.py").read_text() == "O = 1\n" and not (repo / "old.py").exists()
    assert not (repo / "gone.py").exists() and (repo / "c.py").exists()
    rb = ak("checkpoint", "rollback", r["data"]["checkpoint"])
    assert rb["data"]["hash_match"]
    assert (repo / "old.py").exists() and (repo / "gone.py").exists() and not (repo / "c.py").exists()
    assert not (repo / "pkg/new.py").exists() and (repo / "a.py").read_text() == "A = 1\n"


@pytest.mark.parametrize("name,src,old,new,line", [
    ("a.py", "def f():\n    return 1\n", "return 1", "return (1", 2),
    ("a.js", "function f() {\n  return 1;\n}\n", "return 1;", "return (1;", 2),
    ("a.go", "package a\n\nfunc F() int {\n\treturn 1\n}\n", "return 1", "return (1", 4),
    ("a.rs", "fn f() -> i32 {\n    1\n}\n", "    1\n}", "    1\n", None),
    ("A.java", "class A {\n  int f() { return 1; }\n}\n", "return 1;", "return 1", 2),
])
def test_syntax_error_rejected_with_line(ak, repo, name, src, old, new, line):
    (repo / name).write_text(src)
    r = ak("edit", name, "--old", old, "--new", new)
    assert not r["ok"] and "syntax error" in r["error"]["what"], r
    assert r["data"]["line"] >= 1
    if line:
        assert r["data"]["line"] in (line, line + 1), r["data"]
    assert (repo / name).read_text() == src


def test_already_broken_file_not_blocked(ak, repo):
    (repo / "a.py").write_text("def f(:\n    pass\nX = 1\n")
    r = ak("edit", "a.py", "--old", "X = 1", "--new", "X = 2")
    assert r["ok"] and "notes" in r["data"]


def test_stale_read_refused(ak, repo):
    (repo / "a.py").write_text("A = 1\n")
    ak("read", "a.py")
    (repo / "a.py").write_text("A = 1\nB = 2\n")  # changed outside agentkit
    r = ak("edit", "a.py", "--old", "A = 1", "--new", "A = 3")
    assert not r["ok"] and "changed since you last read" in r["error"]["what"]
    ak("read", "a.py")
    assert ak("edit", "a.py", "--old", "A = 1", "--new", "A = 3")["ok"]
    # consecutive edits after our own edit are fine
    assert ak("edit", "a.py", "--old", "A = 3", "--new", "A = 4")["ok"]


def test_create_delete_rename_cli(ak, repo):
    assert ak("create", "x/y.py", "Y = 1\n")["ok"]
    assert not ak("create", "x/y.py", "Y = 2\n")["ok"]
    bad = ak("create", "z.py", "def (:\n")
    assert not bad["ok"] and not (repo / "z.py").exists()
    assert ak("rename", "x/y.py", "x/w.py")["ok"] and (repo / "x/w.py").exists()
    assert ak("delete", "x/w.py")["ok"] and not (repo / "x/w.py").exists()
    out = ak("edit", "../outside.py", "--old", "a", "--new", "b")
    assert not out["ok"] and "outside" in out["error"]["what"]
    assert ak("checkpoint", "list")["data"]["total"] == 3


def test_rollback_hash_equality_and_diff(ak, repo):
    p = repo / "m.py"
    p.write_text("".join(f"V{i} = {i}\n" for i in range(50)))
    h0 = sha(p)
    r1 = ak("edit", "m.py", "--old", "V10 = 10", "--new", "V10 = 'ten'")
    ak("edit", "m.py", "--old", "V20 = 20", "--new", "V20 = 'twenty'")
    d = ak("checkpoint", "diff", r1["data"]["checkpoint"])
    assert d["data"]["changed"] and any("twenty" in l for l in d["data"]["diff"])
    rb = ak("checkpoint", "rollback", r1["data"]["checkpoint"])
    assert rb["data"]["hash_match"] and sha(p) == h0
    # rollback is itself undoable
    ak("checkpoint", "rollback", rb["data"]["undo"].split()[-1])
    assert "twenty" in p.read_text()


def test_checkpoint_works_without_git(ak, repo):
    assert not (repo / ".git").exists()
    (repo / "a.txt").write_text("hello\n")
    ak("checkpoint", "create", "a.txt")
    (repo / "a.txt").write_text("bye\n")
    cid = ak("checkpoint", "list")["data"]["checkpoints"][0].split()[0]
    assert ak("checkpoint", "rollback", cid)["data"]["hash_match"]
    assert (repo / "a.txt").read_text() == "hello\n"
    assert (repo / ".agentkit" / ".gitignore").exists()
