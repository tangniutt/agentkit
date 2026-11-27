# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-20 eval harness (offline) and M-15 cheat library: detection must be 100%."""
import json
import os
import subprocess
import sys

import pytest

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


@pytest.mark.skipif(os.name != "posix", reason="dispatch mode requires Linux")
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
    assert "| direct (large) |" in table and "| worker (tiny) |" in table and "| worker (all) |" in table and "pass rate" in table
    # token claim (C-06), offline: on multi-file tasks the host reads far less when it dispatches
    big = {m: sum(r["host_tokens"] for r in res["rows"] if r["mode"] == m and r["size"] == "large") for m in ("direct", "worker")}
    assert len([t for t in TASKS if t["size"] == "large"]) >= 3
    assert big["worker"] * 3 < big["direct"], big


@pytest.mark.skipif(os.name != "posix", reason="dispatch mode requires Linux")
def test_cheat_library_detection_is_100_percent(tmp_path, capsys):
    out = tmp_path / "cheats.json"
    code = R.main(["--only-cheats", "--jobs", "7", "--out", str(out)])
    res = json.loads(out.read_text())
    missed = [c for c in res["cheats"] if not c["detected"]]
    assert not missed, missed
    assert code == 0 and len(res["cheats"]) == len(CHEATS)
    for c in res["cheats"]:
        assert c["verdict"] in ("needs_review", "fail", "invalid")


@pytest.mark.skipif(os.name != "posix", reason="external dispatch requires Linux")
def test_eval_external_worker_uses_same_acceptance_contract(tmp_path):
    script = '''import json, sys
from pathlib import Path
sys.stdin.read()
p = Path("calc.py")
p.write_text(p.read_text().replace("a - b", "a + b"))
print(json.dumps({"type":"step_start","timestamp":1,"sessionID":"ses_eval","part":{"id":"p1","type":"step-start"}}))
print(json.dumps({"type":"text","timestamp":2,"sessionID":"ses_eval","part":{"id":"p2","type":"text","text":"fixed"}}))
finish = {"type":"step_finish","timestamp":3,"sessionID":"ses_eval"}
finish["part"] = {"id":"p3","type":"step-finish","reason":"stop","cost":0.25}
finish["part"]["tokens"] = {"input":100,"output":20,"reasoning":0}
finish["part"]["tokens"]["cache"] = {"read":0,"write":0}
print(json.dumps(finish))
'''
    config = ("[workers.opencode]\n"
              f"command = [{json.dumps(sys.executable)}, \"-c\", {json.dumps(script)}]\n"
              "model = \"fake/model\"\nallow_unsandboxed = true\nprice_in = 1\nprice_out = 1\n")
    out = tmp_path / "external.json"
    code = R.main(["--backend", "opencode", "--model-id", "fake/model", "--worker-config", config,
                   "--tasks", "ev-add", "--no-direct", "--budget-tokens", "1000", "--budget-usd", "1",
                   "--budget-minutes", "2", "--out", str(out)])
    res = json.loads(out.read_text())
    assert code == 0 and res["backend"] == "opencode"
    assert res["schema_version"] == "1.0" and res["git_commit"]
    assert res["task_ids"] == ["ev-add"] and len(res["results_sha256"]) == 64
    assert len(res["worker_config_sha256"]) == 64
    assert res["budget"] == {"minutes": 2, "tokens": 1000, "usd": 1.0, "cheat_minutes": 5}
    assert len(res["rows"]) == 1
    row = res["rows"][0]
    assert row["backend"] == "opencode" and row["pass"] and row["verdict"] == "pass"
    assert row["worker_tokens"] == 120 and row["usd"] == pytest.approx(0.25)
    assert row["host_tokens"] > 0
    rendered = R.table(res["rows"], "external")
    assert "| worker (opencode, tiny) |" in rendered


def test_runner_returns_failure_when_any_task_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "run_worker", lambda *_args, **_kwargs: {
        "id": "ev-add", "mode": "worker", "backend": "builtin", "size": "tiny", "pass": False,
        "verdict": "fail", "host_tokens": 1, "worker_tokens": 0, "usd": 0.0})
    out = tmp_path / "failed.json"
    code = R.main(["--tasks", "ev-add", "--no-direct", "--out", str(out)])
    assert code == 1 and json.loads(out.read_text())["rows"][0]["pass"] is False


def test_runner_rejects_empty_task_selection():
    with pytest.raises(SystemExit) as error:
        R.main(["--tasks", "missing-task"])
    assert error.value.code == 2


def test_runner_cli_help():
    r = subprocess.run([sys.executable, "-m", "eval.run", "--help"], capture_output=True, text=True,
                       cwd=str(R.HERE.parent))
    assert r.returncode == 0 and "--real" in r.stdout and "--backend" in r.stdout
    assert "--budget-tokens" in r.stdout and "--budget-usd" in r.stdout and "--budget-minutes" in r.stdout
