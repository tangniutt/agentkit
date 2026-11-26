# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for verify's NUL-delimited Git paths and copy boundary."""
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentkit import verify
from agentkit.core import AKError, Ctx
from conftest import git

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider"


def _project(repo, files=None):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_baseline.py").write_text("def test_baseline():\n    assert True\n")
    for rel, text in (files or {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "core.quotepath", "true")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")


def _legacy_changes(root, base):
    """The pre-fix line parser, kept here to prove the Unicode false pass."""
    def text(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True).stdout

    out = {}
    for line in text("diff", "--name-status", "--no-renames", base, "--").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out[parts[-1]] = parts[0][0]
    for f in text("ls-files", "--others", "--exclude-standard").splitlines():
        if f:
            out.setdefault(f, "A")
    return out


def _worktrees(repo):
    out = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                         check=True, capture_output=True, text=True).stdout
    return out.count("\n")


@contextmanager
def _prepared(repo, tmp_path):
    ctx = Ctx(root=repo, cwd=repo)
    tmp = tmp_path / "verify-copy"
    tmp.mkdir()
    try:
        where, changes, mode = verify._prepare_copy(ctx, tmp, "HEAD")
        try:
            yield where, changes, mode
        finally:
            verify._cleanup(ctx, where, mode)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _directory_link(target, link, kind):
    if kind == "symlink":
        link.symlink_to(target, target_is_directory=True)
        return
    # Windows junctions do not need Developer Mode or administrator privileges.
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                   check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")


def test_unicode_failure_test_was_missed_but_now_fails(ak, repo):
    _project(repo)
    rel = "reviewtests/test_失败.py"
    failing = repo / rel
    failing.parent.mkdir()
    failing.write_text("def test_worker_broke_it():\n    assert 1 == 2\n", encoding="utf-8")

    # Default core.quotepath quotes this path; the old parser then looks for a
    # filename containing literal quotes/octal escapes and silently skips it.
    legacy = _legacy_changes(repo, "HEAD")
    assert legacy and rel not in legacy
    assert all(p.startswith('"') for p in legacy)

    result = ak("verify", "--check", PYTEST)
    assert result["data"]["verdict"] == "fail", result
    assert result["data"]["checks"][0]["exit"] != 0
    assert result["data"]["changed"] == [f"A {rel}"]
    assert verify._git_changes(repo, "HEAD") == {rel: "A"}


def test_unicode_modified_and_deleted_paths_are_detected_and_copied(ak, repo):
    check = (
        "from pathlib import Path\n"
        "assert Path('src/修改.py').read_text(encoding='utf-8') == 'new\\n'\n"
        "assert not Path('src/删除.py').exists()\n"
    )
    _project(repo, {"src/修改.py": "old\n", "src/删除.py": "old\n", "check_unicode.py": check})
    (repo / "src" / "修改.py").write_text("new\n", encoding="utf-8")
    (repo / "src" / "删除.py").unlink()

    assert verify._git_changes(repo, "HEAD") == {"src/修改.py": "M", "src/删除.py": "D"}
    result = ak("verify", "--check", f"{sys.executable} check_unicode.py")
    assert result["data"]["verdict"] == "pass", result
    assert result["data"]["changed"] == ["M src/修改.py", "D src/删除.py"]


def test_space_paths_keep_status_and_contents(ak, repo):
    check = (
        "from pathlib import Path\n"
        "assert Path('space dir/changed file.txt').read_text() == 'new'\n"
        "assert Path('space dir/new file.txt').read_text() == 'added'\n"
        "assert not Path('space dir/deleted file.txt').exists()\n"
    )
    _project(repo, {
        "space dir/changed file.txt": "old",
        "space dir/deleted file.txt": "old",
        "space dir/check files.py": check,
    })
    (repo / "space dir" / "changed file.txt").write_text("new")
    (repo / "space dir" / "new file.txt").write_text("added")
    (repo / "space dir" / "deleted file.txt").unlink()

    expected = {
        "space dir/changed file.txt": "M",
        "space dir/deleted file.txt": "D",
        "space dir/new file.txt": "A",
    }
    assert verify._git_changes(repo, "HEAD") == expected
    result = ak("verify", "--check", 'python "space dir/check files.py"')
    assert result["data"]["verdict"] == "pass", result
    assert result["data"]["changed"] == [f"{expected[p]} {p}" for p in sorted(expected)]


def test_rename_keeps_delete_add_behavior_and_copies_destination(repo, tmp_path):
    _project(repo, {"old name.py": "payload\n"})
    (repo / "old name.py").rename(repo / "新 name.py")
    git(repo, "add", "-A")  # staged so git diff can emit an R100 record

    expected = {"old name.py": "D", "新 name.py": "A"}
    assert verify._git_changes(repo, "HEAD") == expected
    with _prepared(repo, tmp_path) as (where, changes, mode):
        assert mode == "git-worktree"
        assert changes == expected
        assert not (where / "old name.py").exists()
        assert (where / "新 name.py").read_text(encoding="utf-8") == "payload\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits non-UTF-8 filename bytes")
def test_git_changes_preserve_non_utf8_path_bytes(repo):
    _project(repo)
    raw = os.fsencode(repo) + b"/raw-\xff.py"
    fd = os.open(raw, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(b"x = 1\n")
    name = os.fsdecode(b"raw-\xff.py")
    assert verify._git_changes(repo, "HEAD") == {name: "A"}


def test_prepare_copy_rejects_external_file_symlink_without_copying(repo, tmp_path, monkeypatch):
    _project(repo)
    outside = repo.parent / "outside.py"
    outside.write_text("SECRET = 'outside'\n")
    (repo / "escape.py").symlink_to(outside)
    copied = []
    copy2 = shutil.copy2

    def guarded_copy(src, dst, *args, **kwargs):
        copied.append(Path(src))
        return copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(verify.shutil, "copy2", guarded_copy)
    tmp = tmp_path / "verify-copy"
    tmp.mkdir()
    try:
        with pytest.raises(AKError, match="path outside worktree"):
            verify._prepare_copy(Ctx(root=repo, cwd=repo), tmp, "HEAD")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert copied == []
    assert outside.read_text() == "SECRET = 'outside'\n"
    assert _worktrees(repo) == 1


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_prepare_copy_rejects_external_parent_link(repo, tmp_path, monkeypatch, kind):
    if kind == "junction" and os.name != "nt":
        pytest.skip("Windows junction regression")
    _project(repo, {"linked/tracked.py": "old\n"})
    outside = repo.parent / f"outside-{kind}"
    outside.mkdir()
    (outside / "tracked.py").write_text("worker change\n")
    shutil.rmtree(repo / "linked")
    _directory_link(outside, repo / "linked", kind)
    monkeypatch.setattr(verify, "_git_changes", lambda *a: {"linked/tracked.py": "M"})

    tmp = tmp_path / "verify-copy"
    tmp.mkdir()
    try:
        with pytest.raises(AKError, match="path outside worktree"):
            verify._prepare_copy(Ctx(root=repo, cwd=repo), tmp, "HEAD")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert (outside / "tracked.py").read_text() == "worker change\n"
    assert _worktrees(repo) == 1


def test_prepare_copy_does_not_write_through_external_destination_symlink(repo, tmp_path):
    _project(repo, {"victim.txt": "placeholder\n"})
    git(repo, "config", "core.symlinks", "true")
    outside = repo.parent / "outside-victim.txt"
    outside.write_text("SECRET\n")
    victim = repo / "victim.txt"
    victim.unlink()
    victim.symlink_to(outside)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline symlink")
    victim.unlink()
    victim.write_text("worker change\n")
    assert verify._git_changes(repo, "HEAD") == {"victim.txt": "T"}

    tmp = tmp_path / "verify-copy"
    tmp.mkdir()
    try:
        with pytest.raises(AKError, match="path outside worktree"):
            verify._prepare_copy(Ctx(root=repo, cwd=repo), tmp, "HEAD")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert outside.read_text() == "SECRET\n"
    assert _worktrees(repo) == 1


def test_non_git_copy_also_rejects_external_symlink(repo, tmp_path, monkeypatch):
    outside = repo.parent / "outside-non-git.py"
    outside.write_text("SECRET\n")
    (repo / "ok.py").write_text("OK = True\n")
    (repo / "escape.py").symlink_to(outside)
    copied = []
    copy2 = shutil.copy2

    def guarded_copy(src, dst, *args, **kwargs):
        copied.append(Path(src))
        return copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(verify.shutil, "copy2", guarded_copy)
    monkeypatch.setattr(verify, "list_files", lambda *a: ["ok.py", "escape.py"])
    tmp = tmp_path / "verify-copy"
    tmp.mkdir()
    with pytest.raises(AKError, match="path outside worktree"):
        verify._prepare_copy(Ctx(root=repo, cwd=repo), tmp, "HEAD")
    assert [p.name for p in copied] == ["ok.py"]
    assert outside.read_text() == "SECRET\n"
