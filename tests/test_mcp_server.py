# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""MCP stdio transport and core tool dispatch."""
import json
import queue
import subprocess
import sys
import threading

from agentkit.mcp_server import _argv, _state, _watch, handle


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


def test_backend_arguments_map_to_cli():
    assert _argv("agentkit_task_create", {"goal": "x", "checks": ["pytest"], "backend": "codex"}) == [
        "task", "create", "x", "--check", "pytest", "--backend", "codex"]
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
    names = {t["name"] for t in out[1]["result"]["tools"]}
    assert {"agentkit_doctor", "agentkit_read", "agentkit_task_diff", "agentkit_task_answer",
            "agentkit_task_handoff", "agentkit_goal_create", "agentkit_goal_run", "agentkit_goal_answer",
            "agentkit_code_search", "agentkit_code_defs", "agentkit_code_index"} <= names
    assert all(t["inputSchema"]["type"] == "object" and "outputSchema" in t for t in out[1]["result"]["tools"])
    schemas = {t["name"]: t["inputSchema"] for t in out[1]["result"]["tools"]}
    assert schemas["agentkit_task_create"]["properties"]["backend"]["enum"] == ["builtin", "codex", "opencode", "qoder", "cursor"]
    assert schemas["agentkit_task_handoff"]["properties"]["backend"]["enum"] == ["builtin", "codex", "opencode", "qoder", "cursor"]


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
    assert "agentkit_doctor" in out[1]["result"]["contents"][0]["text"]
    assert out[2]["error"]["code"] == -32602
    assert out[3]["error"]["code"] == -32601


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
