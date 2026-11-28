# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""MCP stdio transport and core tool dispatch."""
import json
import queue
import subprocess
import sys
import threading

from agentkit.mcp_server import _argv, _normalize_tool_arguments, _schema_issues, _state, _watch, handle


def _run(repo, messages):
    payload = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
    p = subprocess.run([sys.executable, "-m", "agentkit", "--root", str(repo), "mcp"],
                       input=payload, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert p.returncode == 0, p.stderr
    return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]


def _meta(version="2025-11-25"):
    return {"_meta": {"io.modelcontextprotocol/protocolVersion": version,
                      "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
                      "io.modelcontextprotocol/clientCapabilities": {}}}


def _task(repo, tid: str, status: str = "queued"):
    path = repo / ".agentkit" / "tasks" / tid / "task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": tid, "status": status, "created": 1, "rounds": 0, "tokens": 0}), encoding="utf-8")
    return path


def test_tool_argument_aliases_normalize_without_breaking_cli_mapping():
    read, issues = _normalize_tool_arguments("agentkit_read", {"path": "calc.py"})
    assert not issues and _argv("agentkit_read", read) == ["read", "calc.py"]
    report, issues = _normalize_tool_arguments("agentkit_task_report", {"ids": ["t123abc"]})
    assert not issues and _argv("agentkit_task_report", report) == ["task", "report", "t123abc"]
    status, issues = _normalize_tool_arguments("agentkit_task_status", {"id": "t123abc"})
    assert not issues and _argv("agentkit_task_status", status) == ["task", "status", "t123abc"]
    assert _schema_issues({"type": "integer"}, True) == ["arguments: expected integer"]


def test_mcp_invalid_arguments_are_not_unknown_tool(repo):
    out = _run(repo, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "agentkit_task_report", "arguments": {"ids": []}, **_meta()}},
    ])
    error = out[-1]["error"]
    assert error["code"] == -32602 and "Invalid arguments" in error["message"]
    assert "Unknown tool" not in error["message"]
    assert error["data"]["tool"] == "agentkit_task_report" and error["data"]["issues"]


def test_mcp_read_path_alias_resolves_from_root(repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    out = _run(repo, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "agentkit_read", "arguments": {"path": "calc.py"}, **_meta()}},
    ])
    result = out[-1]["result"]
    assert result["isError"] is False
    assert "return a + b" in result["structuredContent"]["data"]["content"]


def test_backend_arguments_map_to_cli():
    assert _argv("agentkit_credential_show", {"target": "t123abc"}) == ["credential", "show", "t123abc"]
    assert _argv("agentkit_credential_revoke", {"target": "cb_1"}) == ["credential", "revoke", "cb_1"]
    assert _argv("agentkit_task_create", {"goal": "x", "checks": ["pytest"], "backend": "codex"}) == [
        "task", "create", "x", "--check", "pytest", "--backend", "codex"]
    assert _argv("agentkit_task_create", {"goal": "x", "checks": ["true"], "fresh": True}) == [
        "task", "create", "x", "--check", "true", "--fresh"]
    assert _argv("agentkit_task_report", {"id": "t123abc", "details": True}) == [
        "task", "report", "t123abc", "--details"]
    assert _argv("agentkit_verify", {"checks": ["true"], "receipt": True}) == [
        "verify", "--check", "true", "--receipt"]
    assert _argv("agentkit_task_handoff", {"id": "t123abc", "backend": "codex"}) == [
        "task", "handoff", "t123abc", "--backend", "codex"]
    assert _argv("agentkit_task_create", {"goal": "x", "checks": ["pytest"], "backend": "opencode"}) == [
        "task", "create", "x", "--check", "pytest", "--backend", "opencode"]
    assert _argv("agentkit_task_handoff", {"id": "t123abc", "backend": "qoder"}) == [
        "task", "handoff", "t123abc", "--backend", "qoder"]
    assert _argv("agentkit_code_refs", {"symbol": "add", "path": "calc.py", "line": 1, "column": 1,
                                        "backend": "lsp", "include_declaration": True}) == [
        "code", "refs", "add", "-k", "5", "--backend", "lsp", "--path", "calc.py",
        "--line", "1", "--column", "1", "--include-declaration"]


def test_mcp_legacy_initialize_and_current_tools(repo):
    out = _run(repo, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": _meta()},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": _meta()},
    ])
    assert len(out) == 2
    assert out[0]["result"]["protocolVersion"] == "2025-11-25"
    tools = out[1]["result"]["tools"]
    assert len(tools) == 35
    names = {t["name"] for t in tools}
    assert {"agentkit_doctor", "agentkit_read", "agentkit_task_diff", "agentkit_task_answer",
            "agentkit_credential_show", "agentkit_credential_revoke", "agentkit_route",
            "agentkit_task_handoff", "agentkit_goal_create", "agentkit_goal_run", "agentkit_goal_answer",
            "agentkit_code_search", "agentkit_code_defs", "agentkit_code_index"} <= names
    assert all(t["inputSchema"]["type"] == "object" and "outputSchema" in t for t in out[1]["result"]["tools"])
    schemas = {t["name"]: t["inputSchema"] for t in out[1]["result"]["tools"]}
    assert schemas["agentkit_task_create"]["properties"]["backend"]["enum"] == ["builtin", "codex", "opencode", "qoder", "cursor"]
    assert schemas["agentkit_task_handoff"]["properties"]["backend"]["enum"] == ["builtin", "codex", "opencode", "qoder", "cursor"]
    assert schemas["agentkit_credential_show"]["required"] == ["target"]
    assert schemas["agentkit_credential_revoke"]["required"] == ["target"]


def test_mcp_tool_call_resource_and_errors(repo):
    out = _run(repo, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {**_meta(), "name": "agentkit_doctor", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "resources/read",
         "params": {**_meta(), "uri": "agentkit://capabilities"}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {**_meta(), "name": "missing", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "no/such", "params": _meta()},
    ])
    assert out[0]["result"]["resultType"] == "complete"
    assert "ok" in out[0]["result"]["structuredContent"]
    capabilities = json.loads(out[1]["result"]["contents"][0]["text"])
    assert "agentkit_doctor" in capabilities["tools"]
    assert set(capabilities["backends"]) == {"builtin", "codex", "opencode", "qoder", "cursor"}
    assert capabilities["backends"]["cursor"]["usage_reporting"] is False
    assert capabilities["backend_variants"]["cursor_cli"]["usage_reporting"] is False
    assert capabilities["backend_variants"]["cursor_sdk"]["usage_reporting"] is True
    assert capabilities["sandbox"]["capabilities"]["provider"] == "bubblewrap"
    assert capabilities["sandbox"]["capabilities"]["pause"] is False
    assert out[2]["error"]["code"] == -32602
    assert out[3]["error"]["code"] == -32601


def test_mcp_credential_show_and_revoke(repo):
    from agentkit import credentials, tasks

    grant = credentials.issue_grant(repo, provider="openai", backend="builtin", auth_kind="api_key",
                                    ref_kind="env", ref="OPENAI_API_KEY", now=1.0)
    binding = credentials.create_binding(repo, grant_id=grant["grant_id"], task_id="t123abc",
                                         run_id="t123abc", backend="builtin", now=2.0)
    tasks.save(repo, {"id": "t123abc", "status": "queued", "credential_grant_id": grant["grant_id"],
                      "credential_binding_id": binding["binding_id"]})
    state = _state()
    out = []
    for request_id, name, target in ((1, "agentkit_credential_show", "t123abc"),
                                     (2, "agentkit_credential_revoke", binding["binding_id"]),
                                     (3, "agentkit_credential_show", binding["binding_id"])):
        out.append(handle(repo, {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                                 "params": {**_meta(), "name": name, "arguments": {"target": target}}}, state))
    assert out[0]["result"]["structuredContent"]["data"]["binding_active"] is True
    assert out[1]["result"]["structuredContent"]["ok"] is True
    assert out[2]["result"]["structuredContent"]["data"]["active"] is False


def test_mcp_route_uses_measured_profiles(repo):
    profile = {"model_id": "provider/model-a", "model_version": "v1", "status": "active",
               "capabilities": {"code_edit": 0.9},
               "evidence": {"sample_count": 10, "verified_pass_rate": 0.9, "wilson_lower_bound": 0.8,
                            "average_task_cost": 0.02, "expected_cost_per_verified_success": 0.02},
               "evaluated_at": "2026-11-28T00:00:00Z", "eval_config_sha256": "a" * 64}
    arguments = {"task_id": "t123abc", "complexity": 1, "blast_radius": 1,
                 "required_capabilities": ["code_edit"], "profiles": [profile]}
    out = handle(repo, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {**_meta(), "name": "agentkit_route", "arguments": arguments}}, _state())
    data = out["result"]["structuredContent"]
    assert data["ok"] is True and data["data"]["model_id"] == "provider/model-a"
    arguments["complexity"] = 4
    out = handle(repo, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {**_meta(), "name": "agentkit_route", "arguments": arguments}}, _state())
    assert out["error"]["code"] == -32602 and "Invalid arguments" in out["error"]["message"]


def test_mcp_tool_failure_is_result_error(repo):
    out = _run(repo, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {**_meta(), "name": "agentkit_read", "arguments": {"spec": "missing.py"}}}])
    result = out[0]["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "AK-PATH"


def test_mcp_resource_list_paginates_dynamic_tasks(repo):
    for index in range(55):
        _task(repo, f"t{index:06x}")
    state = _state()
    first = handle(repo, {"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": _meta()}, state)
    assert len(first["result"]["resources"]) == 50
    cursor = first["result"]["nextCursor"]
    second = handle(repo, {"jsonrpc": "2.0", "id": 2, "method": "resources/list",
                           "params": {**_meta(), "cursor": cursor}}, state)
    assert len(second["result"]["resources"]) == 9 and "nextCursor" not in second["result"]
    invalid = handle(repo, {"jsonrpc": "2.0", "id": 3, "method": "resources/list",
                            "params": {**_meta(), "cursor": "nope"}}, state)
    assert invalid["error"]["code"] == -32602


def test_mcp_resource_capabilities_are_version_aware(repo):
    old = _run(repo, [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "1"}}}])[0]
    current = _run(repo, [{"jsonrpc": "2.0", "id": 2, "method": "initialize",
                           "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "1"}}}])[0]
    unsupported = _run(repo, [{"jsonrpc": "2.0", "id": 3, "method": "initialize",
                               "params": {"protocolVersion": "2026-07-28", "capabilities": {},
                                          "clientInfo": {"name": "test", "version": "1"}}}])[0]
    assert old["result"]["capabilities"]["resources"] == {"subscribe": True, "listChanged": True}
    assert current["result"]["capabilities"]["resources"] == {"subscribe": True, "listChanged": True}
    assert unsupported["error"]["code"] == -32022


def test_mcp_2025_resource_subscription_and_task_read(repo):
    _task(repo, "t123abc", "paused")
    out = _run(repo, [
        {"jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
         "params": {**_meta("2025-11-25"), "uri": "agentkit://task/t123abc"}},
        {"jsonrpc": "2.0", "id": 2, "method": "resources/read",
         "params": {**_meta("2025-11-25"), "uri": "agentkit://task/t123abc"}},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/unsubscribe",
         "params": {**_meta("2025-11-25"), "uri": "agentkit://task/t123abc"}},
    ])
    assert out[0]["result"] == {"_meta": out[0]["result"]["_meta"]}
    data = json.loads(out[1]["result"]["contents"][0]["text"])
    assert data["id"] == "t123abc" and data["status"] == "paused"
    assert "result" in out[2]


def test_mcp_2026_is_explicitly_unsupported(repo):
    out = _run(repo, [{"jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
                       "params": {**_meta("2026-07-28"), "uri": "agentkit://tasks"}}])[0]
    assert out["error"]["code"] == -32022


def test_mcp_watcher_emits_resource_update(repo):
    path = _task(repo, "t123abc")
    state = _state()
    state["initialized"] = True
    state["subscriptions"] = {"agentkit://tasks", "agentkit://task/t123abc"}
    notified = threading.Event()
    messages = []

    def write(message):
        messages.append(message)
        notified.set()

    thread = threading.Thread(target=_watch, args=(repo, state, write), daemon=True)
    thread.start()
    try:
        assert state["ready"].wait(2)
        path.write_text(json.dumps({"id": "t123abc", "status": "running", "created": 1, "rounds": 1, "tokens": 5}),
                        encoding="utf-8")
        assert notified.wait(3)
        updated = [message for message in messages if message["method"] == "notifications/resources/updated"]
        assert {message["params"]["uri"] for message in updated} == {"agentkit://tasks"}
    finally:
        state["stop"].set()
        thread.join(timeout=2)


def test_mcp_watcher_emits_list_changed_for_new_task(repo):
    state = _state()
    state["initialized"] = True
    notified = threading.Event()
    messages = []

    def write(message):
        messages.append(message)
        notified.set()

    thread = threading.Thread(target=_watch, args=(repo, state, write), daemon=True)
    thread.start()
    try:
        assert state["ready"].wait(2)
        _task(repo, "t123abc")
        assert notified.wait(3)
        assert any(message["method"] == "notifications/resources/list_changed" for message in messages)
    finally:
        state["stop"].set()
        thread.join(timeout=2)


def test_mcp_rejects_malformed_params_without_crashing(repo):
    out = _run(repo, [
        {"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": "bad"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": _meta()},
    ])
    assert out[0]["error"]["code"] == -32602
    assert out[1]["result"] == {"_meta": out[1]["result"]["_meta"]}


def test_mcp_cursor_expires_when_resource_snapshot_changes(repo):
    for index in range(55):
        _task(repo, f"t{index:06x}")
    state = _state()
    first = handle(repo, {"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": _meta()}, state)
    cursor = first["result"]["nextCursor"]
    _task(repo, "tffffff")
    second = handle(repo, {"jsonrpc": "2.0", "id": 2, "method": "resources/list",
                           "params": {**_meta(), "cursor": cursor}}, state)
    assert second["error"]["code"] == -32602


def test_mcp_stdio_process_emits_task_update(repo):
    path = _task(repo, "t123abc")
    proc = subprocess.Popen([sys.executable, "-m", "agentkit", "--root", str(repo), "mcp"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    lines = queue.Queue()

    def reader():
        for line in proc.stdout:
            lines.put(json.loads(line))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": _meta("2025-11-25")},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/subscribe",
             "params": {**_meta("2025-11-25"), "uri": "agentkit://task/t123abc"}},
        ]
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        assert lines.get(timeout=5)["id"] == 1
        assert lines.get(timeout=5)["id"] == 2
        path.write_text(json.dumps({"id": "t123abc", "status": "running", "created": 1, "rounds": 1, "tokens": 5}),
                        encoding="utf-8")
        update = lines.get(timeout=5)
        assert update["method"] == "notifications/resources/updated"
        assert update["params"]["uri"] == "agentkit://task/t123abc"
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr.read())
        thread.join(timeout=2)
