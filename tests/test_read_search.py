# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-04 read, M-05 search/ls/glob, M-06 repo_map."""
import pytest

from conftest import git

SAMPLES = {
    "m.py": ("class Shape:\n    def area(self):\n        return 0\n\ndef helper(x):\n    return x * 2\n", "Shape.area", 2, 3),
    "m.js": ("function alpha(a) {\n  return a + 1;\n}\nclass Beta {\n  run() {\n    return 2;\n  }\n}\n", "Beta.run", 5, 7),
    "m.ts": ("interface Opt { a: number }\nexport const gamma = (x: number): number => {\n  return x;\n};\n", "gamma", 2, 4),
    "m.go": ("package m\n\nfunc Delta(a int) int {\n\treturn a\n}\n\ntype T struct{}\n", "Delta", 3, 5),
    "m.rs": ("struct P;\nimpl P {\n    fn eps(&self) -> i32 {\n        1\n    }\n}\n", "P.eps", 3, 5),
    "M.java": ("class M {\n  int zeta() {\n    return 1;\n  }\n}\n", "M.zeta", 2, 4),
}


def test_read_range_and_single_line(ak, repo):
    (repo / "f.txt").write_text("".join(f"l{i}\n" for i in range(1, 101)))
    r = ak("read", "f.txt:10-12")
    assert r["data"]["content"] == "10|l10\n11|l11\n12|l12\n" and r["data"]["total_lines"] == 100
    assert ak("read", "f.txt:50")["data"]["content"] == "50|l50\n"
    assert ak("read", "f.txt:99-")["data"]["lines"] == [99, 100]
    bad = ak("read", "f.txt:500-510")
    assert not bad["ok"] and "beyond" in bad["error"]["what"]


@pytest.mark.parametrize("name", list(SAMPLES))
def test_read_symbol_treesitter(ak, repo, name):
    src, sym, start, end = SAMPLES[name]
    (repo / name).write_text(src)
    r = ak("read", f"{name}#{sym}")
    assert r["ok"], r
    assert r["data"]["lines"] == [start, end], r["data"]


def test_read_symbol_regex_fallback(ak, repo):
    (repo / "m.py").write_text(SAMPLES["m.py"][0])
    r = ak("read", "m.py#helper", env={"AGENTKIT_NO_TREESITTER": "1"})
    assert r["ok"] and r["data"]["lines"] == [5, 6]
    miss = ak("read", "m.py#nope")
    assert not miss["ok"] and "Shape" in str(miss["data"]["symbols"])


def test_read_binary_rejected(ak, repo):
    (repo / "b.bin").write_bytes(b"\x89PNG\x00\x01\x02" * 100)
    r = ak("read", "b.bin")
    assert not r["ok"] and "binary" in r["error"]["what"]


def test_read_over_1mb_is_chunked(ak, repo):
    (repo / "huge.log").write_text("".join(f"row {i} " + "x" * 80 + "\n" for i in range(20000)))
    assert (repo / "huge.log").stat().st_size > 1_000_000
    r = ak("read", "huge.log", budget=100000)
    assert r["truncated"] and r["data"]["lines"][1] <= 2000
    assert "huge.log:" in r["next"]


def test_read_secret_file_refused(ak, repo):
    fake = "".join(["sk", "-", "abcdefghijklmnopqrstuvwxyz123456"])
    (repo / ".env").write_text(f"OPENAI_API_KEY={fake}\n")
    r = ak("read", ".env")
    assert not r["ok"] and "secret" in r["error"]["what"]
    r2 = ak("read", ".env", "--allow-secret-file")
    assert r2["ok"] and "sk-abc" not in r2["_raw"]
    (repo / ".env.example").write_text("OPENAI_API_KEY=\n")
    assert ak("read", ".env.example")["ok"]


def _search_fixture(repo):
    (repo / ".gitignore").write_text("build/\n*.log\n")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\nTARGET = 2\n")
    (repo / "build").mkdir()
    (repo / "build" / "gen.py").write_text("TARGET = 3\n")
    (repo / "debug.log").write_text("TARGET\n")


@pytest.mark.parametrize("engine", ["rg", "python"])
@pytest.mark.parametrize("use_git", [True, False])
def test_search_respects_gitignore(ak, repo, engine, use_git):
    import shutil
    if engine == "rg" and not shutil.which("rg"):
        pytest.skip("ripgrep not installed")
    _search_fixture(repo)
    if use_git:
        git(repo, "init", "-q")
    r = ak("search", "TARGET", env={"AGENTKIT_SEARCH_ENGINE": engine})
    assert r["data"]["matches"] == ["src/a.py:2:TARGET = 2"], r
    r2 = ak("search", "TARGET", "--no-ignore", env={"AGENTKIT_SEARCH_ENGINE": engine})
    assert len(r2["data"]["matches"]) == 3
    assert ak("search", "target", "-i", env={"AGENTKIT_SEARCH_ENGINE": engine})["data"]["matches"]
    assert ak("search", "x = (", "--fixed", env={"AGENTKIT_SEARCH_ENGINE": engine})["data"]["matches"] == []
    bad = ak("search", "x = (", env={"AGENTKIT_SEARCH_ENGINE": engine})
    assert not bad["ok"] and "regex" in bad["error"]["what"]


def test_ls_glob_respect_gitignore(ak, repo):
    _search_fixture(repo)
    ls = ak("ls")
    assert "src/ (1 files)" in ls["data"]["entries"] and not any("build" in e for e in ls["data"]["entries"])
    assert ak("glob", "**/*.py")["data"]["paths"] == ["src/a.py"]
    assert ak("glob", "*.py")["data"]["paths"] == ["src/a.py"]
    assert ak("glob", "*.log", "--no-ignore")["data"]["paths"] == ["debug.log"]


def test_repo_map_languages_and_fallback(ak, repo):
    for name, (src, *_rest) in SAMPLES.items():
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / name).write_text(src)
    m = ak("repo_map")
    syms = m["data"]["symbols"]
    flat = " ".join(" ".join(v) for v in syms.values())
    for s in ("Shape", "alpha", "gamma", "Delta", "eps", "zeta"):
        assert s in flat, (s, flat)
    assert m["data"]["engine"] == "tree-sitter"
    fb = ak("repo_map", env={"AGENTKIT_NO_TREESITTER": "1"})
    assert fb["ok"] and fb["data"]["engine"] == "regex" and "Shape" in str(fb["data"]["symbols"])


def test_repo_map_10k_files_within_budget(ak, repo):
    for i in range(10000):
        d = repo / f"d{i % 50}"
        d.mkdir(exist_ok=True)
        (d / f"f{i}.py").write_text(f"def fn_{i}():\n    return fn_{(i * 7) % 10000}()\n")
    m = ak("repo_map")
    assert m["data"]["files"] == 10000
    assert len(m["_raw"]) <= 8000 and m["truncated"] and m["next"]
