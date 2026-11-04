"""Wire-level SOCKS proxy tests for protected fetching."""

from __future__ import annotations

import ipaddress
import select
import socket
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from comourat.fetch import FetchPolicy, FetchPolicyError, SafeHttpClient


class _LoopbackControlPolicy(FetchPolicy):
    def _has_builtin_denial(self, address):
        if address == ipaddress.ip_address("127.0.0.2"):
            return False
        return super()._has_builtin_denial(address)


class _TargetHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.requests_seen.append((self.path, self.headers.get("Host", "")))
        if self.path == "/redirect-blocked":
            self.send_response(302)
            self.send_header("Location", self.server.redirect_target)
            self.send_header("Connection", "close")
            self.end_headers()
            return
        body = b"SOCKS control content"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class _TargetServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        self.connections = 0
        self.requests_seen = []
        super().__init__(address, _TargetHandler)

    def verify_request(self, request, client_address):
        self.connections += 1
        return True


def _relay(client, remote):
    sockets = [client, remote]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 2)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                destination = remote if source is client else client
                destination.sendall(data)
    finally:
        remote.close()


class _SOCKS5Handler(socketserver.StreamRequestHandler):
    def handle(self):
        greeting = self.rfile.read(2)
        if len(greeting) != 2 or greeting[0] != 5:
            self.wfile.write(b"\x05\xff")
            return
        self.rfile.read(greeting[1])
        self.wfile.write(b"\x05\x00")
        self.wfile.flush()
        header = self.rfile.read(4)
        if len(header) != 4 or header[:3] != b"\x05\x01\x00":
            return
        address_type = header[3]
        if address_type == 1:
            host = socket.inet_ntoa(self.rfile.read(4))
        elif address_type == 4:
            host = socket.inet_ntop(socket.AF_INET6, self.rfile.read(16))
        elif address_type == 3:
            length = self.rfile.read(1)[0]
            host = self.rfile.read(length).decode("idna")
        else:
            return
        port = struct.unpack(">H", self.rfile.read(2))[0]
        self.server.requests_seen.append((address_type, host, port))
        answers = self.server.dns.get(host, host)
        if isinstance(answers, str):
            answers = [answers]
        if not answers or any(
            ip not in self.server.allowed_target_ips for ip in answers
        ):
            self.wfile.write(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")
            self.wfile.flush()
            return
        target_ip = answers[0]
        try:
            remote = socket.create_connection((target_ip, port), timeout=2)
        except OSError:
            self.wfile.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
            self.wfile.flush()
            return
        self.wfile.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        self.wfile.flush()
        _relay(self.connection, remote)


class _SOCKS4Handler(socketserver.StreamRequestHandler):
    def handle(self):
        header = self.rfile.read(8)
        if len(header) != 8 or header[:2] != b"\x04\x01":
            return
        port = struct.unpack(">H", header[2:4])[0]
        host = socket.inet_ntoa(header[4:8])
        while self.rfile.read(1) not in (b"\x00", b""):
            pass
        self.server.requests_seen.append((1, host, port))
        if host not in self.server.allowed_target_ips:
            self.wfile.write(b"\x00\x5b\x00\x00\x00\x00\x00\x00")
            self.wfile.flush()
            return
        try:
            remote = socket.create_connection((host, port), timeout=2)
        except OSError:
            self.wfile.write(b"\x00\x5b\x00\x00\x00\x00\x00\x00")
            self.wfile.flush()
            return
        self.wfile.write(b"\x00\x5a" + header[2:8])
        self.wfile.flush()
        _relay(self.connection, remote)


class _SOCKSServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_class):
        self.connections = 0
        self.requests_seen = []
        self.dns = {}
        self.allowed_target_ips = {"127.0.0.2"}
        super().__init__(address, handler_class)

    def verify_request(self, request, client_address):
        self.connections += 1
        return True


@pytest.fixture
def servers():
    running = []

    def start(server_class=_TargetServer, host="127.0.0.1", handler_class=None):
        if handler_class is None:
            server = server_class((host, 0))
        else:
            server = server_class((host, 0), handler_class)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running.append((server, thread))
        return server

    yield start
    for server, thread in running:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _client(proxy, scheme, resolver):
    return SafeHttpClient(
        fetch_policy=_LoopbackControlPolicy(),
        proxy=f"{scheme}://proxy.test:{proxy.server_address[1]}",
        resolver=resolver,
    )


def _resolver(calls, target_ip="127.0.0.2"):
    def resolve(host, port):
        calls.append(host)
        address = "127.0.0.1" if host == "proxy.test" else target_ip
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    return resolve


def test_socks5_uses_comourat_dns_and_sends_validated_ipv4(servers):
    target = servers(_TargetServer, "127.0.0.2")
    proxy = servers(_SOCKSServer, handler_class=_SOCKS5Handler)
    calls = []
    client = _client(proxy, "socks5", _resolver(calls))
    response = client.get(f"http://proxied.test:{target.server_port}/")
    assert response.status_code == 200
    assert response.content == b"SOCKS control content"
    assert calls == ["proxy.test", "proxied.test"]
    assert proxy.requests_seen == [(1, "127.0.0.2", target.server_port)]
    assert target.requests_seen == [("/", f"proxied.test:{target.server_port}")]


def test_socks5h_uses_remote_dns_and_sends_hostname(servers):
    target = servers(_TargetServer, "127.0.0.2")
    proxy = servers(_SOCKSServer, handler_class=_SOCKS5Handler)
    proxy.dns["proxied.test"] = "127.0.0.2"
    calls = []
    client = _client(proxy, "socks5h", _resolver(calls))
    response = client.get(f"http://proxied.test:{target.server_port}/")
    assert response.status_code == 200
    assert calls == ["proxy.test"]
    assert proxy.requests_seen == [(3, "proxied.test", target.server_port)]


def test_socks5h_policy_enforcing_proxy_blocks_remote_target(servers):
    target = servers(_TargetServer, "127.0.0.1")
    proxy = servers(_SOCKSServer, handler_class=_SOCKS5Handler)
    proxy.dns["blocked.test"] = "127.0.0.1"
    calls = []
    client = _client(proxy, "socks5h", _resolver(calls))
    with pytest.raises(requests.RequestException):
        client.get(f"http://blocked.test:{target.server_port}/")
    assert proxy.requests_seen == [(3, "blocked.test", target.server_port)]
    assert target.connections == 0


def test_socks4_uses_comourat_dns_and_sends_validated_ipv4(servers):
    target = servers(_TargetServer, "127.0.0.2")
    proxy = servers(_SOCKSServer, handler_class=_SOCKS4Handler)
    calls = []
    client = _client(proxy, "socks4", _resolver(calls))
    response = client.get(f"http://proxied.test:{target.server_port}/")
    assert response.status_code == 200
    assert calls == ["proxy.test", "proxied.test"]
    assert proxy.requests_seen == [(1, "127.0.0.2", target.server_port)]


def test_socks4_rejects_ipv6_before_proxy_connection(servers):
    proxy = servers(_SOCKSServer, handler_class=_SOCKS4Handler)
    calls = []
    client = _client(proxy, "socks4", _resolver(calls))
    with pytest.raises(FetchPolicyError, match="SOCKS4"):
        client.get("http://[2001:4860:4860::8888]/")
    assert proxy.connections == 0


def test_socks5h_rejects_mixed_remote_dns_answers(servers):
    allowed = servers(_TargetServer, "127.0.0.2")
    prohibited = servers(_TargetServer, "127.0.0.1")
    proxy = servers(_SOCKSServer, handler_class=_SOCKS5Handler)
    proxy.dns["mixed.test"] = ["127.0.0.2", "127.0.0.1"]
    calls = []
    client = _client(proxy, "socks5h", _resolver(calls))
    with pytest.raises(requests.RequestException):
        client.get(f"http://mixed.test:{allowed.server_port}/")
    assert allowed.connections == 0
    assert prohibited.connections == 0


def test_socks5h_redirect_uses_proxy_policy_on_every_target(servers):
    allowed = servers(_TargetServer, "127.0.0.2")
    prohibited = servers(_TargetServer, "127.0.0.1")
    allowed.redirect_target = f"http://blocked.test:{prohibited.server_port}/"
    proxy = servers(_SOCKSServer, handler_class=_SOCKS5Handler)
    proxy.dns.update({"proxied.test": "127.0.0.2", "blocked.test": "127.0.0.1"})
    calls = []
    client = _client(proxy, "socks5h", _resolver(calls))
    with pytest.raises(requests.RequestException):
        client.get(f"http://proxied.test:{allowed.server_port}/redirect-blocked")
    assert proxy.requests_seen == [
        (3, "proxied.test", allowed.server_port),
        (3, "blocked.test", prohibited.server_port),
    ]
    assert prohibited.connections == 0


def test_socks5_redirect_to_literal_denial_makes_no_second_proxy_request(servers):
    allowed = servers(_TargetServer, "127.0.0.2")
    prohibited = servers(_TargetServer, "127.0.0.1")
    allowed.redirect_target = f"http://127.0.0.1:{prohibited.server_port}/"
    proxy = servers(_SOCKSServer, handler_class=_SOCKS5Handler)
    calls = []
    client = _client(proxy, "socks5", _resolver(calls))
    with pytest.raises(FetchPolicyError):
        client.get(f"http://proxied.test:{allowed.server_port}/redirect-blocked")
    assert proxy.requests_seen == [(1, "127.0.0.2", allowed.server_port)]
    assert prohibited.connections == 0
