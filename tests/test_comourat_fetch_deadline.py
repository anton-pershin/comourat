"""Overall-deadline tests for protected fetching."""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
import requests

from comourat._fetch_transport import _DeadlineRetry
from comourat.fetch import (
    FetchDeadlineExceeded,
    FetchPolicy,
    SafeHttpClient,
    _current_deadline,
    _Deadline,
    _DeadlineExceeded,
)


class _LoopbackControlPolicy(FetchPolicy):
    def _has_builtin_denial(self, address):
        if address == ipaddress.ip_address("127.0.0.2"):
            return False
        return super()._has_builtin_denial(address)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.calls += 1
        self.server.behavior(self)

    def log_message(self, *args):
        return


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, behavior):
        self.calls = 0
        self.behavior = behavior
        super().__init__(address, _Handler)


@pytest.fixture
def server_factory():
    running = []

    def start(behavior, host="127.0.0.2"):
        server = _Server((host, 0), behavior)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running.append((server, thread))
        return server

    yield start
    for server, thread in running:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _client(server=None, timeout=0.1, retries=0, resolver=None):
    return SafeHttpClient(
        fetch_policy=_LoopbackControlPolicy(
            overall_timeout=timeout, max_retries=retries
        ),
        resolver=resolver,
    )


def _send_ok(handler, body=b"ok"):
    handler.send_response(200)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


def test_blocking_dns_uses_overall_deadline_and_late_answer_does_not_connect(
    server_factory,
):
    server = server_factory(_send_ok)
    resolver_started = threading.Event()
    release_resolver = threading.Event()

    def resolver(host, port):
        resolver_started.set()
        release_resolver.wait(1)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port))]

    client = _client(server, timeout=0.05, resolver=resolver)
    started = time.monotonic()
    with pytest.raises(FetchDeadlineExceeded):
        client.get(f"http://slow-dns.test:{server.server_port}/")
    elapsed = time.monotonic() - started
    assert resolver_started.is_set()
    assert elapsed < 0.5
    release_resolver.set()
    time.sleep(0.02)
    assert server.calls == 0


def test_trickled_response_body_cannot_reset_deadline(server_factory):
    def trickle(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "5")
        handler.send_header("Connection", "close")
        handler.end_headers()
        for _ in range(5):
            try:
                handler.wfile.write(b"x")
                handler.wfile.flush()
            except BrokenPipeError:
                return
            time.sleep(0.04)

    server = server_factory(trickle)
    started = time.monotonic()
    with pytest.raises(FetchDeadlineExceeded):
        _client(server, timeout=0.09).get(f"http://127.0.0.2:{server.server_port}/")
    elapsed = time.monotonic() - started
    assert elapsed < 0.18


def test_redirect_and_slow_headers_share_one_deadline(server_factory):
    def behavior(handler):
        if handler.path == "/redirect":
            handler.send_response(302)
            handler.send_header("Location", "/slow")
            handler.send_header("Connection", "close")
            handler.end_headers()
            return
        time.sleep(0.15)
        _send_ok(handler)

    server = server_factory(behavior)
    with pytest.raises(FetchDeadlineExceeded):
        _client(server, timeout=0.06).get(
            f"http://127.0.0.2:{server.server_port}/redirect"
        )
    assert server.calls == 2


def test_retry_consumes_the_same_deadline(server_factory):
    def behavior(handler):
        if handler.server.calls == 1:
            handler.connection.close()
            return
        time.sleep(0.15)
        _send_ok(handler)

    server = server_factory(behavior)
    with pytest.raises(FetchDeadlineExceeded):
        _client(server, timeout=0.06, retries=1).get(
            f"http://127.0.0.2:{server.server_port}/"
        )
    assert server.calls <= 2


def test_default_policy_has_thirty_second_deadline_and_no_retries():
    policy = FetchPolicy()
    assert policy.overall_timeout == 30
    assert policy.max_retries == 0
    assert policy.max_redirects == 30


def test_redirect_limit_stops_before_another_request(server_factory):
    def redirect_loop(handler):
        handler.send_response(302)
        handler.send_header("Location", "/loop")
        handler.send_header("Connection", "close")
        handler.end_headers()

    server = server_factory(redirect_loop)
    client = SafeHttpClient(fetch_policy=_LoopbackControlPolicy(max_redirects=0))
    with pytest.raises(requests.TooManyRedirects):
        client.get(f"http://127.0.0.2:{server.server_port}/loop")
    assert server.calls == 1


def test_retry_delay_longer_than_remaining_deadline_does_not_sleep(monkeypatch):
    deadline = _Deadline.start(0.1, time.monotonic)
    token = _current_deadline.set(deadline)
    sleep = MagicMock()
    monkeypatch.setattr("comourat._fetch_transport.time.sleep", sleep)
    try:
        with pytest.raises(_DeadlineExceeded):
            _DeadlineRetry(total=1)._sleep(1)
    finally:
        _current_deadline.reset(token)
    sleep.assert_not_called()
