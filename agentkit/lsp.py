# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Minimal stdio LSP client for definitions and references."""
from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname


class LSPError(RuntimeError):
    pass


def _read_message(stream) -> dict:
    headers = {}
    header_bytes = 0
    while True:
        line = stream.readline()
        if not line:
            raise LSPError("language server closed the stream")
        header_bytes += len(line)
        if header_bytes > 8192:
            raise LSPError("language server headers are too large")
        if line in (b"\r\n", b"\n"):
            break
        try:
            name, value = line.decode("ascii").strip().split(":", 1)
        except (UnicodeError, ValueError) as e:
            raise LSPError("invalid language server header") from e
        name = name.lower()
        if name in headers:
            raise LSPError("duplicate language server header")
        headers[name] = value.strip()
        if len(headers) > 32:
            raise LSPError("too many language server headers")
    if "transfer-encoding" in headers:
        raise LSPError("transfer encoding is not supported")
    try:
        length = int(headers.get("content-length", ""))
    except ValueError as e:
        raise LSPError("missing or invalid Content-Length") from e
    if length < 0 or length > 16 * 1024 * 1024:
        raise LSPError("language server message is too large")
    payload = stream.read(length)
    if len(payload) != length:
        raise LSPError("truncated language server message")
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError) as e:
        raise LSPError("language server returned invalid JSON") from e
    if not isinstance(message, dict):
        raise LSPError("language server returned a non-object message")
    return message


def _write_message(stream, message: dict) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload)
    stream.flush()


def _utf16_column(line: str, column: int) -> int:
    return len(line[:column].encode("utf-16-le")) // 2


def _python_column(line: str, column: int) -> int:
    units = line.encode("utf-16-le")
    return len(units[:column * 2].decode("utf-16-le", errors="ignore"))


def _windows_job(proc):
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimitInformation),
                    ("IoCounters", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                  wintypes.LPVOID, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise LSPError(f"cannot create language server job object: {ctypes.get_last_error()}")
    info = ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x2000
    try:
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise LSPError(f"cannot configure language server job object: {ctypes.get_last_error()}")
        if not kernel32.AssignProcessToJobObject(job, proc._handle):
            raise LSPError(f"cannot isolate language server process tree: {ctypes.get_last_error()}")
    except Exception:
        kernel32.CloseHandle(job)
        raise
    return job, kernel32.CloseHandle


def _close_windows_job(close, job) -> None:
    if job:
        close(job)


class LSPClient:
    def __init__(self, command: list[str], root: Path, *, timeout: float = 30, env: Optional[dict] = None):
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise LSPError("LSP command must be a non-empty argv array")
        self.command = list(command)
        self.root = root.resolve()
        self.timeout = max(1.0, float(timeout))
        self.env = dict(os.environ if env is None else env)
        self.proc: Optional[subprocess.Popen] = None
        self.job = None
        self.job_close = None
        self.messages: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.reader: Optional[threading.Thread] = None
        self.next_id = 1
        self.capabilities: dict = {}
        self.diagnostics: dict = {}

    def _send(self, message: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise LSPError("language server is not running")
        try:
            _write_message(self.proc.stdin, message)
        except (BrokenPipeError, OSError) as e:
            raise LSPError(f"language server write failed: {e}") from e

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def _server_request(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "workspace/configuration":
            result = [None for _ in params.get("items") or []]
        elif method == "workspace/workspaceFolders":
            result = [{"uri": self.root.as_uri(), "name": self.root.name}]
        elif method in ("client/registerCapability", "window/workDoneProgress/create"):
            result = None
        else:
            self._send({"jsonrpc": "2.0", "id": message.get("id"),
                        "error": {"code": -32601, "message": f"Unsupported server request: {method}"}})
            return
        self._send({"jsonrpc": "2.0", "id": message.get("id"), "result": result})

    def _read_loop(self) -> None:
        try:
            while not self.stop.is_set() and self.proc is not None and self.proc.stdout is not None:
                self.messages.put(_read_message(self.proc.stdout))
        except Exception as e:
            if not self.stop.is_set():
                self.messages.put(e)

    def request(self, method: str, params: Optional[dict] = None):
        req_id = self.next_id
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._notify("$/cancelRequest", {"id": req_id})
                raise LSPError(f"language server timed out on {method}")
            try:
                response = self.messages.get(timeout=remaining)
            except queue.Empty:
                continue
            if isinstance(response, Exception):
                raise LSPError(str(response)) from response
            if response.get("id") == req_id and "method" not in response:
                if "error" in response:
                    error = response["error"] or {}
                    raise LSPError(str(error.get("message") or error))
                return response.get("result")
            if "method" in response and "id" in response:
                self._server_request(response)
            elif response.get("method") == "textDocument/publishDiagnostics":
                params = response.get("params") or {}
                self.diagnostics[str(params.get("uri") or "")] = params.get("diagnostics") or []

    def start(self) -> "LSPClient":
        executable = self.command[0]
        if shutil.which(executable) is None and not Path(executable).is_file():
            raise LSPError(f"language server executable not found: {executable}")
        self.proc = subprocess.Popen(self.command, cwd=str(self.root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=self.env, start_new_session=os.name == "posix")
        if os.name == "nt":
            try:
                self.job, self.job_close = _windows_job(self.proc)
            except Exception:
                self.proc.kill()
                self.proc.wait(timeout=2)
                for stream in (self.proc.stdin, self.proc.stdout):
                    if stream is not None:
                        stream.close()
                self.proc = None
                raise
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        try:
            self.capabilities = self.request("initialize", {
                "processId": os.getpid(), "rootUri": self.root.as_uri(),
                "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
                "capabilities": {
                    "general": {"positionEncodings": ["utf-16"]},
                    "workspace": {"configuration": True, "workspaceFolders": True},
                    "textDocument": {"definition": {}, "references": {}},
                },
            }) or {}
            self._notify("initialized", {})
            return self
        except Exception:
            self.close()
            raise

    def open_document(self, path: Path, text: str, language: str) -> None:
        self._notify("textDocument/didOpen", {"textDocument": {"uri": path.as_uri(), "languageId": language,
                                                               "version": 1, "text": text}})

    def definition(self, path: Path, line: int, column: int):
        return self.request("textDocument/definition", {"textDocument": {"uri": path.as_uri()},
                                                        "position": {"line": line, "character": _utf16_column(
                                                            path.read_text(encoding="utf-8").splitlines()[line], column)}})

    def references(self, path: Path, line: int, column: int, include_declaration: bool = False):
        return self.request("textDocument/references", {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": line, "character": _utf16_column(
                path.read_text(encoding="utf-8").splitlines()[line], column)},
            "context": {"includeDeclaration": include_declaration},
        })

    def close(self) -> None:
        if self.proc is None:
            return
        if self.reader is not None and self.proc.poll() is None:
            try:
                self.request("shutdown")
                self._notify("exit")
                self.proc.wait(timeout=2)
            except Exception:
                pass
        self.stop.set()
        try:
            if os.name == "posix":
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.proc.wait(timeout=2)
            elif self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
        finally:
            _close_windows_job(self.job_close, self.job)
            self.job = None
            self.job_close = None
        if self.reader is not None:
            self.reader.join(timeout=1)
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream is not None:
                stream.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.close()


def _location(item: dict, root: Path) -> Optional[dict]:
    uri = item.get("uri") or item.get("targetUri")
    if not isinstance(uri, str):
        return None
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    if parsed.netloc.lower() not in ("", "localhost") or parsed.query or parsed.fragment:
        return None
    try:
        path = Path(url2pathname(unquote(parsed.path))).resolve()
        path.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    link_range = item.get("targetSelectionRange") or item.get("range") or {}
    start = link_range.get("start") or {}
    try:
        line = int(start.get("line", 0))
        column = int(start.get("character", 0))
    except (TypeError, ValueError):
        return None
    if line < 0 or column < 0:
        return None
    text = ""
    try:
        source_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[line]
        text = source_line.strip()
        column = _python_column(source_line, column)
    except (OSError, IndexError):
        pass
    return {"path": path.relative_to(root.resolve()).as_posix(), "line": line + 1,
            "column": column + 1, "text": text[:300]}


def normalize_locations(result, root: Path, k: int = 50) -> list:
    if result is None:
        return []
    items = result if isinstance(result, list) else [result]
    out = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        location = _location(item, root)
        if location is None:
            continue
        key = (location["path"], location["line"], location["column"])
        if key not in seen:
            seen.add(key)
            out.append(location)
        if len(out) >= k:
            break
    return out
