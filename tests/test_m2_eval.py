# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-20 eval harness (offline) and M-15 cheat library: detection must be 100%."""
import json
import os
import subprocess
import sys

from eval import run as R
from eval.cheats import CHEATS
from eval.tasks import TASKS

ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}  # same-size rewrite within 1 s would reuse a stale .pyc


def test_eval_set_size():
    assert len(TASKS) >= 20 and len({t["id"] for t in TASKS}) == len(TASKS)
    assert len(CHEATS) >= 10 and len({c["id"] for c in CHEATS}) == len(CHEATS)


def test_eval_tasks_start_broken_and_reference_fix_passes(tmp_path):
    for t in TASKS:
        root = R.make_repo(tmp_path / t["id"], t["files"])
        r = subprocess.run(R.sub(t["check"]), shell=True, cwd=root, capture_output=True, env=ENV)
        assert r.returncode != 0, f"{t['id']}: check must fail before the fix"
        for path, old, new in t["fix"]:
            p = root / path
            text = p.read_text()
            assert text.count(old) == 1, f"{t['id']}: fix snippet must be unique"
            p.write_text(text.replace(old, new))
        r = subprocess.run(R.sub(t["check"]), shell=True, cwd=root, capture_output=True, env=ENV)
        assert r.returncode == 0, f"{t['id']}: reference fix must pass: {r.stdout[-300:]}"


def test_eval_offline_runner_baseline_vs_worker(tmp_path, capsys):
    out = tmp_path / "res.json"
    code = R.main(["--jobs", "6", "--out", str(out)])
    assert code == 0
    res = json.loads(out.read_text())
    direct = [r for r in res["rows"] if r["mode"] == "direct"]
    worker = [r for r in res["rows"] if r["mode"] == "worker"]
    assert len(direct) == len(worker) == len(TASKS)
    assert all(r["pass"] for r in direct), [r for r in direct if not r["pass"]]
    assert all(r["pass"] for r in worker), [r for r in worker if not r["pass"]]
    assert all(r["worker_tokens"] > 0 and r["host_tokens"] > 0 for r in worker)
    table = capsys.readouterr().out
    assert "| direct |" in table and "| worker |" in table and "pass rate" in table


def test_cheat_library_detection_is_100_percent(tmp_path, capsys):
    out = tmp_path / "cheats.json"
    code = R.main(["--only-cheats", "--jobs", "7", "--out", str(out)])
    res = json.loads(out.read_text())
    missed = [c for c in res["cheats"] if not c["detected"]]
    assert not missed, missed
    assert code == 0 and len(res["cheats"]) == len(CHEATS)
    for c in res["cheats"]:
        assert c["verdict"] in ("needs_review", "fail", "invalid")


def test_runner_cli_help():
    r = subprocess.run([sys.executable, "-m", "eval.run", "--help"], capture_output=True, text=True,
                       cwd=str(R.HERE.parent))
    assert r.returncode == 0 and "--real" in r.stdout
