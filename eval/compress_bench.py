# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""C-08 measurement: what the command-aware filters save on verbose real-style logs.

  python -m eval.compress_bench [--budget 2000] [--transcript PATH ...] [--json OUT]

For every fixture (real captures in eval/fixtures/logs/ + generated ones below) it reports:
  raw        tokens of the whole output (chars / 4, JSON-escaped)
  before     tokens `agentkit run` showed before C-08 (tail-first fit into the output budget)
  after      tokens shown with the filter (same budget fit)
  key lines  how many of the fixture's must-see lines are visible before / after
--transcript replays a real worker transcript (.agentkit/tasks/ID/transcript.jsonl) and sums the
prompt size of every model call with and without session dedup + progressive aging.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentkit.compress import compress, select_filter
from agentkit.core import dumps
from agentkit.run import fit_tails

FIX = Path(__file__).with_name("fixtures") / "logs"


def _pytest_warnings() -> str:
    blocks = "".join(f"tests/test_api.py::test_case_{i}\n  /src/app/legacy.py:41: DeprecationWarning: parse_v1() is deprecated, use parse()\n"
                     f"    warnings.warn(\"parse_v1() is deprecated, use parse()\", DeprecationWarning)\n\n" for i in range(300))
    return ("============================= test session starts ==============================\n"
            "platform linux -- Python 3.12.3, pytest-8.3.4, pluggy-1.5.0\nrootdir: /src\ncollected 301 items\n\n"
            "tests/test_api.py " + "." * 70 + " [ 23%]\n" + ("tests/test_api.py " + "." * 70 + " [ 46%]\n") +
            ("tests/test_api.py " + "." * 70 + " [ 70%]\n") + ("tests/test_api.py " + "." * 70 + " [ 93%]\n") +
            "tests/test_api.py ....................F [100%]\n\n"
            "=================================== FAILURES ===================================\n"
            "_______________________________ test_roundtrip ________________________________\n\n"
            "    def test_roundtrip():\n        doc = parse(SAMPLE)\n>       assert dump(doc) == SAMPLE\n"
            "E       AssertionError: assert 'a: 1\\nb: 2' == 'a: 1\\nb: 2\\n'\nE         - a: 1\nE         - b: 2\n\n"
            "tests/test_api.py:212: AssertionError\n"
            "=============================== warnings summary ===============================\n" + blocks +
            "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_api.py::test_roundtrip - AssertionError: assert 'a: 1\\nb: 2' == 'a: 1\\nb: 2\\n'\n"
            "================= 1 failed, 300 passed, 300 warnings in 4.21s ==================\n")


def _tsc() -> str:
    errs = "".join(f"src/components/Widget{i}.tsx({10 + i},5): error TS2322: Type 'string | undefined' is not assignable to type 'string'.\n"
                   f"  Type 'undefined' is not assignable to type 'string'.\n" for i in range(40))
    return errs + "\nFound 40 errors in 40 files.\n\nErrors  Files\n" + "".join(f"     1  src/components/Widget{i}.tsx:{10 + i}\n" for i in range(40))


def _cargo() -> str:
    comp = "".join(f"   Compiling crate{i} v0.{i}.3\n" for i in range(80))
    ok = "".join(f"test tests::case_{i} ... ok\n" for i in range(150))
    return (comp + "    Finished `test` profile [unoptimized + debuginfo] target(s) in 41.20s\n"
            "     Running unittests src/lib.rs (target/debug/deps/app-1a2b3c)\n\nrunning 151 tests\n" + ok +
            "test tests::parse_header ... FAILED\n\nfailures:\n\n---- tests::parse_header stdout ----\n"
            "thread 'tests::parse_header' panicked at src/lib.rs:88:9:\nassertion `left == right` failed\n  left: 3\n right: 4\n"
            "note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace\n\n\nfailures:\n    tests::parse_header\n\n"
            "test result: FAILED. 150 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.31s\n")


def _jest() -> str:
    passes = "".join(f"PASS src/feature{i}/index.test.ts\n  ✓ renders {i} (12 ms)\n  ✓ handles click {i} (4 ms)\n" for i in range(60))
    return (passes + "FAIL src/cart/total.test.ts\n  ● cart › applies discount\n\n    expect(received).toBe(expected) // Object.is equality\n\n"
            "    Expected: 90\n    Received: 100\n\n      12 |   const t = total(cart, 0.1);\n    > 13 |   expect(t).toBe(90);\n"
            "         |             ^\n\n      at Object.<anonymous> (src/cart/total.test.ts:13:15)\n\n"
            "Test Suites: 1 failed, 60 passed, 61 total\nTests:       1 failed, 120 passed, 121 total\nSnapshots:   0 total\nTime:        8.41 s\n")


GENERATED = {
    "pytest-300-warnings (synthetic)": ("python -m pytest -q", _pytest_warnings,
                                        ["E       AssertionError", "FAILED tests/test_api.py::test_roundtrip", "1 failed, 300 passed",
                                         "tests/test_api.py:212: AssertionError", "parse_v1() is deprecated"]),
    "tsc 40 errors (synthetic)": ("npx tsc --noEmit", _tsc, ["Widget0.tsx(10,5): error TS2322", "Widget39.tsx(49,5): error TS2322", "Found 40 errors"]),
    "cargo test (synthetic)": ("cargo test", _cargo, ["test tests::parse_header ... FAILED", "panicked at src/lib.rs:88:9", "left: 3",
                                                       "test result: FAILED. 150 passed; 1 failed"]),
    "jest (synthetic)": ("npm test", _jest, ["FAIL src/cart/total.test.ts", "Expected: 90", "Received: 100", "Tests:       1 failed, 120 passed"]),
}
REAL = {
    "npm install (real)": ("npm-install.txt", "npm install --loglevel http express@4 request@2",
                           ["added 111 packages", "5 vulnerabilities (3 moderate, 2 critical)"]),
    "pip install (real)": ("pip-install.txt", "pip install requests==2.32.3 rich==13.9.4", ["Successfully installed"]),
    "git diff, 3 files (real)": ("git-diff.txt", "git diff 99cdca3 cdda359 -- agentkit/", ["agentkit/models.py", "agentkit/tasks.py", "agentkit/worker.py"]),
    "git log -25 (real)": ("git-log.txt", "git log -25", ["feat(m2): uninstall", "docs: STATUS records CI result"]),
    "git status (real, small)": ("git-status.txt", "git status", ["On branch main"]),
    "pytest -v, 1 failure (real)": ("pytest-pass-verbose.txt", "python -m pytest -v tests", ["FAILED tests/test_compress.py::test_worker_compression_end_to_end_stable_prefix",
                                                                                           "1 failed, 42 passed", "E       assert (0 >= 1)"]),
}


def _tok(s: str) -> int:
    return len(dumps(s)) // 4


def measure(name: str, command: str, text: str, keys: list, budget: int) -> dict:
    room = budget * 4 - 400  # what `run` leaves for stdout/stderr after the envelope (approximation)
    _, before, *_ = fit_tails("", text, room)
    comp, info = compress(command, text)
    f = select_filter(command, text)
    _, after, *_ = fit_tails("", comp, room, head_first=info["applied"] and f.get("fit") == "head")
    return {"name": name, "filter": info["filter"], "raw": _tok(text), "before": _tok(before), "after": _tok(after),
            "filtered_full": _tok(comp), "keys": len(keys), "keys_before": sum(k in before for k in keys),
            "keys_after": sum(k in after for k in keys), "applied": info["applied"], "fallback": info.get("fallback")}


def replay(path: Path, cfg: dict = None) -> dict:
    """Prompt chars summed over every model call of a real transcript, without / with dedup + aging."""
    from agentkit.context import TranscriptCompressor

    msgs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    msgs = [m for m in msgs if m.get("role") in ("system", "user", "assistant", "tool")]
    tc = TranscriptCompressor(cfg or {})
    plain, comp, calls, turn = 0, 0, 0, 0
    live: list = []
    for i, m in enumerate(msgs):
        if m["role"] == "assistant":
            # a model call happened with everything before this message as the prompt
            tc.age(live)
            plain += len(dumps(msgs[:i]))
            comp += len(dumps(live))
            calls += 1
            turn += 1
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                tc.note_call(c.get("id"), fn.get("name"), args if isinstance(args, dict) else {})
            live.append(json.loads(json.dumps(m)))
        elif m["role"] == "tool":
            name, args = tc.calls.get(m.get("tool_call_id"), ("tool", {}))
            try:
                env = json.loads(m["content"])
            except ValueError:
                env = {}
            content = tc.tool_result(live, turn, m.get("tool_call_id"), name, args, env if isinstance(env, dict) else {}, m["content"])
            live.append({**m, "content": content})
        else:
            live.append(dict(m))
    return {"transcript": str(path), "calls": calls, "prompt_tokens_plain": plain // 4, "prompt_tokens_compressed": comp // 4,
            **tc.stats()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.compress_bench")
    ap.add_argument("--budget", type=int, default=2000)
    ap.add_argument("--transcript", action="append", default=[])
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    rows = []
    for name, (cmd, gen, keys) in GENERATED.items():
        rows.append(measure(name, cmd, gen(), keys, a.budget))
    for name, (fn, cmd, keys) in REAL.items():
        p = FIX / fn
        if p.exists():
            rows.append(measure(name, cmd, p.read_text(errors="replace"), keys, a.budget))
    print(f"### C-08 output filters (run budget {a.budget} tokens; tokens = chars/4)\n")
    print("| fixture | filter | raw | before C-08 (shown) | after C-08 (shown) | saved vs before | whole filtered | key lines before → after |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        sv = 100 * (r["before"] - r["after"]) / r["before"] if r["before"] else 0
        print(f"| {r['name']} | {r['filter']}{'' if r['applied'] else ' (not applied)'} | {r['raw']:,} | {r['before']:,} | {r['after']:,} | "
              f"{sv:.0f}% | {r['filtered_full']:,} | {r['keys_before']}/{r['keys']} → {r['keys_after']}/{r['keys']} |")
    reps = [replay(Path(t)) for t in a.transcript]
    if reps:
        print("\n### Session dedup + progressive aging on real transcripts (prompt tokens summed over all model calls)\n")
        print("| transcript | calls | plain | with C-08 | saved | dedup refs | aged results |")
        print("|---|---|---|---|---|---|---|")
        for r in reps:
            sv = 100 * (r["prompt_tokens_plain"] - r["prompt_tokens_compressed"]) / max(1, r["prompt_tokens_plain"])
            print(f"| {Path(r['transcript']).parent.name} | {r['calls']} | {r['prompt_tokens_plain']:,} | {r['prompt_tokens_compressed']:,} | "
                  f"{sv:.0f}% | {r['deduped']} | {r['aged']} |")
    if a.json:
        Path(a.json).write_text(json.dumps({"filters": rows, "transcripts": reps}, indent=1))
    bad = [r for r in rows if r["keys_after"] < r["keys_before"]]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
