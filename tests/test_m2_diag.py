# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""C-03 diagnosability (run ids, error codes, event logs, logs/debug-bundle, AK-INTERNAL) and C-04 feedback."""
import json
import os
import re
import tarfile
import urllib.parse
from pathlib import Path

import pytest
import yaml

from agentkit import cli
from agentkit.events import ERRORS, classify
from conftest import git

ROOT = Path(__file__).resolve().parent.parent
SECRET = "sk-" + "proj" + "Q" * 30  # looks like a provider key; assembled at runtime

# Frozen: codes may be added, never removed or renamed (they are API for agents).
STABLE = ["AK-USAGE", "AK-CONFIG", "AK-PATH", "AK-READ-BINARY", "AK-READ-SECRET", "AK-READ-SPEC", "AK-SEARCH-REGEX",
          "AK-TOOL-EDIT-NOMATCH", "AK-TOOL-EDIT-MULTI", "AK-TOOL-EDIT-SYNTAX", "AK-TOOL-EDIT-STALE", "AK-TOOL-EDIT-OP",
          "AK-TOOL-WRITE", "AK-RUN-DENIED", "AK-CHECKPOINT", "AK-MEMORY", "AK-VERIFY-NOCHECK", "AK-VERIFY-FAIL",
          "AK-VERIFY-INVALID", "AK-VERIFY-SUSPICIOUS", "AK-MODEL-NONE", "AK-MODEL-KEYENV", "AK-MODEL-AUTH", "AK-MODEL-429",
          "AK-MODEL-TIMEOUT", "AK-MODEL-NETWORK", "AK-MODEL-HTTP", "AK-MODEL-BAD-RESPONSE", "AK-WORKER-MALFORMED",
          "AK-WORKER-STUCK", "AK-WORKER-ROUNDS", "AK-WORKER-CRASH", "AK-BUDGET", "AK-TASK-CANCELLED",
          "AK-TASK-INTERRUPTED", "AK-TASK-NOGIT", "AK-TASK-UNKNOWN", "AK-TASK-STATE", "AK-GIT", "AK-DISK-FULL",
          "AK-LOGS-UNKNOWN", "AK-INTERNAL", "AK-MODEL-UNAVAILABLE"]


def test_error_codes_stable_and_documented():
    for c in STABLE:
        assert c in ERRORS, f"error code {c} was removed/renamed"
        cause, fix = ERRORS[c]
        assert cause and fix
    doc = (ROOT / "docs" / "errors.md").read_text()
    for c in ERRORS:
        assert f"`{c}`" in doc, f"{c} missing from docs/errors.md"


def test_cli_errors_carry_codes(ak, repo):
    (repo / "a.py").write_text("x = 1\ny = 1\n")
    assert ak("read", "nope.py")["error"]["code"] == "AK-PATH"
    assert ak("edit", "a.py", "--old", "zzz", "--new", "q")["error"]["code"] == "AK-TOOL-EDIT-NOMATCH"
    assert ak("edit", "a.py", "--old", " = 1", "--new", "= 2")["error"]["code"] == "AK-TOOL-EDIT-MULTI"
    assert ak("edit", "a.py", "--old", "x = 1", "--new", "x = (")["error"]["code"] == "AK-TOOL-EDIT-SYNTAX"
    assert ak("run", "git push origin main")["error"]["code"] == "AK-RUN-DENIED"
    assert ak("search", "(")["error"]["code"] == "AK-SEARCH-REGEX"
    assert ak("verify")["error"]["code"] == "AK-VERIFY-NOCHECK"
    assert ak("task", "create", "x")["error"]["code"] == "AK-MODEL-NONE"
    assert ak("frobnicate")["error"]["code"] == "AK-USAGE"
    r = ak("read", "nope.py")
    assert re.match(r"^r\d{10}[0-9a-f]{4}$", r["error"]["run"])
    assert classify("model test failed: rate_limited") == "AK-MODEL-429"


def test_event_log_and_logs_command_redacted(ak, repo):
    (repo / "a.py").write_text("x = 1\n")
    r = ak("edit", "a.py", "--old", f"token={SECRET}", "--new", "y")
    rid = r["error"]["run"]
    raw = (repo / ".agentkit" / "logs" / "events.jsonl").read_text()
    assert SECRET not in raw
    rec = [json.loads(l) for l in raw.splitlines() if rid in l][0]
    assert {"ts", "run_id", "step", "component", "event", "duration", "error_code"} <= set(rec)
    assert rec["error_code"] == "AK-TOOL-EDIT-NOMATCH" and rec["step"] == "edit"
    lg = ak("logs", rid, "--errors")
    assert lg["ok"] and lg["data"]["shown"] == 1 and "AK-TOOL-EDIT-NOMATCH" in lg["data"]["lines"][0]
    assert SECRET not in lg["_raw"]
    assert ak("logs", "r0000000000beef")["error"]["code"] == "AK-LOGS-UNKNOWN"
    assert ak("logs", "../../etc/passwd")["error"]["code"] == "AK-LOGS-UNKNOWN"


def test_logs_budget_and_tail(ak, repo):
    (repo / "a.py").write_text("x = 1\n")
    from agentkit.events import EventLog

    ak("read", "a.py")
    ev = EventLog(repo, "tabc123", "task")
    for i in range(300):
        ev.emit("tool", step=i, component="tool:read", duration=0.01, note="n" * 50)
    r = ak("logs", "tabc123", budget=300)
    assert r["truncated"] and r["next"] and r["data"]["shown"] < 300
    assert "#299" in r["data"]["lines"][-1], "newest events first to survive truncation"
    r = ak("logs", "tabc123", "--tail", "3")
    assert r["data"]["shown"] == 3


def test_internal_crash_path(tmp_path, monkeypatch):
    (tmp_path / ".agentkit").mkdir()
    monkeypatch.chdir(tmp_path)

    def boom(ctx, a):
        raise RuntimeError("kaboom " + SECRET)

    real = cli._handlers
    monkeypatch.setattr(cli, "_handlers", lambda: {**real(), "doctor": boom})
    env, code, _ = cli.run_cli(["doctor"])
    assert code == 1 and not env["ok"]
    err = env["error"]
    assert err["code"] == "AK-INTERNAL" and err["run"].startswith("r")
    assert "Traceback" not in json.dumps(env) and SECRET not in json.dumps(env)
    assert "agentkit feedback --bug " + err["run"] in err["next"]
    crash = tmp_path / ".agentkit" / "logs" / f"crash-{err['run']}.log"
    text = crash.read_text()
    assert "Traceback" in text and "kaboom" in text and SECRET not in text
    ev = (tmp_path / ".agentkit" / "logs" / "events.jsonl").read_text()
    assert "AK-INTERNAL" in ev and err["run"] in ev


def test_debug_bundle_redacted(ak, repo):
    (repo / ".agentkit").mkdir()
    (repo / ".agentkit" / "config.toml").write_text(
        f'[models.x]\nbase_url = "http://127.0.0.1:1/v1"\nkey_env = "X_KEY"\napi_key = "{SECRET}"\n')
    (repo / "a.py").write_text("x = 1\n")
    ak.env["X_KEY"] = "plainvalue" + "123456"
    rid = ak("read", "zzz.py", f"--allow-secret-file")["error"]["run"]
    r = ak("debug-bundle", rid)
    assert r["ok"], r
    p = repo / r["data"]["bundle"]
    with tarfile.open(p) as tf:
        names = tf.getnames()
        blob = b"".join(tf.extractfile(m).read() for m in tf.getmembers())
    assert any(n.endswith("manifest.json") for n in names) and any(n.endswith("events.jsonl") for n in names)
    assert SECRET.encode() not in blob and b"plainvalue123456" not in blob
    assert b"X_KEY" in blob and b"agentkit" in blob


@pytest.mark.skipif(os.name != "posix", reason="dispatch mode requires Linux")
def test_task_failure_shows_code_component_step_and_log(ak, repo, fake):
    from test_m2_tasks import project, create, wait

    project(repo, ak, fake)
    fake.add("[DIAG]", [{"call": "read", "args": {"spec": "calc.py"}, "before": [{"http": 401}]}])
    tid = create(ak, "fix add [DIAG]")
    s = wait(ak, tid)
    assert s["code"] == "AK-MODEL-AUTH" and s["at"].startswith("model#") and s["log"] == f"agentkit logs {tid} --errors"
    assert len(json.dumps(s)) < 400
    rep = ak("task", "report", tid)
    assert rep["data"]["error"]["code"] == "AK-MODEL-AUTH" and rep["data"]["error"]["component"] == "model"
    assert len(rep["_raw"]) <= 1200
    lg = ak("logs", tid, "--errors")["data"]
    assert any("AK-MODEL-AUTH" in l for l in lg["lines"])
    fb = ak("feedback", "--bug", tid)["data"]
    assert fb["posted"] is False and "AK-MODEL-AUTH" in fb["title"]


def test_feedback_bug_redacts_and_encodes(ak, repo):
    (repo / "a.py").write_text("x = 1\n")
    rid = ak("edit", "a.py", "--old", f"password={SECRET} & x=1 #frag", "--new", "y")["error"]["run"]
    r = ak("feedback", "--bug", rid)
    d = r["data"]
    assert d["posted"] is False and SECRET not in r["_raw"]
    u = urllib.parse.urlsplit(d["url"])
    assert u.scheme == "https" and u.netloc == "github.com" and u.path == "/tangniutt/agentkit/issues/new"
    q = urllib.parse.parse_qs(u.query)
    assert q["template"] == ["bug_report.yml"] and q["error_code"] == ["AK-TOOL-EDIT-NOMATCH"] and q["run_id"] == [rid]
    assert " " not in d["url"] and "#" not in u.query and "&x=1" not in d["url"]
    assert "ask your human" in d["rules"] and d["search_first"].startswith("https://github.com/tangniutt/agentkit/issues?q=")
    assert len(d["url"]) <= 6000


def test_feedback_idea_url_encoding(ak, repo):
    idea = f"stream output & 'quotes' / 中文 #tag key={SECRET}"
    d = ak("feedback", "--idea", idea)["data"]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(d["url"]).query)
    assert q["template"] == ["feature_request.yml"]
    assert q["proposal"][0].startswith("stream output & 'quotes' / 中文 #tag") and SECRET not in q["proposal"][0]
    long = ak("feedback", "--idea", "x" * 20000)["data"]
    assert len(long["url"]) <= 6000
    assert ak("feedback")["error"]["code"] == "AK-USAGE"


def test_issue_templates_match_feedback_fields():
    t = ROOT / ".github" / "ISSUE_TEMPLATE"
    bug = yaml.safe_load((t / "bug_report.yml").read_text())
    ids = {b.get("id") for b in bug["body"]}
    assert {"version", "command", "error_code", "run_id", "repro", "expected", "actual", "logs", "environment", "filed_by_ai"} <= ids
    feat = yaml.safe_load((t / "feature_request.yml").read_text())
    assert {"problem", "proposal", "alternatives", "version", "filed_by_ai"} <= {b.get("id") for b in feat["body"]}
    cfg = yaml.safe_load((t / "config.yml").read_text())
    assert cfg["blank_issues_enabled"] is False
    for doc in ("llms.txt", "AGENTS.md", "CONTRIBUTING.md"):
        assert "agentkit feedback" in (ROOT / doc).read_text(encoding="utf-8"), doc
