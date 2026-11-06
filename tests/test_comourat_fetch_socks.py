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

from comourat.enrichment.page_fetch import ArticleFetchError
from comourat.fetch import FetchPolicy, FetchPolicyError, SafeHttpClient
from comourat.models import ArticleResult


class _LoopbackControlPolicy(FetchPolicy):
    def _has_builtin_denial(self, address):
        if address == ipaddress.ip_address("127.0.0.2"):
            return False
        return super()._has_builtin_denial(address)


class _TargetHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.requests_seen.append((self.path, self.headers.get("Host", "")))
        self.server.headers_seen.append(dict(self.headers))
        if self.path == "/redirect-final":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        if self.path == "/redirect-blocked":
            self.send_response(302)
            self.send_header("Location", self.server.redirect_target)
            self.send_header("Connection", "close")
            self.end_headers()
            return
        if self.path == "/article":
            body = b"""<html><body><article><h1>SOCKS article</h1><p>Allowed SOCKS article content has enough text for extraction and validates the public entry point.</p></article></body></html>"""
        elif self.path == "/search":
            body = b'<html><body><a class="result-link" href="https://example.com">SOCKS result</a></body></html>'
        else:
            body = b"SOCKS control content"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *args):
        return


class _TargetServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        self.connections = 0
        self.requests_seen = []
        self.headers_seen = []
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

    def start(
        server_class=_TargetServer,
        host="127.0.0.1",
        handler_class=None,
        tls_context=None,
    ):
        if handler_class is None:
            server = server_class((host, 0))
        else:
            server = server_class((host, 0), handler_class)
        if tls_context is not None:
            server.socket = tls_context.wrap_socket(server.socket, server_side=True)
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


def test_authenticated_socks_redirect_never_exposes_proxy_header_to_origin(servers):
    target = servers(_TargetServer, "127.0.0.2")
    proxy = servers(_SOCKSServer, handler_class=_SOCKS5Handler)
    proxy.dns["proxied.test"] = "127.0.0.2"
    calls = []
    session = requests.Session()
    session.headers["Proxy-Authorization"] = "Basic secret"
    client = SafeHttpClient(
        session,
        fetch_policy=_LoopbackControlPolicy(),
        proxy=f"socks5h://user:secret@proxy.test:{proxy.server_address[1]}",
        resolver=_resolver(calls),
    )
    response = client.get(f"http://proxied.test:{target.server_port}/redirect-final")
    assert response.status_code == 200
    assert [path for path, _ in target.requests_seen] == [
        "/redirect-final",
        "/final",
    ]
    for headers in target.headers_seen:
        assert "Proxy-Authorization" not in headers
        assert "secret" not in str(headers)


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_https_through_socks_preserves_sni_and_certificate(
    servers, tmp_path, tls_server_context, scheme
):
    tls_context, ca_path = tls_server_context(tmp_path, "proxied.test")
    sni_names = []
    tls_context.set_servername_callback(
        lambda sock, name, context: sni_names.append(name)
    )
    target = servers(
        _TargetServer,
        "127.0.0.2",
        tls_context=tls_context,
    )
    proxy = servers(_SOCKSServer, handler_class=_SOCKS5Handler)
    proxy.dns["proxied.test"] = "127.0.0.2"
    calls = []
    session = requests.Session()
    session.verify = ca_path
    client = SafeHttpClient(
        session,
        fetch_policy=_LoopbackControlPolicy(),
        proxy=f"{scheme}://proxy.test:{proxy.server_address[1]}",
        resolver=_resolver(calls),
    )
    response = client.get(f"https://proxied.test:{target.server_port}/")
    assert response.status_code == 200
    assert response.content == b"SOCKS control content"
    assert sni_names == ["proxied.test"]
    expected_calls = (
        ["proxy.test"] if scheme == "socks5h" else ["proxy.test", "proxied.test"]
    )
    assert calls == expected_calls


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_socks_allowed_redirect_control(servers, scheme):
    target = servers(_TargetServer, "127.0.0.2")
    proxy = servers(_SOCKSServer, handler_class=_SOCKS5Handler)
    proxy.dns["proxied.test"] = "127.0.0.2"
    calls = []
    client = SafeHttpClient(
        fetch_policy=_LoopbackControlPolicy(),
        proxy=f"{scheme}://proxy.test:{proxy.server_address[1]}",
        resolver=_resolver(calls),
    )
    response = client.get(f"http://proxied.test:{target.server_port}/redirect-final")
    assert response.status_code == 200
    assert [path for path, _ in target.requests_seen] == [
        "/redirect-final",
        "/final",
    ]


def _entry_succeeded(kind, result):
    if kind == "search":
        return "Title: SOCKS result" in result
    if kind == "page":
        return (
            isinstance(result, ArticleResult)
            and "Allowed SOCKS article content" in result.text
        )
    return result is not None and "Allowed SOCKS article content" in result


def _entry_failed(result):
    return (
        isinstance(result, ArticleFetchError)
        or result is None
        or (isinstance(result, str) and result.startswith("Error "))
    )


@pytest.mark.parametrize("kind", ["page", "extract", "search"])
@pytest.mark.parametrize("scenario", ["direct", "redirect", "change"])
@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_entry_point_security_through_socks(
    servers, entry_point_factory, kind, scenario, scheme
):
    allowed = servers(_TargetServer, "127.0.0.2")
    prohibited = servers(_TargetServer, "127.0.0.1")
    proxy = servers(_SOCKSServer, handler_class=_SOCKS5Handler)
    proxy.dns.update(
        {
            "proxied.test": "127.0.0.2",
            "blocked.test": "127.0.0.1",
            "changing.test": "127.0.0.2",
        }
    )
    calls = []
    if scenario == "change" and scheme == "socks5":
        answers = iter(("127.0.0.1", "127.0.0.2", "127.0.0.1", "127.0.0.1"))

        def resolver(host, port):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))]

    else:
        resolver = _resolver(calls)
    invoke = entry_point_factory(
        kind,
        fetch_policy=_LoopbackControlPolicy(),
        proxy=f"{scheme}://proxy.test:{proxy.server_address[1]}",
        resolver=resolver,
    )
    content_path = "/search" if kind == "search" else "/article"
    if scenario == "direct":
        result = invoke(f"http://127.0.0.1:{prohibited.server_port}{content_path}")
        assert _entry_failed(result)
        assert proxy.connections == 0
    elif scenario == "redirect":
        redirect_host = "blocked.test" if scheme == "socks5h" else "127.0.0.1"
        allowed.redirect_target = (
            f"http://{redirect_host}:{prohibited.server_port}{content_path}"
        )
        result = invoke(f"http://proxied.test:{allowed.server_port}/redirect-blocked")
        assert _entry_failed(result)
    else:
        first = invoke(f"http://changing.test:{allowed.server_port}{content_path}")
        assert _entry_succeeded(kind, first)
        if scheme == "socks5h":
            proxy.dns["changing.test"] = "127.0.0.1"
        second = invoke(f"http://changing.test:{prohibited.server_port}{content_path}")
        assert _entry_failed(second)
    assert prohibited.connections == 0
    assert prohibited.requests_seen == []


@pytest.mark.parametrize("scheme", ["socks5", "socks4"])
def test_local_socks_mixed_dns_answers_reject_before_proxy_connection(servers, scheme):
    allowed = servers(_TargetServer, "127.0.0.2")
    prohibited = servers(_TargetServer, "127.0.0.1")
    proxy = servers(_SOCKSServer, handler_class=_SOCKS5Handler)

    def resolver(host, port):
        if host == "proxy.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    client = SafeHttpClient(
        fetch_policy=_LoopbackControlPolicy(),
        proxy=f"{scheme}://proxy.test:{proxy.server_address[1]}",
        resolver=resolver,
    )
    with pytest.raises(FetchPolicyError):
        client.get(f"http://mixed.test:{allowed.server_port}/")
    assert proxy.connections == 0
    assert allowed.connections == 0
    assert prohibited.connections == 0
