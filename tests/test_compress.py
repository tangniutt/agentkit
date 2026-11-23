# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""C-08: command-aware output filters, safety validator, raw recovery, session dedup,
progressive aging, terse style and the byte-stable prompt prefix."""
import json
import random

from conftest import git

from agentkit import compress as C
from agentkit.context import AGED, TranscriptCompressor
from test_m2_tasks import CHECK, FIX, create, project, tjson, wait


# ---------------------------------------------------------------- filters as data

def test_every_filter_has_passing_inline_samples():
    names = {f["id"] for f in C.load_filters()}
    for need in ("pytest", "unittest", "jest", "go-test", "cargo-test", "tsc", "eslint", "ruff", "mypy",
                 "pip", "npm-install", "git-status", "git-diff", "git-log", "generic"):
        assert need in names
    assert C.verify_filters() == []


def test_filter_selection_by_command_and_by_output():
    assert C.select_filter("cd sub && python -m pytest -q tests", "")["id"] == "pytest"
    assert C.select_filter("FOO=1 npx jest", "")["id"] == "jest"
    assert C.select_filter("uv run ruff check .", "")["id"] == "ruff"
    assert C.select_filter("git diff HEAD~1", "")["id"] == "git-diff"
    assert C.select_filter("bash ci.sh", "==== 1 failed, 2 passed in 0.1s ====")["id"] == "pytest"
    assert C.select_filter("make", "nothing special")["id"] == "generic"


_SEV = ["E       AssertionError: boom", "ERROR: could not build wheel", "Traceback (most recent call last):",
        "FAILED tests/t.py::test_x - assert 1 == 2", "npm ERR! code ERESOLVE", "panic: runtime error: index out of range",
        "src/a.ts(3,7): error TS2322: bad", "thread 'main' panicked at src/lib.rs:3:5", "fatal: not a git repository"]


def test_severity_lines_survive_every_filter():
    rnd = random.Random(7)
    noise = ["Collecting x==1.0", "tests/test_a.py ........ [ 40%]", "npm http fetch GET 200 https://r/x 5ms",
             "   Compiling dep v0.1.0", "=== RUN   TestX", "test t::ok ... ok", "\x1b[32mok\x1b[0m", "[=====>     ] 50%",
             "index 1111111..2222222 100644", "Author: someone", "  (use \"git add\" to update)"]
    for f in C.load_filters():
        for trial in range(5):
            lines = [rnd.choice(noise) for _ in range(300)]
            sev = rnd.sample(_SEV, 4)
            for s in sev:
                lines.insert(rnd.randrange(len(lines)), s)
            text = "\n".join(lines) + "\n"
            out, info = C.compress("x", text, filt=f, force=True)
            for s in sev:
                assert s in out, (f["id"], s, info)


def test_validator_rejects_lost_code_paths_urls_versions_and_errors():
    orig = ("see https://example.com/docs and ```py\nx = 1\n```\n"
            "ERROR: pkg/mod.py failed with numpy 1.26.4\n")
    assert C.validate(orig, orig) == []
    assert any("code block" in p for p in C.validate(orig, orig.replace("x = 1", "x=1")))
    assert any("important line" in p for p in C.validate(orig, orig.replace("ERROR: pkg/mod.py failed with numpy 1.26.4", "")))
    assert C.validate(orig, "") == ["compressed output is empty"]


def test_compress_falls_back_to_original_when_validation_fails(monkeypatch):
    text = "\n".join(["noise"] * 100 + ["ERROR: real problem in a/b.py"]) + "\n"
    monkeypatch.setattr(C, "apply_filter", lambda t, f: "noise [×100]\n")
    out, info = C.compress("make", text, force=True)
    assert out == text and "fallback" in info and not info["applied"]


def test_small_output_untouched():
    text = "On branch main\nnothing to commit, working tree clean\n"
    out, info = C.compress("git status", text)
    assert out == text and not info["applied"]


def test_pytest_300_repeated_warnings_shrink_and_keep_failure():
    blocks = "".join(f"tests/test_m.py::test_{i}\n  /r/pkg/old.py:12: DeprecationWarning: old_api() is deprecated\n"
                     f"    warnings.warn('old_api() is deprecated', DeprecationWarning)\n\n" for i in range(300))
    text = ("tests/test_m.py " + "." * 299 + "F [100%]\n=================================== FAILURES ===================================\n"
            "__________________________________ test_299 ___________________________________\n\n    def test_299():\n"
            ">       assert old_api() == 2\nE       assert 1 == 2\n\ntests/test_m.py:900: AssertionError\n"
            "=============================== warnings summary ===============================\n" + blocks +
            "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_m.py::test_299 - assert 1 == 2\n============ 1 failed, 299 passed, 300 warnings in 1.10s ============\n")
    out, info = C.compress("pytest", text)
    assert info["applied"] and len(out) < len(text) / 10
    for s in ("E       assert 1 == 2", "FAILED tests/test_m.py::test_299", "1 failed, 299 passed, 300 warnings",
              "DeprecationWarning: old_api() is deprecated  [×300]", "https://docs.pytest.org/en/stable/how-to/capture-warnings.html"):
        assert s in out


def test_best_why_prefers_summary_over_trailing_noise():
    assert C.best_why("x\n1 failed, 2 passed in 0.1s\n\n") == "1 failed, 2 passed in 0.1s"
    assert C.best_why("ok\nall done\n") == "all done"


# ---------------------------------------------------------------- run integration + raw recovery

def _pytest_like(n=200):
    return ("".join(f"tests/test_{i}.py::test_ok PASSED [ {i % 100}%]\n" for i in range(n)) +
            "FAILED tests/test_x.py::test_bad - assert 1 == 2\n==== 1 failed, %d passed in 0.5s ====\n" % n)


def test_run_filters_output_and_raw_is_recoverable(ak, gitrepo):
    (gitrepo / "out.txt").write_text(_pytest_like())
    r = ak("run", "cat out.txt")
    d = r["data"]
    assert d["filtered"]["filter"] == "pytest" and "FAILED tests/test_x.py::test_bad" in d["stdout_tail"]
    assert "PASSED" not in d["stdout_tail"]
    raw_cmd = d["filtered"]["raw"].split()
    assert raw_cmd[:2] == ["agentkit", "logs"] and raw_cmd[-1] == "--raw"
    raw = ak("logs", raw_cmd[2], "--raw", budget=20000)
    assert raw["ok"] and raw["data"]["stdout"] == (gitrepo / "out.txt").read_text()
    # --raw on run, and [compress] run_filters=false, both switch the filter off
    r2 = ak("run", "cat out.txt", "--raw", budget=20000)
    assert "filtered" not in r2["data"] and "PASSED" in r2["data"]["stdout_tail"]
    (gitrepo / ".agentkit" / "config.toml").write_text("[compress]\nrun_filters = false\n")
    r3 = ak("run", "cat out.txt", budget=20000)
    assert "filtered" not in r3["data"]


def test_logs_raw_rejects_bad_ids(ak, gitrepo):
    r = ak("logs", "../../etc/passwd", "--raw")
    assert not r["ok"] and r["error"]["code"] == "AK-LOGS-UNKNOWN"
    r = ak("logs", "run-20990101-000000-1-1", "--raw")
    assert not r["ok"] and r["error"]["code"] == "AK-LOGS-UNKNOWN"


def test_verify_why_uses_summary_line(ak, gitrepo):
    (gitrepo / "f.sh").write_text("echo '1 failed, 3 passed in 0.2s'\necho 'cleanup done'\nexit 1\n")
    git(gitrepo, "add", "-A")
    git(gitrepo, "commit", "-qm", "x")
    r = ak("verify", "--check", "sh f.sh")
    assert r["data"]["checks"][0]["why"] == "1 failed, 3 passed in 0.2s"


# ---------------------------------------------------------------- transcript compression (unit)

def _env(content):
    return {"ok": True, "data": {"path": "a.py", "lines": [1, 50], "total_lines": 50, "sha": "abc", "content": content},
            "error": None, "truncated": False, "next": None}


def test_session_dedup_and_aging_keep_pairs_and_prefix():
    tc = TranscriptCompressor({"age_min_chars": 1000})
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "TASK"}]
    big = "x = 1\n" * 200
    for turn in range(1, 6):
        cid = f"c{turn}"
        msgs.append({"role": "assistant", "tool_calls": [{"id": cid, "type": "function",
                                                         "function": {"name": "read", "arguments": json.dumps({"spec": "a.py"})}}]})
        env = _env(big)
        content = json.dumps(env)
        msgs.append({"role": "tool", "tool_call_id": cid, "content": tc.tool_result(msgs, turn, cid, "read", {"spec": "a.py"}, env, content)})
    assert "same_as_turn" in msgs[5]["content"] and json.loads(msgs[5]["content"])["data"]["same_as_turn"] == 1
    tc.age(msgs)
    assert msgs[0]["content"] == "SYS" and msgs[1]["content"] == "TASK"
    # the last 2 turns are verbatim; the referenced text moved into the window before its source was aged
    window = [m for m in msgs[-4:] if m["role"] == "tool"]
    assert all(not m["content"].startswith(AGED) for m in window)
    assert any(big[:50] in json.loads(m["content"])["data"].get("content", "") for m in window
               if "content" in (json.loads(m["content"])["data"] or {}))
    assert msgs[3]["content"].startswith(AGED)
    ids = [c["id"] for m in msgs if m["role"] == "assistant" for c in m["tool_calls"]]
    assert ids == [m["tool_call_id"] for m in msgs if m["role"] == "tool"]


def test_aging_is_batched():
    tc = TranscriptCompressor({"age_min_chars": 100000})
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "T"}]
    for i in range(4):
        msgs.append({"role": "assistant", "tool_calls": [{"id": f"c{i}", "function": {"name": "ls", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 2000})
    assert tc.age(msgs) == 0 and not any(str(m.get("content", "")).startswith(AGED) for m in msgs)


def test_recent_turns_never_aged_early():
    """Fix found while adding C-09: with <= keep_turns assistant turns nothing is old yet. The window used to be
    treated as 'everything old', so a small age_min_chars aged the file the worker had just read (and made the
    end-to-end dedup test depend on two pytest runs printing the same timing)."""
    tc = TranscriptCompressor({"age_min_chars": 10})
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "T"}]
    for i in range(2):
        msgs.append({"role": "assistant", "tool_calls": [{"id": f"c{i}", "function": {"name": "ls", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 2000})
        assert tc.age(msgs) == 0 and not any(str(m.get("content", "")).startswith(AGED) for m in msgs)
    msgs.append({"role": "assistant", "tool_calls": [{"id": "c2", "function": {"name": "ls", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "tool_call_id": "c2", "content": "y" * 2000})
    assert tc.age(msgs) > 0 and msgs[3]["content"].startswith(AGED) and msgs[5]["content"] == "y" * 2000


# ---------------------------------------------------------------- worker end-to-end (fake model)

def test_worker_compression_end_to_end_stable_prefix(ak, repo, fake):
    project(repo, ak, fake, cfg="\n[compress]\nage_min_chars = 500\n")
    (repo / "big.py").write_text("".join(f"VALUE_{i} = {i}  # filler line to make the file large\n" for i in range(120)))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "big")
    steps = [{"call": "read", "args": {"spec": "big.py"}}, {"call": "read", "args": {"spec": "big.py"}},
             {"call": "run", "args": {"command": CHECK}}, {"call": "run", "args": {"command": CHECK}}] + FIX
    fake.add("[CC]", steps)
    tid = create(ak, "fix add [CC]")
    s = wait(ak, tid)
    assert s["verdict"] == "pass"
    reqs = fake.requests_for("[CC]")
    bodies = [r["body"] for r in reqs]
    # byte-stable prefix: system prompt (with memory brief), task message, tool schemas
    first = bodies[0]
    for b in bodies[1:]:
        assert json.dumps(b["messages"][:2]) == json.dumps(first["messages"][:2])
        assert json.dumps(b["tools"]) == json.dumps(first["tools"])
    # small repo, style=auto: the note is not in the prefix; it is appended once, when compression kicks in
    assert "Output style: terse" not in first["messages"][0]["content"]
    notes = [i for i, m in enumerate(bodies[-1]["messages"]) if "Output style: terse" in str(m.get("content"))]
    assert len(notes) == 1 and notes[0] >= 2 and bodies[-1]["messages"][notes[0]]["role"] == "user"
    last = bodies[-1]["messages"]
    assert any("same_as_turn" in str(m.get("content")) for m in last)
    assert any(str(m.get("content", "")).startswith(AGED) for m in last)
    ids = [c["id"] for m in last if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    assert ids == [m["tool_call_id"] for m in last if m["role"] == "tool"]
    st = tjson(repo, tid)["compression"]
    assert st["deduped"] >= 1 and st["aged"] >= 1 and st["saved_chars"] > 0, st
    assert st["style"].startswith("round "), st


def _style_hits(body) -> list:
    return [i for i, m in enumerate(body["messages"]) if "Output style: terse" in str(m.get("content"))]


def test_style_note_costs_tiny_tasks_nothing(ak, repo, fake):
    """C-08 follow-up: the ~70-token style note made tiny tasks +5.7% worker tokens. With style=auto a short
    task in a small repo never gets it (not in the prefix, never appended)."""
    project(repo, ak, fake)
    fake.add("[TINY]", [{"call": "read", "args": {"spec": "calc.py"}}] + FIX)
    tid = create(ak, "fix add [TINY]")
    assert wait(ak, tid)["verdict"] == "pass"
    for r in fake.requests_for("[TINY]"):
        assert _style_hits(r["body"]) == []
    assert tjson(repo, tid)["compression"]["style"] == "off"


def test_style_note_appended_once_when_transcript_grows(ak, repo, fake):
    project(repo, ak, fake, cfg="\n[compress]\naging = false\ndedup = false\nstyle_after_chars = 1500\n")
    (repo / "big.py").write_text("".join(f"VALUE_{i} = {i}  # filler line to make the file large\n" for i in range(120)))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "big")
    fake.add("[GROW]", [{"call": "read", "args": {"spec": "big.py:1-40"}}, {"call": "read", "args": {"spec": "big.py:41-80"}}] + FIX)
    tid = create(ak, "fix add [GROW]")
    assert wait(ak, tid)["verdict"] == "pass"
    bodies = [r["body"] for r in fake.requests_for("[GROW]")]
    assert _style_hits(bodies[0]) == []
    hits = [_style_hits(b) for b in bodies]
    k = next(i for i, h in enumerate(hits) if h)
    at = hits[k][0]
    for b in bodies[k:]:  # appended once, then carried unchanged at the same position (append-only)
        assert _style_hits(b) == [at]
        assert json.dumps(b["messages"][:at + 1]) == json.dumps(bodies[k]["messages"][:at + 1])
    for b in bodies[1:]:
        assert json.dumps(b["messages"][:2]) == json.dumps(bodies[0]["messages"][:2])
    assert bodies[k]["messages"][-1]["role"] == "user"
    assert tjson(repo, tid)["compression"]["style"] == f"round {k + 1}"


def test_style_note_in_prefix_for_big_repo_or_when_forced(ak, repo, fake):
    project(repo, ak, fake, cfg="\n[compress]\nstyle_min_files = 5\n")
    for i in range(5):
        (repo / f"mod{i}.py").write_text(f"X = {i}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "more files")
    fake.add("[BIGREPO]", [{"call": "read", "args": {"spec": "calc.py"}}] + FIX)
    tid = create(ak, "fix add [BIGREPO]")
    assert wait(ak, tid)["verdict"] == "pass"
    for r in fake.requests_for("[BIGREPO]"):
        assert _style_hits(r["body"]) == [0]  # in the system prompt from round 1, never appended again
    assert tjson(repo, tid)["compression"]["style"] == "start"


def test_style_terse_forces_note_and_normal_disables_it(ak, repo, fake):
    project(repo, ak, fake, cfg="\n[compress]\nstyle = \"terse\"\n")
    fake.add("[FORCE]", FIX)
    tid = create(ak, "fix add [FORCE]")
    assert wait(ak, tid)["verdict"] == "pass"
    assert all(_style_hits(r["body"]) == [0] for r in fake.requests_for("[FORCE]"))
    (repo / ".agentkit" / "config.toml").write_text((repo / ".agentkit" / "config.toml").read_text()
                                                    .replace('style = "terse"', 'style = "normal"\nstyle_after_chars = 1'))
    fake.add("[NORMAL]", [{"call": "read", "args": {"spec": "calc.py"}}] + FIX)
    tid = create(ak, "fix add [NORMAL]")
    assert wait(ak, tid)["verdict"] == "pass"
    assert all(_style_hits(r["body"]) == [] for r in fake.requests_for("[NORMAL]"))
    assert tjson(repo, tid)["compression"]["style"] == "off"


def test_worker_compression_can_be_switched_off(ak, repo, fake):
    project(repo, ak, fake, cfg="\n[compress]\nenabled = false\n")
    fake.add("[NOCC]", [{"call": "read", "args": {"spec": "calc.py"}}, {"call": "read", "args": {"spec": "calc.py"}}] + FIX)
    tid = create(ak, "fix add [NOCC]")
    assert wait(ak, tid)["verdict"] == "pass"
    last = fake.requests_for("[NOCC]")[-1]["body"]["messages"]
    assert "Output style: terse" not in last[0]["content"]
    assert not any("same_as_turn" in str(m.get("content")) for m in last)


def test_run_git_diff_lists_every_changed_file_first(ak, gitrepo):
    for n in ("a.txt", "b.txt"):
        (gitrepo / n).write_text("".join(f"old {i}\n" for i in range(400)))
    git(gitrepo, "add", "-A")
    git(gitrepo, "commit", "-qm", "x")
    for n in ("a.txt", "b.txt"):
        (gitrepo / n).write_text("".join(f"new {i}\n" for i in range(400)))
    r = ak("run", "git diff")
    d = r["data"]
    assert d["filtered"]["filter"] == "git-diff"
    assert d["stdout_tail"].startswith("[2 files] a.txt (+400 -400), b.txt (+400 -400)")
    assert "diff --git a/b.txt b/b.txt" in d["stdout_tail"] and "lines omitted in this file" in d["stdout_tail"]


def test_bench_never_hides_a_key_line_that_was_visible_before(capsys):
    from eval.compress_bench import main

    assert main([]) == 0
    assert "pytest-300-warnings" in capsys.readouterr().out
