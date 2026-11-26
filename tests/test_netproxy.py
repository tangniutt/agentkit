# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Domain allowlist proxy and relay contract tests."""
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentkit import netproxy
from agentkit.netproxy import host_allowed, normalize_domain, parse_client_hello, start_relay


def test_domain_matching_is_exact_or_subdomain_and_ip_is_exact():
    domains = [normalize_domain("API.Example.com."), normalize_domain("127.0.0.1")]
    assert host_allowed("api.example.com", domains)
    assert host_allowed("v1.api.example.com", domains)
    assert not host_allowed("badexample.com", domains)
    assert not host_allowed("example.com", domains)
    assert host_allowed("127.0.0.1", domains)
    assert not host_allowed("127.0.0.2", domains)


@pytest.mark.parametrize("value", ["", "https://example.com", "example.com/path", "*.example.com", "bad..example"])
def test_invalid_allowlist_entries_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_domain(value)


def _client_hello(hostname: str) -> bytes:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=hostname)
    with pytest.raises((ssl.SSLWantReadError, ssl.SSLWantWriteError)):
        tls.do_handshake()
    return outgoing.read()


def test_tls_client_hello_sni_is_parsed():
    raw = _client_hello("api.example.com")
    forwarded, sni = parse_client_hello(raw)
    assert forwarded == raw and sni == "api.example.com"


def test_connect_rejects_sni_that_differs_from_allowed_host(monkeypatch):
    client, accepted = socket.socketpair()
    upstream, _ = socket.socketpair()
    calls, tunneled = [], []
    monkeypatch.setattr(netproxy, "_upstream", lambda host, port: calls.append((host, port)) or upstream)
    monkeypatch.setattr(netproxy, "_tunnel", lambda _left, _right, initial=b"": tunneled.append(initial))
    thread = threading.Thread(target=netproxy.handle_client,
                              args=(accepted, ["api.example.com"], threading.BoundedSemaphore(4)), daemon=True)
    thread.start()
    try:
        client.settimeout(3)
        client.sendall(b"CONNECT api.example.com:443 HTTP/1.1\r\nHost: api.example.com:443\r\n\r\n" +
                       _client_hello("other.example.com"))
        assert client.recv(4096).startswith(b"HTTP/1.1 200")
        thread.join(timeout=3)
        assert calls == [] and tunneled == []
    finally:
        client.close()
        accepted.close()
        upstream.close()


def test_connect_forwards_client_hello_only_after_sni_match(monkeypatch):
    client, accepted = socket.socketpair()
    upstream, _ = socket.socketpair()
    calls, tunneled = [], []
    monkeypatch.setattr(netproxy, "_upstream", lambda host, port: calls.append((host, port)) or upstream)
    monkeypatch.setattr(netproxy, "_tunnel", lambda _left, _right, initial=b"": tunneled.append(initial))
    thread = threading.Thread(target=netproxy.handle_client,
                              args=(accepted, ["api.example.com"], threading.BoundedSemaphore(4)), daemon=True)
    thread.start()
    hello = _client_hello("api.example.com")
    try:
        client.settimeout(3)
        client.sendall(b"CONNECT api.example.com:443 HTTP/1.1\r\nHost: api.example.com:443\r\n\r\n" + hello)
        assert client.recv(4096).startswith(b"HTTP/1.1 200")
        thread.join(timeout=3)
        assert calls == [("api.example.com", 443)] and tunneled == [hello]
    finally:
        client.close()
        accepted.close()
        upstream.close()


def test_domain_rebinding_to_private_address_is_rejected(monkeypatch):
    monkeypatch.setattr(netproxy.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(OSError, match="globally routable"):
        netproxy._upstream("api.example.com", 443)


def test_plain_http_does_not_forward_a_second_pipelined_request(monkeypatch):
    client, accepted = socket.socketpair()
    upstream, upstream_peer = socket.socketpair()
    monkeypatch.setattr(netproxy, "_upstream", lambda _host, _port: upstream)
    monkeypatch.setattr(netproxy, "_pump_response", lambda _upstream, conn: conn.sendall(b"ok"))
    thread = threading.Thread(target=netproxy.handle_client,
                              args=(accepted, ["127.0.0.1"], threading.BoundedSemaphore(4)), daemon=True)
    thread.start()
    try:
        client.settimeout(3)
        client.sendall(
            b"GET http://127.0.0.1/first HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
            b"GET http://example.invalid/second HTTP/1.1\r\nHost: example.invalid\r\n\r\n")
        upstream_peer.settimeout(3)
        forwarded = upstream_peer.recv(65536)
        thread.join(timeout=3)
        assert b"/first" in forwarded and b"/second" not in forwarded
    finally:
        client.close()
        accepted.close()
        upstream.close()
        upstream_peer.close()


def test_start_relay_waits_for_a_complete_port_file(tmp_path, monkeypatch):
    port_file = tmp_path / "relay.port"
    original_read_text = Path.read_text
    reads = iter(("", "12345\n"))

    def read_text(path, *args, **kwargs):
        return next(reads) if path == port_file else original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    proc = SimpleNamespace(poll=lambda: None, kill=lambda: None, wait=lambda timeout: None)
    monkeypatch.setattr(netproxy.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    started, url = start_relay(tmp_path / "missing.sock", port_file, tmp_path / "relay.log")
    assert started is proc and url == "http://127.0.0.1:12345"


@pytest.mark.skipif(os.name != "posix", reason="Unix sockets and relay processes require POSIX")
def test_relay_does_not_inherit_secret_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AK_TEST_SECRET", "must-not-leak")
    proc, _url = start_relay(tmp_path / "missing.sock", tmp_path / "relay.port", tmp_path / "relay.log")
    try:
        environ = Path(f"/proc/{proc.pid}/environ").read_bytes()
        assert b"AK_TEST_SECRET" not in environ and b"must-not-leak" not in environ
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="Unix sockets and relay processes require POSIX")
def test_host_proxy_and_relay_allow_only_configured_hosts():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"proxied-ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    directory = Path(tempfile.mkdtemp(prefix="aknet-test-"))
    socket_path = directory / "proxy.sock"
    pid_file = directory / "worker.pid"
    ready_file = directory / "ready"
    host = subprocess.Popen([sys.executable, "-m", "agentkit.netproxy", "host", "--socket", str(socket_path),
                             "--pid-file", str(pid_file), "--ready-file", str(ready_file),
                             "--allow", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    relay = None
    try:
        deadline = time.monotonic() + 5
        while not ready_file.exists() and host.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_file.exists()
        pid_file.write_text(str(os.getpid()) + "\n", encoding="ascii")
        relay, proxy_url = start_relay(socket_path, directory / "relay.port", directory / "relay.log")
        port = int(proxy_url.rsplit(":", 1)[1])

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", f"http://127.0.0.1:{server.server_port}/ok", headers={"Host": f"127.0.0.1:{server.server_port}"})
        response = conn.getresponse()
        assert response.status == 200 and response.read() == b"proxied-ok"
        conn.close()

        payload = json.dumps({"model": "fake", "messages": [{"role": "user", "content": "ping"}]}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", f"http://127.0.0.1:{server.server_port}/v1/chat/completions", body=payload,
                     headers={"Host": f"127.0.0.1:{server.server_port}", "Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == 200 and response.read() == payload
        conn.close()

        denied = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        denied.request("GET", "http://example.invalid/secret", headers={"Host": "example.invalid"})
        with pytest.raises((http.client.HTTPException, OSError)):
            response = denied.getresponse()
            assert response.status != 200
        denied.close()
    finally:
        if relay is not None:
            relay.terminate()
            relay.wait(timeout=5)
        host.terminate()
        host.wait(timeout=5)
        server.shutdown()
        server.server_close()
