"""Tests for shared fetch policy and connection-bound HTTP requests."""

from __future__ import annotations

import ipaddress
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
import requests

from comourat._fetch_transport import _DeadlineRetry
from comourat.fetch import (
    FetchDeadlineExceeded,
    FetchPolicy,
    FetchPolicyError,
    FetchResolutionError,
    SafeHttpClient,
)

PUBLIC_IPV4 = "93.184.216.34"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib handler API
        self.server.requests_seen.append((self.path, self.headers.get("Host", "")))
        if self.path == "/redirect-prohibited":
            target = self.server.redirect_target  # type: ignore[attr-defined]
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        if self.path == "/redirect-scheme":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
            return
        if self.path == "/redirect-final":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.end_headers()
            return
        body = b"<html><body><article>allowed control content</article></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class _CountingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        self.connections = 0
        self.requests_seen = []
        super().__init__(address, _Handler)

    def verify_request(self, request, client_address):
        self.connections += 1
        return True


class _CountingServer6(_CountingServer):
    address_family = socket.AF_INET6


@pytest.fixture
def http_server_factory():
    servers = []

    def start(host="127.0.0.1"):
        server_class = _CountingServer6 if ":" in host else _CountingServer
        server = server_class((host, 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _url(server, path="/"):
    host, port = server.server_address[:2]
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}{path}"


class _LoopbackControlPolicy(FetchPolicy):
    """Permit only the IPv4 control address; keep all other built-in denials."""

    def _has_builtin_denial(self, address):
        if address == ipaddress.ip_address("127.0.0.2"):
            return False
        return super()._has_builtin_denial(address)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http:///missing",
        "http://example.com:bad/",
    ],
)
def test_invalid_targets_fail_before_session_request(url):
    session = MagicMock()
    client = SafeHttpClient(http_session=session)
    with pytest.raises(FetchPolicyError):
        client.get(url)
    session.get.assert_not_called()


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.1.1",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "100.64.0.1",
        "192.0.2.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::",
        "ff02::1",
        "::ffff:127.0.0.1",
    ],
)
def test_prohibited_ip_literals_fail_before_session_request(address):
    host = f"[{address}]" if ":" in address else address
    session = MagicMock()
    client = SafeHttpClient(http_session=session)
    with pytest.raises(FetchPolicyError):
        client.get(f"http://{host}/")
    session.get.assert_not_called()


def test_custom_host_ip_and_cidr_denials():
    policy = FetchPolicy(
        deny_hosts=["ExAmPlE.COM."],
        deny_addresses=["93.184.216.34", "2001:4860:4860::/64"],
    )
    session = MagicMock()
    client = SafeHttpClient(http_session=session, fetch_policy=policy)
    for url in (
        "http://example.com/",
        "http://EXAMPLE.COM./",
        "http://93.184.216.34/",
        "http://[2001:4860:4860::8888]/",
        "http://[::ffff:93.184.216.34]/",
    ):
        with pytest.raises(FetchPolicyError):
            client.get(url)
    session.get.assert_not_called()


def test_mixed_dns_answers_reject_entire_target(http_server_factory):
    prohibited = http_server_factory("127.0.0.1")

    def resolver(host, port):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IPV4, port)),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1", prohibited.server_port),
            ),
        ]

    client = SafeHttpClient(resolver=resolver)
    with pytest.raises(FetchPolicyError):
        client.get(f"http://mixed.test:{prohibited.server_port}/")
    assert prohibited.connections == 0
    assert prohibited.requests_seen == []


def test_direct_connection_uses_validated_address_and_original_host(
    http_server_factory,
):
    allowed = http_server_factory("127.0.0.2")
    calls = []

    def resolver(host, port):
        calls.append((host, port))
        if len(calls) > 1:
            raise AssertionError("unexpected second DNS resolution")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port))]

    client = SafeHttpClient(fetch_policy=_LoopbackControlPolicy(), resolver=resolver)
    response = client.get(f"http://control.test:{allowed.server_port}/")
    assert response.status_code == 200
    assert b"allowed control content" in response.content
    assert len(calls) == 1
    assert allowed.requests_seen[0][1] == f"control.test:{allowed.server_port}"


def test_direct_prohibited_listener_receives_no_connection(http_server_factory):
    prohibited = http_server_factory("127.0.0.1")
    client = SafeHttpClient()
    with pytest.raises(FetchPolicyError):
        client.get(_url(prohibited))
    assert prohibited.connections == 0
    assert prohibited.requests_seen == []


def test_redirect_to_prohibited_listener_receives_no_connection(http_server_factory):
    prohibited = http_server_factory("127.0.0.1")
    allowed = http_server_factory("127.0.0.2")
    allowed.redirect_target = _url(prohibited)  # type: ignore[attr-defined]

    def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port))]

    client = SafeHttpClient(fetch_policy=_LoopbackControlPolicy(), resolver=resolver)
    with pytest.raises(FetchPolicyError):
        client.get(f"http://control.test:{allowed.server_port}/redirect-prohibited")
    assert prohibited.connections == 0
    assert prohibited.requests_seen == []


def test_allowed_redirect_control(http_server_factory):
    allowed = http_server_factory("127.0.0.2")

    def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port))]

    client = SafeHttpClient(fetch_policy=_LoopbackControlPolicy(), resolver=resolver)
    response = client.get(f"http://control.test:{allowed.server_port}/redirect-final")
    assert response.status_code == 200
    assert b"allowed control content" in response.content
    assert [path for path, _ in allowed.requests_seen] == ["/redirect-final", "/final"]


def test_resolution_change_to_prohibited_address_is_rejected(http_server_factory):
    allowed = http_server_factory("127.0.0.2")
    prohibited = http_server_factory("127.0.0.1")
    answers = iter(("127.0.0.2", "127.0.0.1"))

    def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))]

    client = SafeHttpClient(fetch_policy=_LoopbackControlPolicy(), resolver=resolver)
    assert client.get(f"http://changing.test:{allowed.server_port}/").status_code == 200
    with pytest.raises(FetchPolicyError):
        client.get(f"http://changing.test:{prohibited.server_port}/")
    assert prohibited.connections == 0


def test_ipv6_prohibited_listener_receives_no_connection(http_server_factory):
    try:
        prohibited = http_server_factory("::1")
    except OSError as exc:
        pytest.skip(f"IPv6 listener unavailable: {exc}")
    client = SafeHttpClient()
    with pytest.raises(FetchPolicyError):
        client.get(_url(prohibited))
    assert prohibited.connections == 0


def test_overall_timeout_must_be_positive():
    with pytest.raises(ValueError):
        FetchPolicy(overall_timeout=0)


def test_policy_and_deadline_errors_are_requests_exceptions():
    assert issubclass(FetchPolicyError, requests.RequestException)
    assert issubclass(FetchDeadlineExceeded, requests.Timeout)


@pytest.mark.parametrize("host", ["2130706433", "127.1", "0x7f000001", "017700000001"])
def test_alternative_ipv4_notation_is_checked_after_resolution(
    http_server_factory, host
):
    prohibited = http_server_factory("127.0.0.1")

    def resolver(resolved_host, port):
        assert resolved_host == host
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    client = SafeHttpClient(resolver=resolver)
    with pytest.raises(FetchPolicyError):
        client.get(f"http://{host}:{prohibited.server_port}/")
    assert prohibited.connections == 0


def test_environment_proxies_are_ignored(http_server_factory, monkeypatch):
    allowed = http_server_factory("127.0.0.2")
    prohibited = http_server_factory("127.0.0.1")
    monkeypatch.setenv("HTTP_PROXY", _url(prohibited))
    monkeypatch.setenv("HTTPS_PROXY", _url(prohibited))
    monkeypatch.setenv("ALL_PROXY", _url(prohibited))

    def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port))]

    session = requests.Session()
    client = SafeHttpClient(
        session,
        fetch_policy=_LoopbackControlPolicy(),
        resolver=resolver,
    )
    response = client.get(f"http://control.test:{allowed.server_port}/")
    assert response.status_code == 200
    assert client.session.trust_env is False
    assert prohibited.connections == 0


def test_a_session_cannot_be_shared_by_clients_with_different_policies():
    session = MagicMock()
    session.proxies = {}
    SafeHttpClient(session)
    with pytest.raises(ValueError, match="separate session"):
        SafeHttpClient(session, fetch_policy=FetchPolicy(deny_hosts=["other.test"]))


def test_concurrent_clients_keep_independent_policies():
    def session():
        value = MagicMock()
        value.proxies = {}
        value.get.return_value = MagicMock(status_code=200)
        return value

    denied_client = SafeHttpClient(
        session(), fetch_policy=FetchPolicy(deny_hosts=["blocked.test"])
    )
    allowed_client = SafeHttpClient(session(), fetch_policy=FetchPolicy())

    with ThreadPoolExecutor(max_workers=2) as executor:
        denied_future = executor.submit(denied_client.get, "http://blocked.test/")
        allowed_future = executor.submit(allowed_client.get, "http://blocked.test/")
        with pytest.raises(FetchPolicyError):
            denied_future.result()
        assert allowed_future.result().status_code == 200


@pytest.mark.parametrize("field", ["max_redirects", "max_retries"])
@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_redirect_and_retry_limits_must_be_non_negative_integers(field, value):
    with pytest.raises((ValueError, TypeError)):
        FetchPolicy(**{field: value})


def test_idna_and_punycode_host_rules_are_equivalent():
    for denied_host, target_host in (
        ("faß.de", "xn--fa-hia.de"),
        ("xn--fa-hia.de", "faß.de"),
    ):
        session = MagicMock()
        client = SafeHttpClient(
            session, fetch_policy=FetchPolicy(deny_hosts=[denied_host])
        )
        with pytest.raises(FetchPolicyError):
            client.get(f"http://{target_host}/")
        session.get.assert_not_called()


@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://0.0.0.0:8080",
        "http://169.254.169.254:8080",
        "http://[::]:8080",
        "http://[fe80::1]:8080",
    ],
)
def test_trusted_proxy_exception_does_not_allow_special_use_addresses(proxy_url):
    with pytest.raises(FetchPolicyError):
        FetchPolicy()._parse_target(
            proxy_url,
            allowed_schemes=("http", "https", "socks4", "socks5", "socks5h"),
            proxy=True,
        )


@pytest.mark.parametrize("proxy_url", ["http://10.0.0.1:8080", "http://[fd00::1]:8080"])
def test_explicit_private_proxy_addresses_are_allowed(proxy_url):
    target = FetchPolicy()._parse_target(
        proxy_url,
        allowed_schemes=("http", "https", "socks4", "socks5", "socks5h"),
        proxy=True,
    )
    assert target.host in {"10.0.0.1", "fd00::1"}


def test_empty_dns_answers_fail_before_connection():
    client = SafeHttpClient(resolver=lambda host, port: [])
    with pytest.raises(FetchResolutionError):
        client.get("http://empty.test/")


@pytest.mark.parametrize(
    "answer",
    [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80, 0, 0)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("::1", 80)),
        (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("127.0.0.1", 80)),
    ],
)
def test_malformed_resolver_answers_fail_before_socket_creation(answer):
    client = SafeHttpClient(resolver=lambda host, port: [answer])
    with pytest.raises(FetchResolutionError):
        client.get("http://malformed.test/")


def test_second_allowed_address_is_tried_after_connection_failure(http_server_factory):
    allowed = http_server_factory("127.0.0.2")

    class TwoLoopbackPolicy(_LoopbackControlPolicy):
        def _has_builtin_denial(self, address):
            if address == ipaddress.ip_address("127.0.0.3"):
                return False
            return super()._has_builtin_denial(address)

    def resolver(host, port):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.3", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port)),
        ]

    client = SafeHttpClient(fetch_policy=TwoLoopbackPolicy(), resolver=resolver)
    response = client.get(f"http://fallback.test:{allowed.server_port}/")
    assert response.status_code == 200


def test_injected_host_adapter_cannot_bypass_policy(http_server_factory):
    allowed = http_server_factory("127.0.0.2")

    class RejectingAdapter(requests.adapters.HTTPAdapter):
        called = False

        def send(self, *args, **kwargs):
            self.called = True
            raise AssertionError("unprotected host adapter was used")

    rejecting = RejectingAdapter()
    session = requests.Session()
    session.mount("http://control.test", rejecting)

    def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port))]

    client = SafeHttpClient(
        session,
        fetch_policy=_LoopbackControlPolicy(),
        resolver=resolver,
    )
    session.mount("http://control.test:80", rejecting)
    response = client.get(f"http://control.test:{allowed.server_port}/")
    assert response.status_code == 200
    assert rejecting.called is False


def test_custom_scheme_adapter_cannot_execute_redirect(http_server_factory):
    allowed = http_server_factory("127.0.0.2")

    class RejectingAdapter(requests.adapters.HTTPAdapter):
        called = False

        def send(self, *args, **kwargs):
            self.called = True
            raise AssertionError("custom-scheme adapter was used")

    rejecting = RejectingAdapter()
    session = requests.Session()
    session.mount("file://", rejecting)

    def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port))]

    client = SafeHttpClient(
        session,
        fetch_policy=_LoopbackControlPolicy(),
        resolver=resolver,
    )
    with pytest.raises(FetchPolicyError):
        client.get(f"http://control.test:{allowed.server_port}/redirect-scheme")
    assert rejecting.called is False


def test_production_adapter_uses_deadline_retry_and_fails_closed_on_old_hook():
    client = SafeHttpClient(fetch_policy=FetchPolicy(max_retries=2))
    adapter = client.session.get_adapter("http://example.com")
    assert isinstance(adapter.max_retries, _DeadlineRetry)
    with pytest.raises(FetchPolicyError, match="2.32.2"):
        adapter.get_connection(MagicMock())
    with pytest.raises(FetchPolicyError, match="2.32.2"):
        adapter._get_connection(MagicMock(), True, proxies={}, cert=None)


def test_transport_exception_urls_are_redacted():
    session = MagicMock()
    session.proxies = {}
    session.get.side_effect = requests.ConnectionError(
        "failed http://user:secret@example.com/private/path?token=sensitive"
    )
    client = SafeHttpClient(session)
    with pytest.raises(requests.ConnectionError) as error:
        client.get("http://example.com/private/path?token=sensitive")
    message = str(error.value)
    assert "secret" not in message
    assert "sensitive" not in message
    assert "/private/path" not in message
    assert "http://example.com" in message


@pytest.mark.parametrize("denied_first", [False, True])
def test_caller_denied_mixed_answers_reject_entire_set_in_any_order(
    http_server_factory, denied_first
):
    allowed = http_server_factory("127.0.0.2")
    denied = http_server_factory("127.0.0.3")

    class TwoAddressPolicy(_LoopbackControlPolicy):
        def _has_builtin_denial(self, address):
            if address == ipaddress.ip_address("127.0.0.3"):
                return False
            return super()._has_builtin_denial(address)

    addresses = (
        ["127.0.0.3", "127.0.0.2"] if denied_first else ["127.0.0.2", "127.0.0.3"]
    )

    def resolver(host, port):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    client = SafeHttpClient(
        fetch_policy=TwoAddressPolicy(deny_addresses=["127.0.0.3/32"]),
        resolver=resolver,
    )
    with pytest.raises(FetchPolicyError):
        client.get(f"http://mixed-denied.test:{allowed.server_port}/")
    assert allowed.connections == 0
    assert denied.connections == 0


def test_real_connection_error_redacts_path_only_urls():
    probe = socket.socket()
    probe.bind(("127.0.0.3", 0))
    port = probe.getsockname()[1]
    probe.close()

    class RefusedControlPolicy(_LoopbackControlPolicy):
        def _has_builtin_denial(self, address):
            if address == ipaddress.ip_address("127.0.0.3"):
                return False
            return super()._has_builtin_denial(address)

    client = SafeHttpClient(fetch_policy=RefusedControlPolicy(max_retries=0))
    with pytest.raises(requests.ConnectionError) as error:
        client.get(f"http://127.0.0.3:{port}/private/path?token=sensitive")
    message = str(error.value)
    assert "sensitive" not in message
    assert "/private/path" not in message
