# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Minimal LSP client protocol, lifecycle and location normalization."""
import io
import json
import os
import sys
from pathlib import Path

import pytest

from agentkit.lsp import LSPClient, LSPError, _read_message, normalize_locations


def _server(tmp_path: Path, *, hang: bool = False) -> Path:
    path = tmp_path / "fake_lsp.py"
    path.write_text(f'''import json, sys, time
from pathlib import Path

def read():
    headers = {{}}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\\r\\n", b"\\n"):
            break
        name, value = line.decode("ascii").strip().split(":", 1)
        headers[name.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))

def send(message):
    data = json.dumps(message, separators=(",", ":")).encode()
    sys.stdout.buffer.write(f"Content-Length: {{len(data)}}\\r\\n\\r\\n".encode() + data)
    sys.stdout.buffer.flush()

hang = {hang!r}
while True:
    msg = read()
    if msg is None:
        break
    method = msg.get("method")
    with open("lsp-requests.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(msg) + "\\n")
    if method == "initialize":
        if hang:
            time.sleep(5)
        send({{"jsonrpc":"2.0","id":msg["id"],"result":{{"capabilities":{{"definitionProvider":True,"referencesProvider":True}}}}}})
    elif method == "initialized":
        pass
    elif method == "textDocument/didOpen":
        send({{"jsonrpc":"2.0","method":"textDocument/publishDiagnostics","params":{{"uri":msg["params"]["textDocument"]["uri"],"diagnostics":[]}}}})
    elif method == "textDocument/definition":
        uri = msg["params"]["textDocument"]["uri"]
        send({{"jsonrpc":"2.0","id":msg["id"],"result":{{"uri":uri,"range":{{"start":{{"line":0,"character":0}},"end":{{"line":0,"character":3}}}}}}}})
    elif method == "textDocument/references":
        uri = msg["params"]["textDocument"]["uri"]
        send({{"jsonrpc":"2.0","id":msg["id"],"result":[{{"uri":uri,"range":{{"start":{{"line":1,"character":0}},"end":{{"line":1,"character":3}}}}}}]}})
    elif method == "workspace/configuration":
        send({{"jsonrpc":"2.0","id":msg["id"],"result":[None for _ in msg["params"]["items"]]}})
    elif method == "shutdown":
        send({{"jsonrpc":"2.0","id":msg["id"],"result":None}})
    elif method == "exit":
        break
    elif "id" in msg:
        send({{"jsonrpc":"2.0","id":msg["id"],"result":None}})
''', encoding="utf-8")
    return path


@pytest.mark.parametrize(("headers", "message"), [
    (b"Content-Length: 2\r\ncontent-length: 2\r\n", "duplicate"),
    (b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n", "transfer encoding"),
    (b"".join(b"X-" + str(index).encode() + b":" + b"a" * 500 + b"\r\n" for index in range(20))
     + b"Content-Length: 2\r\n", "too large"),
])
def test_lsp_rejects_ambiguous_or_unbounded_headers(headers, message):
    with pytest.raises(LSPError, match=message):
        _read_message(io.BytesIO(headers + b"\r\n{}"))


def test_lsp_client_lifecycle_definition_references_and_utf16(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "calc.py"
    source.write_text("def add(a, b):\n    return add(a, b)\n", encoding="utf-8")
    server = _server(tmp_path)
    with LSPClient([sys.executable, str(server)], root, timeout=5) as client:
        client.open_document(source, source.read_text(encoding="utf-8"), "python")
        definition = client.definition(source, 0, 0)
        references = client.references(source, 0, 0)
    assert definition["range"]["start"] == {"line": 0, "character": 0}
    assert references[0]["range"]["start"]["line"] == 1
    requests = [json.loads(line) for line in (root / "lsp-requests.jsonl").read_text(encoding="utf-8").splitlines()]
    assert requests[0]["method"] == "initialize"
    assert requests[-2]["method"] == "shutdown" and requests[-1]["method"] == "exit"
    reference_request = next(item for item in requests if item["method"] == "textDocument/references")
    assert reference_request["params"]["context"] == {"includeDeclaration": False}


def test_lsp_client_uses_utf16_columns(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "calc.py"
    source.write_text("value = '😀'; add(value)\n", encoding="utf-8")
    server = _server(tmp_path)
    with LSPClient([sys.executable, str(server)], root, timeout=5) as client:
        client.open_document(source, source.read_text(encoding="utf-8"), "python")
        client.definition(source, 0, 11)
    requests = [json.loads(line) for line in (root / "lsp-requests.jsonl").read_text(encoding="utf-8").splitlines()]
    request = next(item for item in requests if item["method"] == "textDocument/definition")
    assert request["params"]["position"]["character"] == 12


def test_lsp_timeout_kills_server(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    server = _server(tmp_path, hang=True)
    client = LSPClient([sys.executable, str(server)], root, timeout=1)
    with pytest.raises(LSPError, match="timed out"):
        client.start()
    assert client.proc.poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object process-tree cleanup")
def test_lsp_timeout_kills_windows_process_tree(tmp_path):
    import ctypes
    from ctypes import wintypes

    root = tmp_path / "repo"
    root.mkdir()
    server = tmp_path / "tree_lsp.py"
    server.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "Path('child.pid').write_text(str(child.pid), encoding='ascii')\n"
        "time.sleep(30)\n", encoding="utf-8")
    client = LSPClient([sys.executable, str(server)], root, timeout=1)
    with pytest.raises(LSPError, match="timed out"):
        client.start()
    child_pid = int((root / "child.pid").read_text(encoding="ascii"))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x00100000, False, child_pid)
    if handle:
        try:
            assert kernel32.WaitForSingleObject(handle, 2000) == 0
        finally:
            kernel32.CloseHandle(handle)
    else:
        assert ctypes.get_last_error() == 87


def test_lsp_missing_executable_is_structured(tmp_path):
    with pytest.raises(LSPError, match="not found"):
        LSPClient(["definitely-missing-lsp"], tmp_path).start()


def test_normalize_locations_accepts_links_and_rejects_external_paths(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    inside = root / "calc.py"
    inside.write_text("def add():\n    pass\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("secret\n", encoding="utf-8")
    result = [
        {"targetUri": inside.as_uri(), "targetRange": {"start": {"line": 0, "character": 0}},
         "targetSelectionRange": {"start": {"line": 0, "character": 4}}},
        {"uri": outside.as_uri(), "range": {"start": {"line": 0, "character": 0}}},
        {"uri": "https://example.com/code.py", "range": {"start": {"line": 0, "character": 0}}},
        {"uri": inside.as_uri().replace("file:///", "file://remote/", 1),
         "range": {"start": {"line": 0, "character": 0}}},
    ]
    normalized = normalize_locations(result, root)
    assert normalized == [{"path": "calc.py", "line": 1, "column": 5, "text": "def add():"}]
