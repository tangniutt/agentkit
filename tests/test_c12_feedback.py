# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""C-12 AI feedback loop: --improve drafts with redacted evidence, dedupe keywords + search URL,
at most one feedback hint on runs with a signal (none on clean runs), improvement form, issue-triage workflow."""
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

import pytest
import yaml

from agentkit import diag
from agentkit.events import EventLog
from agentkit.redact import redact_text
from conftest import git

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / ".github" / "ISSUE_TEMPLATE"
WF = ROOT / ".github" / "workflows" / "issue-triage.yml"
SECRET = "sk-" + "proj" + "Z" * 30  # looks like a provider key; assembled at runtime
PY = sys.executable


def _q(url):
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}


def _form(name):
    return yaml.safe_load((TPL / name).read_text())


def _edit_errors(ak, repo, n=3):
    (repo / "a.py").write_text("x = 1\n")
    rids = [ak("edit", "a.py", "--old", "PRIVATE_CODE_MARKER()", "--new", "y")["error"]["run"] for _ in range(n)]
    return rids[-1] if n == 1 else rids


# ---------------------------------------------------------------- drafting


def test_improve_draft_fields_evidence_and_redaction(ak, repo):
    rids = _edit_errors(ak, repo)
    rid = rids[0]
    r = ak("feedback", "--improve", f"edit: show the nearest matching line token={SECRET}", "--from", rids[0],
           "--from", rids[1], "--from", rids[2],
           "--observed", "edit NOMATCH three times in a row", "--gain", "fewer edit retries (-1 round per miss)",
           "--cost", "small, stdlib difflib", "--prior-art", "aider fuzzy edit", "--license", "Apache-2.0",
           "--verify-by", "eval task with a whitespace-drifted old text")
    assert r["ok"], r
    d = r["data"]
    assert d["posted"] is False and SECRET not in r["_raw"] and "PRIVATE_CODE_MARKER" not in r["_raw"]
    assert d["title"].startswith("[improve] edit: show the nearest matching line")
    assert "missing" not in d
    q = _q(d["url"])
    assert urllib.parse.urlsplit(d["url"]).path == "/tangniutt/agentkit/issues/new"
    assert q["template"] == "improvement.yml" and q["labels"] == "ai-feedback"
    lines = q["evidence"].splitlines()
    assert len(lines) == 3 and all(r_ in q["evidence"] for r_ in rids)
    assert all("cmd=edit codes AK-TOOL-EDIT-NOMATCHx1" in l for l in lines)
    assert q["license"] == "Apache-2.0" and q["filed_via"].startswith("agentkit feedback")
    ids = {b.get("id") for b in _form("improvement.yml")["body"]}
    assert set(q) - {"template", "title", "labels"} <= ids
    required = {b["id"] for b in _form("improvement.yml")["body"] if (b.get("validations") or {}).get("required")}
    assert required <= set(q)
    assert "ask your human" in d["rules"] and "+1" in d["rules"]
    assert len(d["url"]) <= 6000 and len(r["_raw"]) <= 8000


def test_improve_input_json_stdin_file_and_flag_override(ak, repo):
    rid = _edit_errors(ak, repo, 1)
    obj = {"proposal": "repo_map: cache symbols by file sha", "cost_risk": "a small cache file", "from": [rid],
           "gain": "from JSON"}
    d = ak("feedback", "--improve", "--input", "-", "--gain", "from flag", stdin=json.dumps(obj))["data"]
    q = _q(d["url"])
    assert q["proposal"] == obj["proposal"] and q["cost_risk"] == "a small cache file" and q["gain"] == "from flag"
    assert rid in q["evidence"] and set(d["missing"]) == {"observed", "verify"}
    (repo / "p.json").write_text(json.dumps({"proposal": "x" * 50000, "verify": "a test"}))
    d2 = ak("feedback", "--improve", "--input", "p.json")
    assert d2["ok"] and len(d2["data"]["url"]) <= 6000 and "from" in d2["data"]["missing"]
    assert ak("feedback", "--improve", "--input", "-", stdin="[1,2]")["error"]["code"] == "AK-USAGE"
    assert ak("feedback", "--improve", "--input", "-", stdin="{not json")["error"]["code"] == "AK-USAGE"
    assert ak("feedback", "--improve")["error"]["code"] == "AK-USAGE"  # no proposal
    assert ak("feedback", "--improve", "x", "--from", "r0000000000zzzz")["error"]["code"] == "AK-LOGS-UNKNOWN"


def test_placeholders_listed_as_missing(ak, repo):
    d = ak("feedback", "--improve", "one idea")["data"]
    assert d["missing"] == ["observed", "gain", "cost", "verify", "from"]
    q = _q(d["url"])
    assert q["observed"].startswith("<") and q["evidence"].startswith("<no run/task ids")


def test_dedupe_keywords_and_search_urls(ak, repo):
    kws = diag.keywords("AK-TOOL-EDIT-NOMATCH", "edit should show the nearest matching line", n=5)
    assert kws[0] == "AK-TOOL-EDIT-NOMATCH" and "nearest" in kws and "should" not in kws
    assert diag.keywords("用 ripgrep 的 json 输出 加速 搜索")[:2] == ["ripgrep", "json"]
    assert all("redacted" not in k for k in diag.keywords(f"token={SECRET} streaming output"))
    u = diag.search_url(["AK-TOOL-EDIT-NOMATCH", "nearest", "matching", "line"])
    assert _q(u)["q"] == "is:issue nearest matching"
    assert _q(diag.search_url(["AK-X", "word"], by_code=True))["q"] == "is:issue AK-X"
    rid = _edit_errors(ak, repo, 1)
    b = ak("feedback", "--bug", rid)["data"]
    assert _q(b["search_first"])["q"] == "is:issue AK-TOOL-EDIT-NOMATCH" and b["keywords"][0] == "AK-TOOL-EDIT-NOMATCH"
    assert _q(b["url"])["labels"] == "ai-feedback" and _q(b["url"])["filed_via"].startswith("agentkit feedback")
    i = ak("feedback", "--idea", "stream task events as json lines")["data"]
    assert "stream" in _q(i["search_first"])["q"] and _q(i["url"])["labels"] == "ai-feedback"


# ---------------------------------------------------------------- signals / hints


def _task(root, tid, events=(), **t):
    d = root / ".agentkit" / "tasks" / tid
    d.mkdir(parents=True, exist_ok=True)
    rep = t.pop("report", {})
    (d / "task.json").write_text(json.dumps({"id": tid, "status": "done", **t}))
    (d / "report.json").write_text(json.dumps(rep))
    ev = EventLog(root, tid, "worker")
    for comp, code in events:
        ev.emit("tool", component=comp, error_code=code)


@pytest.mark.parametrize("case,expect", [
    ({"error_code": "AK-WORKER-CRASH", "status": "failed"}, ("bug", "AK-WORKER-CRASH")),
    ({"events": [("cli", "AK-INTERNAL")]}, ("bug", "AK-INTERNAL")),
    ({"error_code": "AK-WORKER-MALFORMED", "status": "failed"}, ("improve", "AK-WORKER-MALFORMED")),
    ({"events": [("tool:search", "AK-PATH")] * 3}, ("improve", "AK-PATH x3")),
    ({"events": [("tool:edit", "AK-TOOL-EDIT-NOMATCH"), ("tool:edit", "AK-TOOL-EDIT-MULTI"),
                 ("tool:patch", "AK-TOOL-EDIT-STALE")]}, ("improve", "edit failures x3")),
    ({"stopped": "stuck", "status": "failed"}, ("improve", "stuck")),
    ({"status": "failed", "component": "tool:run", "worker_status": "failed"}, ("improve", "failed at tool:run")),
    ({"verdict": "needs_review", "report": {"verdict": "needs_review", "suspicious": [{"flag": "hardcoded_return_suspect"}]}},
     ("improve", "needs_review hardcoded_return_suspect")),
])
def test_signal_triggers(tmp_path, case, expect):
    case = dict(case)
    _task(tmp_path, "tabc123", case.pop("events", ()), **case)
    kind, why = diag.signal(tmp_path, "tabc123")
    assert kind == expect[0] and why.startswith(expect[1]), why
    h = diag.feedback_hint(tmp_path, "tabc123")
    assert h.startswith("agentkit feedback --bug tabc123" if kind == "bug" else "agentkit feedback --improve --from tabc123")
    assert "ask your human" in h and len(h) < 170


@pytest.mark.parametrize("case", [
    {"verdict": "pass"},
    {"verdict": "fail", "status": "done"},
    {"events": [("tool:edit", "AK-TOOL-EDIT-NOMATCH"), ("tool:read", "AK-PATH")]},  # a couple of ordinary misses
    {"status": "paused", "component": "model", "error_code": "AK-MODEL-429"},
    {"status": "stopped", "stopped": "budget", "error_code": "AK-BUDGET"},
    {"verdict": "fail", "report": {"verdict": "fail", "suspicious": [{"flag": "tests_modified"}]}},
])
def test_no_signal_on_ordinary_runs(tmp_path, case):
    case = dict(case)
    _task(tmp_path, "tdef456", case.pop("events", ()), **case)
    assert diag.signal(tmp_path, "tdef456") is None and diag.feedback_hint(tmp_path, "tdef456") is None


@pytest.mark.skipif(os.name != "posix", reason="dispatch mode requires Linux")
def test_clean_task_has_no_hint_and_stuck_task_has_one(ak, repo, fake):
    from test_m2_tasks import FIX, create, project, wait

    project(repo, ak, fake)
    fake.add("[C12OK]", FIX)
    ok = create(ak, "fix add [C12OK]")
    w = ak("task", "wait", ok, "--timeout", "90")
    assert w["data"]["verdict"] == "pass" and w["next"] is None and "feedback" not in w["_raw"]
    rep = ak("task", "report", ok)
    assert rep["next"] is None and "feedback" not in rep["_raw"]

    fake.add("[C12ERR]", [{"call": "edit", "args": {"path": "calc.py", "old": "nonexistent text", "new": "x"}}])
    bad = create(ak, "fix add [C12ERR]")
    wait(ak, bad)
    r = ak("task", "report", bad)
    assert r["next"].startswith(f"agentkit feedback --improve --from {bad}") and "AK-TOOL-EDIT-NOMATCH x3" in r["next"]
    assert r["_raw"].count("agentkit feedback") == 1 and len(r["_raw"]) <= 1200
    # the hint's command works and its evidence carries the task's numbers, not its code
    d = ak("feedback", "--improve", "edit: suggest the nearest match", "--from", bad)["data"]
    ev = _q(d["url"])["evidence"]
    assert bad in ev and "AK-TOOL-EDIT-NOMATCHx3" in ev and "rounds=" in ev and "tokens=" in ev
    assert "nonexistent text" not in ev


def test_verify_hint_only_for_suspicious_pass(ak, repo):
    from test_verify_memory import CHECK, _project

    _project(repo)
    clean = ak("verify", "--check", CHECK)
    assert clean["data"]["verdict"] == "pass" and clean["next"] is None
    ak("edit", "tests/test_calc.py", "--old", "assert add(2, 3) == 5", "--new", "assert True")
    r = ak("verify", "--check", CHECK)
    assert r["data"]["suspicious"] and r["next"].startswith("agentkit feedback --improve --from r")
    assert "if a false alarm" in r["next"] and len(r["_raw"]) <= 1400


# ---------------------------------------------------------------- forms + workflow


def test_issue_forms_carry_ai_fields():
    imp = _form("improvement.yml")
    assert set(imp["labels"]) == {"ai-feedback", "needs-eval"}
    ids = [b.get("id") for b in imp["body"] if b.get("id")]
    assert ids == ["observed", "evidence", "proposal", "gain", "cost_risk", "prior_art", "license", "verify", "version",
                   "filed_via", "filed_by_ai"]
    for name in ("bug_report.yml", "feature_request.yml"):
        assert "filed_via" in {b.get("id") for b in _form(name)["body"]}, name
    for doc in ("llms.txt", "AGENTS.md", "CONTRIBUTING.md", "docs/agent-manual.md"):
        assert "feedback --improve" in (ROOT / doc).read_text(encoding="utf-8"), doc
    t = (ROOT / "docs" / "triage.md").read_text(encoding="utf-8")
    for word in ("needs-eval", "accepted", "declined", "duplicate", "needs-info", "C-xx", "许可证"):
        assert word in t


def _triage():
    wf = yaml.safe_load(WF.read_text())
    run = wf["jobs"]["triage"]["steps"][0]["run"]
    lines = run.splitlines()
    assert lines[0].startswith("python3 - <<'PY'") and lines[-1] == "PY"
    ns = {"__name__": "triage"}
    exec(compile("\n".join(lines[1:-1]), str(WF), "exec"), ns)
    return wf, ns


def _render(form, values):
    """Issue body as GitHub renders a submitted form: ### <label> then the value or _No response_."""
    out = []
    for b in _form(form)["body"]:
        if b["type"] in ("markdown", "checkboxes"):
            continue
        out.append(f"### {b['attributes']['label']}\n\n{values.get(b['id']) or '_No response_'}")
    return "\n\n".join(out)


def test_workflow_is_safe_and_parses():
    wf, ns = _triage()
    assert wf["permissions"] == {"issues": "write"}
    on = wf.get("on", wf.get(True))
    assert on["issues"]["types"] == ["opened", "edited", "labeled"]
    text = WF.read_text()
    assert "secrets." not in text and "actions/checkout" not in text and "${{ github.event" not in text
    assert {"check", "secret_hits", "sections", "main"} <= set(ns)


def test_workflow_checks_drafts_end_to_end(ak, repo):
    _, ns = _triage()
    rid = _edit_errors(ak, repo, 1)
    draft = ak("feedback", "--improve", "idea", "--from", rid)["data"]
    q = _q(draft["url"])
    res = ns["check"](q["title"], _render("improvement.yml", q), set())
    assert res["ai"] and res["kind"] == "improvement"
    assert res["missing"] == ["Observed", "Expected gain", "Cost / risk", "How to verify"] and not res["secrets"]
    full = ak("feedback", "--improve", "idea", "--from", rid, "--observed", "o", "--gain", "g", "--cost", "c",
              "--verify-by", "v")["data"]
    q = _q(full["url"])
    assert ns["check"](q["title"], _render("improvement.yml", q), set()) == \
        {"ai": True, "kind": "improvement", "missing": [], "secrets": []}
    # a human pasting a key into the rendered issue is caught, by line, without echoing it
    leaked = _render("improvement.yml", {**q, "observed": f"my key is {SECRET}"})
    res = ns["check"](q["title"], leaked, set())
    assert res["secrets"] and res["secrets"][0][1] == "provider token"
    msg = ns["comment_text"](res)
    assert SECRET not in msg and "REDACTED" in msg and ns["MARK"] in msg
    # bug drafts keep their placeholders -> asked to fill them; a human-filed issue is left alone
    b = _q(ak("feedback", "--bug", rid)["data"]["url"])
    res = ns["check"](b["title"], _render("bug_report.yml", b), set())
    assert res["ai"] and res["kind"] == "bug" and res["missing"] == ["Minimal reproduction", "Expected"]
    human = _render("feature_request.yml", {"problem": "p", "proposal": "q"})
    assert ns["check"]("[idea] x", human, set()) == {"ai": False, "kind": "feature", "missing": [], "secrets": []}
    assert ns["check"]("[idea] x", human, {"ai-feedback"})["ai"] is True


@pytest.mark.parametrize("sample", [
    "sk-" + "a" * 24, "ghp_" + "b" * 30, "github_pat_" + "c" * 30, "AKIA" + "D" * 16, "AIza" + "e" * 35,
    "Authorization: Bearer " + "f" * 20, "https://user:hunter2pass@example.com/x", "api_key = '" + "g" * 16 + "'",
    "password: " + "h" * 14, "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
])
def test_workflow_secret_scan_mirrors_redaction(sample):
    _, ns = _triage()
    assert redact_text(sample) != sample  # agentkit masks it ...
    assert ns["secret_hits"](sample)  # ... and the issue scan flags it
    assert not ns["secret_hits"](redact_text(sample).replace("[REDACTED PRIVATE KEY]", "[REDACTED]"))


def test_workflow_ignores_ordinary_text():
    _, ns = _triage()
    assert ns["secret_hits"]("token = get_token()\npassword: <set via env>\nsee AK-TOOL-EDIT-NOMATCH x3") == []
