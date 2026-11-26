# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""P0 path, memory, budget and long-line regressions."""
import os
import subprocess

import pytest


def test_edit_rejects_junction_escape(ak, repo):
    outside = repo.parent / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("BEFORE\n", encoding="utf-8")
    link = repo / "link"
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            pytest.skip("directory junctions are unavailable")
    else:
        os.symlink(outside, link, target_is_directory=True)
    try:
        out = ak("edit", "link/victim.txt", "--old", "BEFORE", "--new", "AFTER")
        assert not out["ok"] and out["error"]["code"] == "AK-PATH"
        assert victim.read_text(encoding="utf-8") == "BEFORE\n"
    finally:
        if os.name == "nt":
            os.rmdir(link)
        else:
            os.unlink(link)


def test_external_paths_are_rejected(ak, repo):
    outside = repo.parent / "outside.txt"
    outside.write_text("SECRET\n", encoding="utf-8")
    assert not ak("read", str(outside))["ok"]
    assert not ak("search", "SECRET", str(outside))["ok"]
    assert not ak("checkpoint", "create", str(outside))["ok"]
    assert not ak("run", "echo nope", "--cwd", str(repo.parent))["ok"]


def test_memory_id_cannot_escape(ak, repo):
    outside = repo.parent / "outside.md"
    outside.write_text("OUTSIDE\n", encoding="utf-8")
    memdir = repo / ".agentkit" / "memory"
    mid = os.path.relpath(str(outside), str(memdir)).replace(chr(92), "/")[:-3]
    assert ".." in mid
    assert not ak("memory", "show", mid)["ok"]
    assert not ak("memory", "add", "escape", "ESCAPED", "--id", mid)["ok"]
    assert not ak("memory", "forget", mid)["ok"]
    assert outside.read_text(encoding="utf-8") == "OUTSIDE\n"


def test_long_line_has_real_continuation(ak, repo):
    (repo / "long.txt").write_text("A" * 1000, encoding="utf-8")
    first = ak("read", "long.txt")
    assert first["truncated"] and first["data"]["long_lines_cut"] is True
    assert "--full-lines" in first["next"]
    full = ak.next(first)
    assert full["ok"] and len(full["data"]["content"]) >= 1000


def test_small_budget_never_exceeds_envelope(ak, repo):
    out = ak("read", "missing-" + "x" * 300, budget=50)
    assert not out["ok"] and out["truncated"]
    assert len(out["_raw"]) <= 200
