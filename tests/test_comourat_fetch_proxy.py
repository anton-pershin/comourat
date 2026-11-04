"""Controlled HTTP proxy tests for protected fetching."""

from __future__ import annotations

import datetime
import http.client
import select
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from comourat.fetch import FetchPolicy, FetchPolicyError, SafeHttpClient


class _TargetHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.requests_seen.append((self.path, self.headers.get("Host", "")))
        if self.path == "/redirect-blocked":
            self.send_response(302)
            self.send_header("Location", self.server.redirect_target)
            self.send_header("Connection", "close")
            self.end_headers()
            return
        body = b"proxied control content"
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


class _ForwardProxyHandler(BaseHTTPRequestHandler):
    def _target_address(self, host):
        answers = self.server.dns.get(host)
        if answers is None:
            return None
        if isinstance(answers, str):
            answers = [answers]
        if not answers or any(
            ip not in self.server.allowed_target_ips for ip in answers
        ):
            return None
        return answers[0]

    def do_GET(self):  # noqa: N802
        self.server.requests_seen.append(self.path)
        parsed = urlsplit(self.path)
        target_ip = self._target_address(parsed.hostname)
        if target_ip is None:
            self.send_error(502, "controlled proxy destination denial")
            return
        path = urlunsplit(("", "", parsed.path, parsed.query, ""))
        connection = http.client.HTTPConnection(target_ip, parsed.port or 80, timeout=2)
        headers = dict(self.headers)
        headers["Host"] = parsed.netloc
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        self.send_response(response.status)
        self.send_header("Content-Length", str(len(body)))
        if response.getheader("Location"):
            self.send_header("Location", response.getheader("Location"))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        connection.close()

    def do_CONNECT(self):  # noqa: N802
        self.server.connect_requests.append(self.path)
        parsed = urlsplit(f"//{self.path}")
        target_ip = self._target_address(parsed.hostname)
        if target_ip is None:
            self.send_error(502, "controlled proxy destination denial")
            return
        remote = socket.create_connection((target_ip, parsed.port or 443), timeout=2)
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.wfile.flush()
        sockets = [self.connection, remote]
        try:
            while True:
                readable, _, _ = select.select(sockets, [], [], 2)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    destination = (
                        remote if source is self.connection else self.connection
                    )
                    destination.sendall(data)
        finally:
            remote.close()

    def log_message(self, *args):
        return


class _ForwardProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        self.connections = 0
        self.requests_seen = []
        self.connect_requests = []
        self.dns = {}
        self.allowed_target_ips = {"127.0.0.2"}
        super().__init__(address, _ForwardProxyHandler)

    def verify_request(self, request, client_address):
        self.connections += 1
        return True


@pytest.fixture
def proxy_servers():
    servers = []

    def start(server_class=_TargetServer, host="127.0.0.1", tls_context=None):
        server = server_class((host, 0))
        if tls_context is not None:
            server.socket = tls_context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _proxy_client(proxy, resolver, policy=None, scheme="http", session=None):
    return SafeHttpClient(
        session,
        fetch_policy=policy,
        proxy=f"{scheme}://proxy.test:{proxy.server_port}",
        resolver=resolver,
    )


def _tls_server_context(tmp_path, hostname):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / f"{hostname}.crt"
    key_path = tmp_path / f"{hostname}.key"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)
    return context, str(certificate_path)


def test_http_forward_proxy_receives_hostname_without_local_target_dns(proxy_servers):
    target = proxy_servers(_TargetServer, "127.0.0.2")
    proxy = proxy_servers(_ForwardProxyServer)
    proxy.dns["proxied.test"] = "127.0.0.2"
    resolver_calls = []

    def resolver(host, port):
        resolver_calls.append(host)
        assert host == "proxy.test"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    client = _proxy_client(proxy, resolver)
    response = client.get(f"http://proxied.test:{target.server_port}/article")
    assert response.status_code == 200
    assert response.content == b"proxied control content"
    assert resolver_calls == ["proxy.test"]
    assert proxy.requests_seen == [f"http://proxied.test:{target.server_port}/article"]
    assert target.requests_seen == [("/article", f"proxied.test:{target.server_port}")]


def test_literal_prohibited_target_is_rejected_before_http_proxy_connection(
    proxy_servers,
):
    target = proxy_servers(_TargetServer, "127.0.0.1")
    proxy = proxy_servers(_ForwardProxyServer)
    client = _proxy_client(proxy, lambda host, port: [])
    with pytest.raises(FetchPolicyError):
        client.get(f"http://127.0.0.1:{target.server_port}/")
    assert proxy.connections == 0
    assert target.connections == 0


def test_policy_enforcing_http_proxy_blocks_remote_resolved_target(proxy_servers):
    target = proxy_servers(_TargetServer, "127.0.0.1")
    proxy = proxy_servers(_ForwardProxyServer)
    proxy.dns["blocked.test"] = "127.0.0.1"
    resolver_calls = []

    def resolver(host, port):
        resolver_calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    client = _proxy_client(proxy, resolver)
    response = client.get(f"http://blocked.test:{target.server_port}/")
    assert response.status_code == 502
    assert resolver_calls == ["proxy.test"]
    assert target.connections == 0
    assert target.requests_seen == []


def test_caller_denial_of_proxy_endpoint_prevents_connection(proxy_servers):
    proxy = proxy_servers(_ForwardProxyServer)
    policy = FetchPolicy(deny_addresses=["127.0.0.0/8"])
    client = _proxy_client(
        proxy,
        lambda host, port: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
        ],
        policy=policy,
    )
    with pytest.raises(FetchPolicyError):
        client.get("http://proxied.test/")
    assert proxy.connections == 0


def test_http_connect_preserves_hostname_sni_and_certificate(proxy_servers, tmp_path):
    tls_context, ca_path = _tls_server_context(tmp_path, "proxied.test")
    sni_names = []
    tls_context.set_servername_callback(
        lambda sock, name, context: sni_names.append(name)
    )
    target = proxy_servers(_TargetServer, "127.0.0.2", tls_context)
    proxy = proxy_servers(_ForwardProxyServer)
    proxy.dns["proxied.test"] = "127.0.0.2"
    resolver_calls = []

    def resolver(host, port):
        resolver_calls.append(host)
        assert host == "proxy.test"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    session = requests.Session()
    session.verify = ca_path
    client = _proxy_client(proxy, resolver, session=session)
    response = client.get(f"https://proxied.test:{target.server_port}/secure")
    assert response.status_code == 200
    assert response.content == b"proxied control content"
    assert resolver_calls == ["proxy.test"]
    assert proxy.connect_requests == [f"proxied.test:{target.server_port}"]
    assert sni_names == ["proxied.test"]
    assert target.requests_seen == [("/secure", f"proxied.test:{target.server_port}")]


def test_policy_enforcing_connect_proxy_blocks_remote_target(proxy_servers):
    target = proxy_servers(_TargetServer, "127.0.0.1")
    proxy = proxy_servers(_ForwardProxyServer)
    proxy.dns["blocked.test"] = "127.0.0.1"

    def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    client = _proxy_client(proxy, resolver)
    with pytest.raises(requests.RequestException):
        client.get(f"https://blocked.test:{target.server_port}/")
    assert proxy.connect_requests == [f"blocked.test:{target.server_port}"]
    assert target.connections == 0


def test_https_proxy_identity_and_forwarded_http_request(proxy_servers, tmp_path):
    tls_context, ca_path = _tls_server_context(tmp_path, "proxy.test")
    sni_names = []
    tls_context.set_servername_callback(
        lambda sock, name, context: sni_names.append(name)
    )
    target = proxy_servers(_TargetServer, "127.0.0.2")
    proxy = proxy_servers(_ForwardProxyServer, tls_context=tls_context)
    proxy.dns["proxied.test"] = "127.0.0.2"

    def resolver(host, port):
        assert host == "proxy.test"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    session = requests.Session()
    session.verify = ca_path
    client = _proxy_client(proxy, resolver, scheme="https", session=session)
    response = client.get(f"http://proxied.test:{target.server_port}/")
    assert response.status_code == 200
    assert response.content == b"proxied control content"
    assert sni_names == ["proxy.test"]
    assert proxy.requests_seen == [f"http://proxied.test:{target.server_port}/"]


def test_http_proxy_rejects_mixed_remote_dns_answers(proxy_servers):
    allowed = proxy_servers(_TargetServer, "127.0.0.2")
    prohibited = proxy_servers(_TargetServer, "127.0.0.1")
    proxy = proxy_servers(_ForwardProxyServer)
    proxy.dns["mixed.test"] = ["127.0.0.2", "127.0.0.1"]

    def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    client = _proxy_client(proxy, resolver)
    response = client.get(f"http://mixed.test:{allowed.server_port}/")
    assert response.status_code == 502
    assert allowed.connections == 0
    assert prohibited.connections == 0


def test_http_proxy_redirect_uses_proxy_policy_on_every_target(proxy_servers):
    allowed = proxy_servers(_TargetServer, "127.0.0.2")
    prohibited = proxy_servers(_TargetServer, "127.0.0.1")
    allowed.redirect_target = f"http://blocked.test:{prohibited.server_port}/"
    proxy = proxy_servers(_ForwardProxyServer)
    proxy.dns.update({"proxied.test": "127.0.0.2", "blocked.test": "127.0.0.1"})

    def resolver(host, port):
        assert host == "proxy.test"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    client = _proxy_client(proxy, resolver)
    response = client.get(f"http://proxied.test:{allowed.server_port}/redirect-blocked")
    assert response.status_code == 502
    assert proxy.requests_seen == [
        f"http://proxied.test:{allowed.server_port}/redirect-blocked",
        f"http://blocked.test:{prohibited.server_port}/",
    ]
    assert prohibited.connections == 0


def test_http_proxy_redirect_to_literal_denial_makes_no_second_proxy_request(
    proxy_servers,
):
    allowed = proxy_servers(_TargetServer, "127.0.0.2")
    prohibited = proxy_servers(_TargetServer, "127.0.0.1")
    allowed.redirect_target = f"http://127.0.0.1:{prohibited.server_port}/"
    proxy = proxy_servers(_ForwardProxyServer)
    proxy.dns["proxied.test"] = "127.0.0.2"

    def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    client = _proxy_client(proxy, resolver)
    with pytest.raises(FetchPolicyError):
        client.get(f"http://proxied.test:{allowed.server_port}/redirect-blocked")
    assert len(proxy.requests_seen) == 1
    assert prohibited.connections == 0


def test_proxy_policy_error_does_not_expose_proxy_credentials(proxy_servers):
    proxy = proxy_servers(_ForwardProxyServer)
    policy = FetchPolicy(deny_addresses=["127.0.0.0/8"])

    def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    client = SafeHttpClient(
        fetch_policy=policy,
        proxy=f"http://user:secret@proxy.test:{proxy.server_port}",
        resolver=resolver,
    )
    with pytest.raises(FetchPolicyError) as error:
        client.get("http://proxied.test/")
    assert "secret" not in str(error.value)
    assert "user" not in str(error.value)


def test_https_target_through_https_proxy_verifies_both_identities(
    proxy_servers, tmp_path
):
    target_context, target_ca = _tls_server_context(tmp_path, "proxied.test")
    proxy_context, proxy_ca = _tls_server_context(tmp_path, "proxy.test")
    target_sni = []
    proxy_sni = []
    target_context.set_servername_callback(
        lambda sock, name, context: target_sni.append(name)
    )
    proxy_context.set_servername_callback(
        lambda sock, name, context: proxy_sni.append(name)
    )
    target = proxy_servers(_TargetServer, "127.0.0.2", target_context)
    proxy = proxy_servers(_ForwardProxyServer, tls_context=proxy_context)
    proxy.dns["proxied.test"] = "127.0.0.2"
    bundle = tmp_path / "bundle.crt"
    bundle.write_bytes(
        (tmp_path / "proxied.test.crt").read_bytes()
        + (tmp_path / "proxy.test.crt").read_bytes()
    )

    def resolver(host, port):
        assert host == "proxy.test"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    session = requests.Session()
    session.verify = str(bundle)
    client = _proxy_client(proxy, resolver, scheme="https", session=session)
    response = client.get(f"https://proxied.test:{target.server_port}/")
    assert response.status_code == 200
    assert response.content == b"proxied control content"
    assert proxy_sni == ["proxy.test"]
    assert target_sni == ["proxied.test"]
