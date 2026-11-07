"""Overall-deadline tests for protected fetching."""

from __future__ import annotations

import ipaddress
import queue
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
import requests
import socks

import comourat.clients.arxiv as arxiv_module
from comourat._fetch_transport import (
    _DeadlineRetry,
    _DeadlineSocket,
    _DeadlineSSLContext,
    _DeadlineTimeout,
    _PinnedSocksSocket,
)
from comourat.clients.arxiv import ArxivClient
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


class _RawHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.connections += 1
        self.server.behavior(self.request)


class _RawServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, behavior):
        self.connections = 0
        self.behavior = behavior
        super().__init__(address, _RawHandler)


@pytest.fixture
def raw_server_factory():
    running = []

    def start(behavior, host="127.0.0.2"):
        server = _RawServer((host, 0), behavior)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running.append((server, thread))
        return server

    yield start
    for server, thread in running:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _start_stalling_raw_server(raw_server_factory, host="127.0.0.2"):
    connection_started = threading.Event()

    def stall(_connection):
        connection_started.set()
        time.sleep(1)

    server = raw_server_factory(stall, host=host)
    return server, connection_started


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


_ARXIV_FEED = (
    b'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
    b"<title>A controlled title</title>"
    b"<summary>A controlled abstract</summary>"
    b"</entry></feed>"
)


def _send_arxiv(handler, status=200):
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(_ARXIV_FEED)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(_ARXIV_FEED)


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


def test_session_authentication_consumes_the_same_deadline(server_factory):
    server = server_factory(_send_ok)
    now = [0.0]

    class AdvancingAuth:
        def __call__(self, request):
            now[0] += 2.0
            return request

    session = requests.Session()
    session.auth = AdvancingAuth()
    client = SafeHttpClient(
        http_session=session,
        fetch_policy=_LoopbackControlPolicy(overall_timeout=1.0),
        clock=lambda: now[0],
    )

    with pytest.raises(FetchDeadlineExceeded):
        client.get(f"http://127.0.0.2:{server.server_port}/")

    assert server.calls == 0


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
    assert policy.connection_timeout is None
    assert policy.read_timeout is None
    assert policy.max_retries == 0
    assert policy.max_redirects == 30


@pytest.mark.parametrize(
    "value", [0, -1, True, False, float("nan"), float("inf"), "10"]
)
def test_connection_timeout_must_be_positive_finite_number_or_none(value):
    with pytest.raises(ValueError):
        FetchPolicy(connection_timeout=value)


@pytest.mark.parametrize(
    "value", [0, -1, True, False, float("nan"), float("inf"), "10"]
)
def test_read_timeout_must_be_positive_finite_number_or_none(value):
    with pytest.raises(ValueError):
        FetchPolicy(read_timeout=value)


def test_connection_timeout_uses_remaining_overall_time_as_its_upper_bound():
    class ControlledDeadline:
        remaining_seconds = 5.0

        def remaining(self):
            return self.remaining_seconds

    deadline = ControlledDeadline()
    timeout = _DeadlineTimeout(deadline, connection_timeout=10.0)

    assert timeout.connect_timeout == 5.0
    assert timeout.read_timeout == 5.0

    deadline.remaining_seconds = 20.0
    assert timeout.connect_timeout == 10.0
    assert timeout.read_timeout == 20.0

    cloned = timeout.clone()
    assert cloned.connect_timeout == 10.0
    assert cloned.read_timeout == 20.0


def test_read_timeout_uses_remaining_overall_time_as_its_upper_bound():
    class ControlledDeadline:
        remaining_seconds = 5.0
        read_timeout = 2.0

        def remaining(self):
            return self.remaining_seconds

    deadline = ControlledDeadline()
    timeout = _DeadlineTimeout(deadline, connection_timeout=10.0)

    assert timeout.connect_timeout == 5.0
    assert timeout.read_timeout == 2.0

    deadline.remaining_seconds = 1.0
    assert timeout.connect_timeout == 1.0
    assert timeout.read_timeout == 1.0

    cloned = timeout.clone()
    assert cloned.connect_timeout == 1.0
    assert cloned.read_timeout == 1.0


def test_safe_client_reuses_but_does_not_close_an_external_deadline(monkeypatch):
    session = requests.Session()
    client = SafeHttpClient(http_session=session)
    deadline = _Deadline.start(5, time.monotonic)
    observed_deadlines = []
    response = MagicMock()

    def get(*args, **kwargs):
        observed_deadlines.append(_current_deadline.get())
        assert "_deadline" not in kwargs
        return response

    monkeypatch.setattr(session, "get", get)
    try:
        result = client._call(session.get, "https://example.com", _deadline=deadline)
        assert result is response
        assert observed_deadlines == [deadline]
        assert deadline.remaining() > 0
        assert deadline._active
    finally:
        deadline.close()


def test_direct_socket_restores_overall_timeout_after_connect(monkeypatch):
    deadline = _Deadline.start(5, time.monotonic)
    sock = _DeadlineSocket(
        socket.AF_INET,
        socket.SOCK_STREAM,
        deadline=deadline,
        connection_timeout=0.25,
    )
    observed = []

    def successful_connect(socket_instance, address):
        observed.append(socket_instance.gettimeout())

    monkeypatch.setattr(socket.socket, "connect", successful_connect)
    try:
        sock.connect(("93.184.216.34", 80))
        assert observed == [0.25]
        assert 0.25 < sock.gettimeout() <= 5
    finally:
        sock.close()
        deadline.close()


def test_socks_socket_restores_overall_timeout_after_connect(monkeypatch):
    deadline = _Deadline.start(5, time.monotonic)
    sock = _PinnedSocksSocket(
        socket.AF_INET,
        socket.SOCK_STREAM,
        proxy_sockaddr=("127.0.0.1", 1080),
        deadline=deadline,
        connection_timeout=0.25,
    )
    observed = []

    def successful_connect(socket_instance, address, *args):
        observed.append(socket_instance.gettimeout())

    monkeypatch.setattr(socks.socksocket, "connect", successful_connect)
    try:
        sock.connect(("93.184.216.34", 80))
        assert observed == [0.25]
        assert 0.25 < sock.gettimeout() <= 5
    finally:
        sock.close()
        deadline.close()


def test_adapter_receives_the_configured_connection_timeout(
    server_factory, monkeypatch
):
    import comourat._fetch_transport as transport

    server = server_factory(_send_ok)
    original_timeout = transport._DeadlineTimeout
    received = []

    def recording_timeout(deadline, connection_timeout=None):
        received.append(connection_timeout)
        return original_timeout(deadline, connection_timeout)

    monkeypatch.setattr(transport, "_DeadlineTimeout", recording_timeout)
    client = SafeHttpClient(
        fetch_policy=_LoopbackControlPolicy(overall_timeout=5.0, connection_timeout=1.0)
    )

    response = client.get(f"http://127.0.0.2:{server.server_port}/")

    assert response.status_code == 200
    assert received
    assert set(received) == {1.0}


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
        deadline.close()
    sleep.assert_not_called()


def test_adapter_retry_after_uses_overall_deadline(server_factory):
    def retry_after(handler):
        if handler.server.calls == 1:
            handler.send_response(503)
            handler.send_header("Retry-After", "1")
            handler.send_header("Content-Length", "0")
            handler.send_header("Connection", "close")
            handler.end_headers()
            return
        _send_ok(handler)

    server = server_factory(retry_after)
    client = _client(server, timeout=0.05, retries=1)
    adapter = client.session.get_adapter("http://example.com")
    adapter.max_retries = _DeadlineRetry(
        total=1,
        status=1,
        status_forcelist={503},
    )
    started = time.monotonic()
    with pytest.raises(FetchDeadlineExceeded):
        client.get(f"http://127.0.0.2:{server.server_port}/")
    assert time.monotonic() - started < 0.5
    assert server.calls == 1


def test_watchdog_interrupts_blocking_socket_receive():
    left, right = socket.socketpair()
    deadline = _Deadline.start(0.05, time.monotonic)
    deadline.register(left)
    started = time.monotonic()
    try:
        try:
            received = left.recv(1)
            assert received == b""
        except OSError:
            pass
        assert time.monotonic() - started < 0.5
    finally:
        deadline.close()
        left.close()
        right.close()


def test_watchdog_stops_even_with_continuous_socket_progress():
    left, right = socket.socketpair()
    stop = threading.Event()

    def write_progress():
        while not stop.is_set():
            try:
                right.send(b"x")
            except OSError:
                return
            stop.wait(0.01)

    writer = threading.Thread(target=write_progress, daemon=True)
    writer.start()
    deadline = _Deadline.start(0.06, time.monotonic)
    deadline.register(left)
    started = time.monotonic()
    reads = 0
    try:
        while True:
            try:
                if not left.recv(1):
                    break
                reads += 1
            except OSError:
                break
        assert time.monotonic() - started < 0.5
        assert reads < 50
    finally:
        stop.set()
        deadline.close()
        left.close()
        right.close()
        writer.join(timeout=1)


def test_ssl_context_registers_socket_before_handshake():
    events = []

    class TLSSocket:
        def do_handshake(self):
            events.append("handshake")

        def close(self):
            pass

    class Context:
        def wrap_socket(self, sock, *args, **kwargs):
            events.append(("handshake_on_connect", kwargs["do_handshake_on_connect"]))
            return TLSSocket()

    deadline = _Deadline.start(1, time.monotonic)
    original_register = deadline.register

    def register(sock):
        events.append("register")
        original_register(sock)

    deadline.register = register
    try:
        _DeadlineSSLContext(Context(), deadline).wrap_socket(object())
        assert events == [("handshake_on_connect", False), "register", "handshake"]
    finally:
        deadline.close()


def test_stalled_tls_handshake_uses_absolute_deadline(raw_server_factory):
    server, connection_started = _start_stalling_raw_server(raw_server_factory)
    client = _client(timeout=0.2)
    started = time.monotonic()
    with pytest.raises(FetchDeadlineExceeded):
        client.get(f"https://127.0.0.2:{server.server_address[1]}/")
    assert time.monotonic() - started < 0.5
    assert connection_started.wait(0.5), "server did not accept the TLS connection"
    assert server.connections == 1


def test_proxy_negotiation_uses_absolute_deadline(raw_server_factory):
    server, connection_started = _start_stalling_raw_server(
        raw_server_factory, host="127.0.0.1"
    )
    client = SafeHttpClient(
        fetch_policy=FetchPolicy(overall_timeout=0.2),
        proxy=f"http://127.0.0.1:{server.server_address[1]}",
    )
    started = time.monotonic()
    with pytest.raises(FetchDeadlineExceeded):
        client.get("https://93.184.216.34/")
    assert time.monotonic() - started < 0.5
    assert connection_started.wait(0.5), "server did not accept the proxy connection"
    assert server.connections == 1


def test_request_body_upload_uses_absolute_deadline(raw_server_factory):
    server, connection_started = _start_stalling_raw_server(raw_server_factory)

    def body():
        for _ in range(1024):
            yield b"x" * 65536

    client = _client(timeout=0.2)
    started = time.monotonic()
    with pytest.raises(FetchDeadlineExceeded):
        client.post(
            f"http://127.0.0.2:{server.server_address[1]}/upload",
            data=body(),
            headers={"Content-Length": str(1024 * 65536)},
        )
    assert time.monotonic() - started < 0.5
    assert connection_started.wait(0.5), "server did not accept the upload connection"
    assert server.connections == 1


@pytest.fixture
def arxiv_controlled_transport(monkeypatch):
    """Use the real safe transport with loopback access and no live DNS."""

    def install(resolver=None):
        def safe_client_factory(*args, **kwargs):
            policy = kwargs["fetch_policy"]
            kwargs["fetch_policy"] = _LoopbackControlPolicy(
                overall_timeout=policy.overall_timeout,
                connection_timeout=policy.connection_timeout,
                read_timeout=policy.read_timeout,
                max_redirects=policy.max_redirects,
                max_retries=policy.max_retries,
            )
            kwargs["resolver"] = resolver
            return SafeHttpClient(*args, **kwargs)

        monkeypatch.setattr(
            arxiv_module, "SafeHttpClient", safe_client_factory, raising=False
        )
        original_get = requests.Session.get

        def guarded_get(session, *args, **kwargs):
            if session.__dict__.get("_comourat_fetch_client_owner") is None:
                raise AssertionError("arXiv bypassed SafeHttpClient")
            return original_get(session, *args, **kwargs)

        # Before the fix this guard fails locally instead of attempting DNS.
        monkeypatch.setattr(requests.Session, "get", guarded_get)

    return install


def _arxiv_deadline_client(**kwargs):
    settings = {
        "user_agent": "comourat-test/0.1",
        "regular_delay_seconds": 0,
        "max_retries": 0,
    }
    settings.update(kwargs)
    return ArxivClient(**settings)


def _observe_resolver_results(monkeypatch):
    result_put = threading.Event()
    original_queue = queue.Queue

    class ObservedQueue(original_queue):
        def put(self, *args, **kwargs):
            result = super().put(*args, **kwargs)
            result_put.set()
            return result

    monkeypatch.setattr("comourat.fetch.queue.Queue", ObservedQueue)
    return result_put


def test_arxiv_default_transport_uses_safe_http_client():
    client = ArxivClient(user_agent="comourat-test/0.1")

    assert isinstance(client._safe_client, SafeHttpClient)


def test_injected_safe_client_uses_arxiv_operation_deadline(
    server_factory, monkeypatch
):
    server = server_factory(_send_arxiv)
    release_resolver = threading.Event()

    def resolver(host, port):
        release_resolver.wait(1)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port))]

    safe_client = SafeHttpClient(
        fetch_policy=_LoopbackControlPolicy(overall_timeout=0.5),
        resolver=resolver,
    )
    monkeypatch.setattr(
        arxiv_module,
        "ARXIV_API_QUERY_URL",
        f"http://slow-dns.test:{server.server_port}/api/query",
    )
    resolver_result_put = _observe_resolver_results(monkeypatch)
    client = _arxiv_deadline_client(
        http_client=safe_client,
        overall_timeout_seconds=0.05,
    )

    started = time.monotonic()
    try:
        with pytest.raises(requests.Timeout):
            client.get_metadata("1706.03762")
        assert time.monotonic() - started < 0.25
    finally:
        release_resolver.set()
    assert resolver_result_put.wait(1)
    assert server.calls == 0


def test_arxiv_blocking_dns_uses_operation_deadline(
    arxiv_controlled_transport, server_factory, monkeypatch
):
    server = server_factory(_send_arxiv)
    resolver_started = threading.Event()
    release_resolver = threading.Event()

    def resolver(host, port):
        resolver_started.set()
        release_resolver.wait(1)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port))]

    arxiv_controlled_transport(resolver)
    monkeypatch.setattr(
        arxiv_module,
        "ARXIV_API_QUERY_URL",
        f"http://slow-dns.test:{server.server_port}/api/query",
    )
    resolver_result_put = _observe_resolver_results(monkeypatch)
    client = _arxiv_deadline_client(overall_timeout_seconds=0.05)

    started = time.monotonic()
    try:
        with pytest.raises(requests.Timeout):
            client.get_metadata("1706.03762")
        elapsed = time.monotonic() - started
        assert resolver_started.is_set()
        assert elapsed < 0.5
    finally:
        release_resolver.set()
    assert resolver_result_put.wait(1)
    assert server.calls == 0


def test_arxiv_dns_failures_use_existing_retry_policy(
    arxiv_controlled_transport, server_factory, monkeypatch
):
    server = server_factory(_send_arxiv)
    resolver_calls = []

    def resolver(host, port):
        resolver_calls.append((host, port))
        raise socket.gaierror("controlled DNS failure")

    arxiv_controlled_transport(resolver)
    monkeypatch.setattr(
        arxiv_module,
        "ARXIV_API_QUERY_URL",
        f"http://slow-dns.test:{server.server_port}/api/query",
    )
    client = _arxiv_deadline_client(
        overall_timeout_seconds=0.5,
        max_retries=2,
        backoff_seconds=0,
    )

    with pytest.raises(requests.ConnectionError):
        client.get_metadata("1706.03762")

    assert len(resolver_calls) == 3
    assert server.calls == 0


def test_arxiv_redirect_consumes_operation_deadline(
    arxiv_controlled_transport, server_factory, monkeypatch
):
    final_response_sent = threading.Event()

    def behavior(handler):
        time.sleep(0.2 if handler.path.startswith("/redirect") else 0.3)
        if handler.path.startswith("/redirect"):
            handler.send_response(302)
            handler.send_header("Location", "/slow")
            handler.send_header("Content-Length", "0")
            handler.send_header("Connection", "close")
            handler.end_headers()
            return
        final_response_sent.set()
        _send_arxiv(handler)

    server = server_factory(behavior)
    arxiv_controlled_transport()
    monkeypatch.setattr(
        arxiv_module,
        "ARXIV_API_QUERY_URL",
        f"http://127.0.0.2:{server.server_port}/redirect",
    )
    client = _arxiv_deadline_client(overall_timeout_seconds=0.35)

    started = time.monotonic()
    with pytest.raises(requests.Timeout):
        client.get_metadata("1706.03762")

    assert time.monotonic() - started < 0.55
    assert server.calls == 2
    assert not final_response_sent.is_set()


def test_arxiv_retries_share_one_operation_deadline(
    arxiv_controlled_transport, server_factory, monkeypatch
):
    final_response_sent = threading.Event()

    def behavior(handler):
        time.sleep(0.2 if handler.server.calls == 1 else 0.3)
        if handler.server.calls == 2:
            final_response_sent.set()
        _send_arxiv(handler, status=500 if handler.server.calls == 1 else 200)

    server = server_factory(behavior)
    arxiv_controlled_transport()
    monkeypatch.setattr(
        arxiv_module,
        "ARXIV_API_QUERY_URL",
        f"http://127.0.0.2:{server.server_port}/api/query",
    )
    client = _arxiv_deadline_client(
        overall_timeout_seconds=0.35,
        timeout_seconds=1,
        max_retries=1,
        backoff_seconds=0,
    )

    started = time.monotonic()
    with pytest.raises(requests.Timeout):
        client.get_metadata("1706.03762")

    assert time.monotonic() - started < 0.55
    assert server.calls == 2
    assert not final_response_sent.is_set()


def test_arxiv_read_timeout_remains_active_within_overall_budget(
    arxiv_controlled_transport, server_factory, monkeypatch
):
    def slow_headers(handler):
        time.sleep(0.1)
        _send_arxiv(handler)

    server = server_factory(slow_headers)
    arxiv_controlled_transport()
    monkeypatch.setattr(
        arxiv_module,
        "ARXIV_API_QUERY_URL",
        f"http://127.0.0.2:{server.server_port}/api/query",
    )
    client = _arxiv_deadline_client(
        overall_timeout_seconds=0.5,
        timeout_seconds=0.03,
    )

    started = time.monotonic()
    with pytest.raises(requests.Timeout):
        client.get_metadata("1706.03762")

    assert time.monotonic() - started < 0.3


def test_arxiv_tls_handshake_uses_connection_timeout(
    arxiv_controlled_transport, raw_server_factory, monkeypatch
):
    connection_started = threading.Event()

    def stall_tls(_connection):
        connection_started.set()
        time.sleep(0.3)

    server = raw_server_factory(stall_tls)
    arxiv_controlled_transport()
    monkeypatch.setattr(
        arxiv_module,
        "ARXIV_API_QUERY_URL",
        f"https://127.0.0.2:{server.server_address[1]}/api/query",
    )
    client = _arxiv_deadline_client(
        overall_timeout_seconds=0.4,
        timeout_seconds=0.03,
    )

    started = time.monotonic()
    with pytest.raises(requests.Timeout):
        client.get_metadata("1706.03762")

    assert time.monotonic() - started < 0.2
    assert connection_started.is_set()


def test_arxiv_trickled_response_cannot_extend_operation_deadline(
    arxiv_controlled_transport, server_factory, monkeypatch
):
    def trickle(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(_ARXIV_FEED)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        for offset in range(0, len(_ARXIV_FEED), 8):
            try:
                handler.wfile.write(_ARXIV_FEED[offset : offset + 8])
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(0.04)

    server = server_factory(trickle)
    arxiv_controlled_transport()
    monkeypatch.setattr(
        arxiv_module,
        "ARXIV_API_QUERY_URL",
        f"http://127.0.0.2:{server.server_port}/api/query",
    )
    client = _arxiv_deadline_client(
        overall_timeout_seconds=0.09,
        timeout_seconds=0.5,
    )

    started = time.monotonic()
    with pytest.raises(requests.Timeout):
        client.get_metadata("1706.03762")

    assert time.monotonic() - started < 0.3


def test_arxiv_valid_controlled_response_succeeds_within_budget(
    arxiv_controlled_transport, server_factory, monkeypatch
):
    server = server_factory(_send_arxiv)
    arxiv_controlled_transport()
    monkeypatch.setattr(
        arxiv_module,
        "ARXIV_API_QUERY_URL",
        f"http://127.0.0.2:{server.server_port}/api/query",
    )
    client = _arxiv_deadline_client(overall_timeout_seconds=0.5)

    result = client.get_metadata("1706.03762")

    assert result is not None
    assert result.identity.arxiv_id == "1706.03762"
    assert result.title == "A controlled title"
    assert result.abstract == "A controlled abstract"
    assert server.calls == 1
