# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Domain allowlist proxy for bubblewrap network namespaces."""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

_MAX_HEADER = 65536
_MAX_CLIENT_HELLO = 65536
_MAX_BODY = 64 * 1024 * 1024


def normalize_domain(value: str) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    if not text or len(text) > 253 or "://" in text or "/" in text or "*" in text:
        raise ValueError("sandbox network allowlist entries must be bare hostnames or IP addresses")
    try:
        return ipaddress.ip_address(text).compressed
    except ValueError:
        pass
    try:
        text = text.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise ValueError("sandbox network allowlist contains an invalid hostname") from e
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", text):
        raise ValueError("sandbox network allowlist contains an invalid hostname")
    return text


def host_allowed(host: str, domains: list[str]) -> bool:
    try:
        host = normalize_domain(host)
    except ValueError:
        return False
    allowed = {normalize_domain(item) for item in domains}
    if host in allowed:
        return True
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return any(host.endswith("." + domain) for domain in allowed if domain != "localhost")


def _read_exact(conn: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ValueError("connection closed")
        data.extend(chunk)
    return bytes(data)


def parse_client_hello(data: bytes) -> tuple[bytes, Optional[str]]:
    if len(data) < 5 or data[0] != 22:
        raise ValueError("not a TLS handshake")
    record_length = struct.unpack("!H", data[3:5])[0]
    if record_length > _MAX_CLIENT_HELLO:
        raise ValueError("TLS record too large")
    payload = bytearray()
    pos = 0
    while pos + 5 <= len(data):
        if data[pos] != 22:
            raise ValueError("unexpected TLS record")
        length = struct.unpack("!H", data[pos + 3:pos + 5])[0]
        payload.extend(data[pos + 5:pos + 5 + length])
        pos += 5 + length
    payload = bytes(payload)
    raw = data[:pos]
    if len(payload) < 4 or payload[0] != 1:
        raise ValueError("not a ClientHello")
    hello_length = int.from_bytes(payload[1:4], "big")
    if len(payload) < 4 + hello_length:
        raise ValueError("incomplete ClientHello")
    hello = payload[4:4 + hello_length]
    if len(hello) < 35:
        raise ValueError("short ClientHello")
    pos = 34
    session_len = hello[pos]
    pos += 1 + session_len
    cipher_len = struct.unpack("!H", hello[pos:pos + 2])[0]
    pos += 2 + cipher_len
    compression_len = hello[pos]
    pos += 1 + compression_len
    if pos + 2 > len(hello):
        raise ValueError("missing ClientHello extensions")
    extensions_len = struct.unpack("!H", hello[pos:pos + 2])[0]
    pos += 2
    end = min(len(hello), pos + extensions_len)
    while pos + 4 <= end:
        kind, length = struct.unpack("!HH", hello[pos:pos + 4])
        pos += 4
        value = hello[pos:pos + length]
        pos += length
        if kind != 0 or len(value) < 2:
            continue
        list_len = struct.unpack("!H", value[:2])[0]
        cursor = 2
        while cursor + 3 <= min(len(value), 2 + list_len):
            name_type = value[cursor]
            name_len = struct.unpack("!H", value[cursor + 1:cursor + 3])[0]
            cursor += 3
            name = value[cursor:cursor + name_len]
            cursor += name_len
            if name_type == 0:
                return raw, name.decode("ascii").lower().rstrip(".")
    return raw, None


def read_client_hello(conn: socket.socket, initial: bytes = b"") -> tuple[bytes, Optional[str]]:
    conn.settimeout(10)
    data = bytearray(initial)

    def need(size: int):
        while len(data) < size:
            chunk = conn.recv(min(8192, size - len(data)))
            if not chunk:
                raise ValueError("connection closed")
            data.extend(chunk)

    need(5)
    if data[0] != 22:
        raise ValueError("not a TLS handshake")
    length = struct.unpack("!H", data[3:5])[0]
    if length > _MAX_CLIENT_HELLO:
        raise ValueError("TLS record too large")
    need(5 + length)
    payload = bytearray(data[5:5 + length])
    while len(payload) < 4 or int.from_bytes(payload[1:4], "big") + 4 > len(payload):
        need(len(data) + 5)
        if data[-5] != 22:
            raise ValueError("unexpected TLS record")
        length = struct.unpack("!H", data[-3:-1])[0]
        if len(data) + length > _MAX_CLIENT_HELLO:
            raise ValueError("ClientHello too large")
        need(len(data) + length)
        payload = bytearray()
        offset = 0
        while offset + 5 <= len(data):
            record_length = struct.unpack("!H", data[offset + 3:offset + 5])[0]
            payload.extend(data[offset + 5:offset + 5 + record_length])
            offset += 5 + record_length
    return parse_client_hello(bytes(data))


def _read_headers(conn: socket.socket) -> tuple[bytes, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(8192)
        if not chunk:
            raise ValueError("connection closed before headers")
        data.extend(chunk)
        if len(data) > _MAX_HEADER:
            raise ValueError("HTTP headers too large")
    head, remainder = bytes(data).split(b"\r\n\r\n", 1)
    return head + b"\r\n\r\n", remainder


def _tunnel(left: socket.socket, right: socket.socket, initial: bytes = b"") -> None:
    if initial:
        right.sendall(initial)
    readers = [left, right]
    last_activity = time.monotonic()
    try:
        while readers:
            ready, _, _ = select.select(readers, [], [], 30)
            if not ready:
                if time.monotonic() - last_activity > 300:
                    break
                continue
            for source in ready:
                try:
                    chunk = source.recv(65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    readers.remove(source)
                    try:
                        other = right if source is left else left
                        other.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                last_activity = time.monotonic()
                target = right if source is left else left
                if os.environ.get("AGENTKIT_NETPROXY_DEBUG"):
                    print(f"netproxy: tunnel {len(chunk)} bytes end={chunk[-4:].hex()}", file=sys.stderr, flush=True)
                target.sendall(chunk)
    except OSError:
        pass
    finally:
        left.close()
        right.close()


def _forward_body(conn: socket.socket, upstream: socket.socket, initial: bytes, length: int) -> None:
    if length < 0 or length > _MAX_BODY:
        raise ValueError("request body too large")
    if initial:
        upstream.sendall(initial[:length])
    remaining = length - min(len(initial), length)
    while remaining:
        chunk = conn.recv(min(65536, remaining))
        if not chunk:
            raise ValueError("connection closed before request body")
        upstream.sendall(chunk)
        remaining -= len(chunk)


def _pump_response(upstream: socket.socket, conn: socket.socket) -> None:
    upstream.settimeout(300)
    while True:
        chunk = upstream.recv(65536)
        if not chunk:
            break
        conn.sendall(chunk)


def _deny(conn: socket.socket) -> None:
    try:
        conn.close()
    except OSError:
        pass


def _upstream(host: str, port: int) -> socket.socket:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return socket.create_connection((host, port), timeout=15)
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    candidates = []
    for family, socktype, proto, _canonical, address in infos:
        try:
            ip = ipaddress.ip_address(address[0])
        except ValueError:
            continue
        if host == "localhost" and ip.is_loopback:
            candidates.append((family, socktype, proto, address))
        elif ip.is_global:
            candidates.append((family, socktype, proto, address))
    if not candidates:
        raise OSError("resolved address is not globally routable")
    family, socktype, proto, address = candidates[0]
    conn = socket.socket(family, socktype, proto)
    conn.settimeout(15)
    try:
        conn.connect(address)
    except OSError:
        conn.close()
        raise
    return conn


def handle_client(conn: socket.socket, domains: list[str], slots: threading.BoundedSemaphore) -> None:
    upstream = None
    try:
        with slots:
            conn.settimeout(15)
            headers, remainder = _read_headers(conn)
            lines = headers.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            debug = bool(os.environ.get("AGENTKIT_NETPROXY_DEBUG"))
            if debug:
                content_length = next((line.split(":", 1)[1].strip() for line in lines[1:]
                                       if line.lower().startswith("content-length:")), "0")
                print(f"netproxy: headers remainder={len(remainder)} content-length={content_length}",
                      file=sys.stderr, flush=True)
            if debug:
                print(f"netproxy: request {method}", file=sys.stderr, flush=True)
            if method.upper() == "CONNECT":
                host, port_text = target.rsplit(":", 1)
                host = host.strip("[]").lower().rstrip(".")
                port = int(port_text)
                if port != 443 or not host_allowed(host, domains):
                    _deny(conn)
                    return
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                hello, sni = read_client_hello(conn, remainder)
                try:
                    expected = normalize_domain(host)
                    actual = normalize_domain(sni) if sni else None
                except ValueError:
                    _deny(conn)
                    return
                if actual != expected:
                    _deny(conn)
                    return
                upstream = _upstream(host, port)
                upstream.settimeout(None)
                conn.settimeout(None)
                _tunnel(conn, upstream, hello)
                return
            parsed = urlsplit(target)
            if parsed.scheme.lower() == "https":
                _deny(conn)
                return
            header_hosts = [line.split(":", 1)[1].strip() for line in lines[1:]
                            if line.lower().startswith("host:")]
            if len(header_hosts) > 1:
                _deny(conn)
                return
            header_host = header_hosts[0] if header_hosts else ""
            try:
                header_name = urlsplit("//" + header_host).hostname if header_host else None
                host = parsed.hostname or header_name
            except ValueError:
                _deny(conn)
                return
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if not host or not host_allowed(host, domains):
                _deny(conn)
                return
            if header_name:
                try:
                    same = normalize_domain(header_name) == normalize_domain(host)
                except ValueError:
                    same = False
                if not same:
                    _deny(conn)
                    return
            if any(line.lower().startswith("transfer-encoding:") for line in lines[1:]):
                _deny(conn)
                return
            content_lengths = [line.split(":", 1)[1].strip() for line in lines[1:]
                               if line.lower().startswith("content-length:")]
            if len(content_lengths) > 1:
                _deny(conn)
                return
            if content_lengths:
                try:
                    body_length = int(content_lengths[0])
                except ValueError:
                    _deny(conn)
                    return
            else:
                body_length = 0
                if method.upper() not in ("GET", "HEAD"):
                    _deny(conn)
                    return
            if body_length < 0 or body_length > _MAX_BODY:
                _deny(conn)
                return
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            request_line = f"{method} {path} {version}\r\n".encode("iso-8859-1")
            kept = []
            for line in lines[1:]:
                if not line or line.lower().startswith(("proxy-connection:", "proxy-authorization:", "connection:")):
                    continue
                kept.append(line)
            kept.append("Connection: close")
            forwarded = request_line + ("\r\n".join(kept) + "\r\n\r\n").encode("iso-8859-1")
            upstream = _upstream(host, port)
            if debug:
                print(f"netproxy: upstream {host}:{port}", file=sys.stderr, flush=True)
            upstream.sendall(forwarded)
            _forward_body(conn, upstream, remainder, body_length)
            conn.settimeout(None)
            _pump_response(upstream, conn)
    except (OSError, ValueError, IndexError, struct.error) as e:
        if os.environ.get("AGENTKIT_NETPROXY_DEBUG"):
            print(f"netproxy: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if upstream is not None:
            try:
                upstream.close()
            except OSError:
                pass
        _deny(conn)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run_host(args: argparse.Namespace) -> int:
    domains = [normalize_domain(item) for item in args.allow]
    socket_path = Path(args.socket)
    pid_file = Path(args.pid_file)
    ready_file = Path(args.ready_file)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (socket_path, pid_file, ready_file):
        path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.listen(64)
    server.settimeout(0.5)
    ready_file.write_text("ready\n", encoding="ascii")

    def cleanup():
        server.close()
        for path in (socket_path, pid_file, ready_file):
            path.unlink(missing_ok=True)
        try:
            socket_path.parent.rmdir()
        except OSError:
            pass

    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        cleanup()
        return 2
    slots = threading.BoundedSemaphore(64)
    stopping = threading.Event()

    def stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping.is_set() and _pid_alive(pid):
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle_client, args=(conn, domains, slots), daemon=True).start()
    finally:
        cleanup()
    return 0


def run_relay(args: argparse.Namespace) -> int:
    port_file = Path(args.port_file)
    port_file.unlink(missing_ok=True)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    server.settimeout(0.5)
    temporary = port_file.with_name(f".{port_file.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(str(server.getsockname()[1]) + "\n", encoding="ascii")
        os.replace(temporary, port_file)
    finally:
        temporary.unlink(missing_ok=True)
    stopping = threading.Event()

    def stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    slots = threading.BoundedSemaphore(64)

    def relay(conn: socket.socket) -> None:
        upstream = None
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            if os.environ.get("AGENTKIT_NETPROXY_DEBUG"):
                print("netproxy: relay accepted", file=sys.stderr, flush=True)
            upstream.connect(args.socket)
            if os.environ.get("AGENTKIT_NETPROXY_DEBUG"):
                print("netproxy: relay connected", file=sys.stderr, flush=True)
            _tunnel(conn, upstream)
        except OSError as e:
            if os.environ.get("AGENTKIT_NETPROXY_DEBUG"):
                print(f"netproxy: relay {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            conn.close()
            if upstream is not None:
                upstream.close()
        finally:
            slots.release()

    try:
        while not stopping.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            if not slots.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=relay, args=(conn,), daemon=True).start()
    finally:
        server.close()
        port_file.unlink(missing_ok=True)
    return 0


def start_relay(socket_path: Path, port_file: Path, log_path: Path) -> tuple[subprocess.Popen, str]:
    port_file.unlink(missing_ok=True)
    relay_env = {"PATH": os.environ.get("PATH", "")}
    if os.environ.get("AGENTKIT_NETPROXY_DEBUG"):
        relay_env["AGENTKIT_NETPROXY_DEBUG"] = "1"
    with open(log_path, "ab") as log:
        proc = subprocess.Popen([sys.executable, "-I", "-B", "-u", "-m", "agentkit.netproxy", "relay", "--socket", str(socket_path),
                                 "--port-file", str(port_file)], stdout=log, stderr=log, env=relay_env,
                                start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            port = int(port_file.read_text(encoding="ascii").strip())
            if 1 <= port <= 65535:
                return proc, f"http://127.0.0.1:{port}"
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)
    detail = log_path.read_text(encoding="utf-8", errors="replace")[-500:] if log_path.exists() else ""
    raise RuntimeError("sandbox network relay failed to start" + (f": {detail}" if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentKit sandbox network proxy")
    sub = parser.add_subparsers(dest="mode", required=True)
    host = sub.add_parser("host")
    host.add_argument("--socket", required=True)
    host.add_argument("--pid-file", required=True)
    host.add_argument("--ready-file", required=True)
    host.add_argument("--allow", action="append", default=[])
    relay = sub.add_parser("relay")
    relay.add_argument("--socket", required=True)
    relay.add_argument("--port-file", required=True)
    args = parser.parse_args()
    return run_host(args) if args.mode == "host" else run_relay(args)


if __name__ == "__main__":
    raise SystemExit(main())
