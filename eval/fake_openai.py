# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""A scripted, local OpenAI-compatible /chat/completions server for tests and offline eval.

No network, no real key. A *script* is chosen per request by a marker string that appears
in the conversation (e.g. the eval task id inside the goal). The step served is the number
of assistant messages already in the request history, so retries of the same request get
the same step. Steps:

  {"call": "read", "args": {...}}                 one tool call
  {"calls": [{"call": ..., "args": ...}, ...]}    several tool calls in one turn
  {"call": "read", "raw_args": "{bad json"}       malformed arguments string
  {"text": "..."}                                 plain text, no tool call
  {"usage": {"prompt_tokens": N, "completion_tokens": M}}   (on any step) override usage
  {"sig": "..."}                                  attach Gemini-style extra_content.thought_signature
  {"before": [{"http": 429, "retry_after": 1}, {"sleep": 3}, {"http": 401}]}
      served, in order, on the first requests for this step before the step itself.

Past the end of the script the last step repeats (useful for stuck / budget tests).
Every request (headers + body) is recorded in `server.requests`.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


class FakeOpenAI:
    def __init__(self, scripts: Optional[dict] = None, default: Optional[list] = None):
        self.scripts = dict(scripts or {})
        self.default = default
        self.requests: list = []
        self.served_before: dict = {}
        self.lock = threading.Lock()
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.sig_echoed: list = []

    # ------------------------------------------------------------ lifecycle
    def start(self) -> "FakeOpenAI":
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8", "replace")
                try:
                    body = json.loads(raw)
                except ValueError:
                    return self._send(400, {"error": {"message": "bad json"}})
                with srv.lock:
                    srv.requests.append({"path": self.path, "headers": dict(self.headers), "body": body, "raw": raw})
                code, payload, headers = srv.respond(body)
                self._send(code, payload, headers)

            def _send(self, code, payload, headers=None):
                data = json.dumps(payload).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    for k, v in (headers or {}).items():
                        self.send_header(k, str(v))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()

    # ------------------------------------------------------------ scripting
    def add(self, marker: str, steps: list):
        with self.lock:
            self.scripts[marker] = steps

    def requests_for(self, marker: str) -> list:
        return [r for r in self.requests if marker in json.dumps(r["body"].get("messages", []))]

    def _pick(self, messages: list):
        text = json.dumps(messages)
        for marker in sorted(self.scripts, key=len, reverse=True):
            if marker in text:
                return marker, self.scripts[marker]
        return None, self.default

    def respond(self, body: dict):
        messages = body.get("messages") or []
        marker, steps = self._pick(messages)
        if not steps:
            return 200, _completion(body, {"role": "assistant", "content": "ok"}), {}
        idx = sum(1 for m in messages if m.get("role") == "assistant")
        # did the client echo back thought signatures?
        for m in messages:
            if m.get("role") == "assistant" and "extra_content" in json.dumps(m):
                self.sig_echoed.append(marker)
                break
        step = steps[min(idx, len(steps) - 1)]
        before = step.get("before") or []
        key = (marker, idx)
        with self.lock:
            k = self.served_before.get(key, 0)
            if k < len(before):
                self.served_before[key] = k + 1
                pre = before[k]
            else:
                pre = None
        if pre is not None:
            if "sleep" in pre:
                time.sleep(pre["sleep"])
                if "http" not in pre:
                    return 200, _completion(body, {"role": "assistant", "content": "late"}), {}
            code = pre.get("http", 500)
            headers = {}
            if pre.get("retry_after") is not None:
                headers["Retry-After"] = pre["retry_after"]
            msg = pre.get("message") or {401: "invalid api key", 429: "rate limited"}.get(code, "error")
            payload = {"error": {"message": msg, "code": code}}
            if pre.get("retry_delay"):
                payload["error"]["details"] = [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                "retryDelay": f"{pre['retry_delay']}s"}]
            return code, payload, headers
        msg = _message(step, idx)
        return 200, _completion(body, msg, step.get("usage")), {}


def _message(step: dict, idx: int) -> dict:
    if "text" in step and "call" not in step and "calls" not in step:
        return {"role": "assistant", "content": step["text"]}
    calls = step.get("calls") or [step]
    tcs = []
    for j, c in enumerate(calls):
        args = c["raw_args"] if "raw_args" in c else json.dumps(c.get("args") or {})
        tc = {"id": f"call_{idx}_{j}", "type": "function", "function": {"name": c["call"], "arguments": args}}
        if step.get("sig"):
            tc["extra_content"] = {"google": {"thought_signature": step["sig"]}}
        tcs.append(tc)
    msg = {"role": "assistant", "content": step.get("text"), "tool_calls": tcs}
    if step.get("sig"):
        msg["extra_content"] = {"google": {"thought_signature": step["sig"]}}
    return msg


def _completion(body: dict, msg: dict, usage: Optional[dict] = None) -> dict:
    pt = len(json.dumps(body.get("messages") or [])) // 4 + len(json.dumps(body.get("tools") or [])) // 4
    ct = max(1, len(json.dumps(msg)) // 4)
    u = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}
    if usage:
        u.update(usage)
        u["total_tokens"] = u["prompt_tokens"] + u["completion_tokens"]
    return {"id": "fake-1", "object": "chat.completion", "model": body.get("model"),
            "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
            "usage": u}


if __name__ == "__main__":  # python eval/fake_openai.py script.json  -> serves until Ctrl-C
    import sys

    scripts = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else {}
    s = FakeOpenAI(scripts).start()
    print(s.url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        s.stop()
