"""Internal Requests/urllib3 transport for protected fetching."""

# This module intentionally integrates with Requests/urllib3 internal hooks.
# pylint: disable=protected-access

from __future__ import annotations

import http.client
import socket
import ssl
import time
from dataclasses import dataclass

import requests
import socks
from requests.utils import get_auth_from_url, select_proxy
from urllib3 import Timeout
from urllib3.connection import HTTPConnection, HTTPSConnection, ProxyConfig
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.contrib.socks import (
    SOCKSConnection,
    SOCKSHTTPConnectionPool,
    SOCKSHTTPSConnection,
    SOCKSHTTPSConnectionPool,
)
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
from urllib3.poolmanager import SSL_KEYWORDS
from urllib3.util import parse_url
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import (
    create_urllib3_context,
    resolve_cert_reqs,
    resolve_ssl_version,
)

from comourat.fetch import (
    Clock,
    FetchDeadlineExceeded,
    FetchPolicy,
    FetchPolicyError,
    Resolver,
    _current_deadline,
    _Deadline,
    _DeadlineExceeded,
    _Target,
)


def _contains_deadline_error(error: BaseException | None) -> bool:
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, _DeadlineExceeded):
            return True
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return False


def _read_timeout_remaining(deadline: _Deadline) -> float:
    remaining = deadline.remaining()
    read_timeout = getattr(deadline, "read_timeout", None)
    if read_timeout is None:
        return remaining
    return min(read_timeout, remaining)


class _DeadlineTimeout(Timeout):
    """Timeout view bounded by connection, read, and overall time."""

    def __init__(
        self, deadline: _Deadline, connection_timeout: float | None = None
    ) -> None:
        self.deadline = deadline
        self.connection_timeout = connection_timeout
        super().__init__(total=None, connect=None, read=None)

    def clone(self):
        return _DeadlineTimeout(self.deadline, self.connection_timeout)

    @property
    def connect_timeout(self):
        try:
            remaining = self.deadline.remaining()
        except _DeadlineExceeded:
            return 0
        if self.connection_timeout is None:
            return remaining
        return min(self.connection_timeout, remaining)

    @property
    def read_timeout(self):
        try:
            return _read_timeout_remaining(self.deadline)
        except _DeadlineExceeded:
            return 0


class _DeadlineSSLContext:
    """Delegate SSLContext behavior and register sockets before handshakes."""

    def __init__(
        self,
        context,
        deadline: _Deadline,
        connection_timeout: float | None = None,
    ) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_deadline", deadline)
        object.__setattr__(self, "_connection_timeout", connection_timeout)

    def __getattr__(self, name):
        return getattr(self._context, name)

    def __setattr__(self, name, value) -> None:
        if name in {"_context", "_deadline", "_connection_timeout"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._context, name, value)

    def wrap_socket(self, sock, *args, **kwargs):
        do_handshake = kwargs.get("do_handshake_on_connect", True)
        kwargs["do_handshake_on_connect"] = False
        tls_socket = self._context.wrap_socket(sock, *args, **kwargs)
        self._deadline.register(tls_socket)
        if do_handshake:
            if self._connection_timeout is not None:
                tls_socket.settimeout(
                    _connection_timeout_remaining(
                        self._deadline, self._connection_timeout
                    )
                )
            tls_socket.do_handshake()
            if self._connection_timeout is not None:
                tls_socket.settimeout(self._deadline.remaining())
        return tls_socket


def _tls_context(
    pool_kwargs,
    deadline: _Deadline,
    connection_timeout: float | None = None,
):
    # Requests reuses a process-global preloaded SSLContext. Never wrap or
    # mutate it: each target and proxy needs independent trust state.
    context = create_urllib3_context(
        ssl_version=resolve_ssl_version(pool_kwargs.get("ssl_version")),
        ssl_minimum_version=pool_kwargs.get("ssl_minimum_version"),
        ssl_maximum_version=pool_kwargs.get("ssl_maximum_version"),
        cert_reqs=resolve_cert_reqs(pool_kwargs.get("cert_reqs")),
    )
    return _DeadlineSSLContext(context, deadline, connection_timeout)


class _DeadlineRetry(Retry):
    """Retry delays that consume the current overall deadline."""

    @classmethod
    def from_retry(cls, retry: Retry):
        """Preserve Requests' retry settings in a deadline-aware copy."""
        return cls(
            total=retry.total,
            connect=retry.connect,
            read=retry.read,
            redirect=retry.redirect,
            status=retry.status,
            other=retry.other,
            allowed_methods=retry.allowed_methods,
            status_forcelist=retry.status_forcelist,
            backoff_factor=retry.backoff_factor,
            backoff_max=retry.backoff_max,
            retry_after_max=retry.retry_after_max,
            raise_on_redirect=retry.raise_on_redirect,
            raise_on_status=retry.raise_on_status,
            history=retry.history,
            remove_headers_on_redirect=retry.remove_headers_on_redirect,
            respect_retry_after_header=retry.respect_retry_after_header,
            backoff_jitter=retry.backoff_jitter,
        )

    @staticmethod
    def _sleep(duration: float) -> None:
        if duration <= 0:
            return
        deadline = _current_deadline.get()
        if deadline is None:
            time.sleep(duration)
            return
        if duration >= deadline.remaining():
            raise _DeadlineExceeded("retry delay exceeded the fetch deadline")
        time.sleep(duration)
        deadline.remaining()

    def _sleep_backoff(self) -> None:
        self._sleep(self.get_backoff_time())

    def sleep_for_retry(self, response) -> bool:
        retry_after = self.get_retry_after(response)
        if retry_after:
            self._sleep(retry_after)
            return True
        return False


class _DeadlineReader:
    """Refresh the socket deadline before every buffered response read."""

    def __init__(self, wrapped, sock, deadline: _Deadline) -> None:
        self._wrapped = wrapped
        self._socket = sock
        self._deadline = deadline

    def _refresh(self) -> None:
        self._socket.settimeout(_read_timeout_remaining(self._deadline))

    def read(self, *args, **kwargs):
        self._refresh()
        return self._wrapped.read(*args, **kwargs)

    def read1(self, *args, **kwargs):
        self._refresh()
        return self._wrapped.read1(*args, **kwargs)

    def readline(self, *args, **kwargs):
        self._refresh()
        return self._wrapped.readline(*args, **kwargs)

    def readinto(self, *args, **kwargs):
        self._refresh()
        return self._wrapped.readinto(*args, **kwargs)

    def readinto1(self, *args, **kwargs):
        self._refresh()
        return self._wrapped.readinto1(*args, **kwargs)

    def peek(self, *args, **kwargs):
        self._refresh()
        return self._wrapped.peek(*args, **kwargs)

    def close(self):
        return self._wrapped.close()

    @property
    def closed(self):
        return self._wrapped.closed

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    def __iter__(self):
        return self

    def __next__(self):
        self._refresh()
        return next(self._wrapped)


def _response_class(deadline: _Deadline):
    class DeadlineHTTPResponse(http.client.HTTPResponse):
        def __init__(self, sock, debuglevel=0, method=None, url=None) -> None:
            super().__init__(sock, debuglevel=debuglevel, method=method, url=url)
            self.fp = _DeadlineReader(self.fp, sock, deadline)  # type: ignore[assignment]

    return DeadlineHTTPResponse


def _connection_timeout_remaining(
    deadline: _Deadline, connection_timeout: float | None
) -> float:
    remaining = deadline.remaining()
    if connection_timeout is None:
        return remaining
    return min(connection_timeout, remaining)


def _is_connection_timeout(error: BaseException | None) -> bool:
    if isinstance(error, (socket.timeout, TimeoutError)):
        return True
    socket_error = getattr(error, "socket_err", None)
    return isinstance(socket_error, (socket.timeout, TimeoutError))


class _DeadlineSocket(socket.socket):
    """Socket that refreshes its timeout around blocking operations."""

    def __init__(
        self,
        *args,
        deadline: _Deadline,
        connection_timeout: float | None = None,
        **kwargs,
    ) -> None:
        self._comourat_deadline = deadline
        self._comourat_connection_timeout = connection_timeout
        super().__init__(*args, **kwargs)

    def _refresh(self) -> None:
        super().settimeout(self._comourat_deadline.remaining())

    def _refresh_read(self) -> None:
        super().settimeout(_read_timeout_remaining(self._comourat_deadline))

    def _refresh_connection(self) -> None:
        super().settimeout(
            _connection_timeout_remaining(
                self._comourat_deadline, self._comourat_connection_timeout
            )
        )

    def connect(self, address):
        self._refresh_connection()
        result = super().connect(address)
        self._refresh()
        return result

    def send(self, *args, **kwargs):
        self._refresh()
        return super().send(*args, **kwargs)

    def sendall(self, *args, **kwargs):
        self._refresh()
        return super().sendall(*args, **kwargs)

    def recv(self, *args, **kwargs):
        self._refresh_read()
        return super().recv(*args, **kwargs)

    def recv_into(self, *args, **kwargs):
        self._refresh_read()
        return super().recv_into(*args, **kwargs)


class _PinnedConnectionMixin:
    def __init__(
        self,
        *args,
        resolved_addresses,
        deadline: _Deadline,
        **kwargs,
    ) -> None:
        self._resolved_addresses = resolved_addresses
        self._comourat_deadline = deadline
        super().__init__(*args, **kwargs)
        self.response_class = _response_class(deadline)

    def _new_conn(self):
        last_error = None
        for family, socktype, proto, _canonname, sockaddr in self._resolved_addresses:
            sock = None
            try:
                self._comourat_deadline.remaining()
                sock = _DeadlineSocket(
                    family,
                    socktype,
                    proto,
                    deadline=self._comourat_deadline,
                    connection_timeout=self.timeout,
                )
                self._comourat_deadline.register(sock)
                if self.socket_options:
                    for option in self.socket_options:
                        sock.setsockopt(*option)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sockaddr)
                return sock
            except (OSError, TypeError, UnicodeError, _DeadlineExceeded) as exc:
                last_error = exc
                if sock is not None:
                    sock.close()
                if self._comourat_deadline.expired():
                    break
        if self._comourat_deadline.expired():
            raise ConnectTimeoutError(self, "overall fetch deadline exceeded")
        if _is_connection_timeout(last_error):
            raise ConnectTimeoutError(
                self, "connection timeout exceeded"
            ) from last_error
        raise NewConnectionError(self, f"failed to connect: {last_error}")


class _PinnedHTTPConnection(_PinnedConnectionMixin, HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnectionMixin, HTTPSConnection):
    pass


class _PinnedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _PinnedHTTPConnection


class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


class _PinnedSocksSocket(socks.socksocket):
    def __init__(
        self,
        *args,
        proxy_sockaddr,
        deadline: _Deadline,
        connection_timeout: float | None = None,
        **kwargs,
    ) -> None:
        self._comourat_deadline = deadline
        self._comourat_connection_timeout = connection_timeout
        self._proxy_sockaddr = proxy_sockaddr
        super().__init__(*args, **kwargs)

    def _refresh(self) -> None:
        super().settimeout(self._comourat_deadline.remaining())

    def _refresh_read(self) -> None:
        super().settimeout(_read_timeout_remaining(self._comourat_deadline))

    def _refresh_connection(self) -> None:
        super().settimeout(
            _connection_timeout_remaining(
                self._comourat_deadline, self._comourat_connection_timeout
            )
        )

    def _proxy_addr(self):
        return self._proxy_sockaddr

    def connect(self, dest_pair, catch_errors=None):
        self._refresh_connection()
        result = super().connect(dest_pair, catch_errors)
        self._refresh()
        return result

    def send(self, *args, **kwargs):
        self._refresh()
        return super().send(*args, **kwargs)

    def sendall(self, *args, **kwargs):
        self._refresh()
        return super().sendall(*args, **kwargs)

    def recv(self, *args, **kwargs):
        self._refresh_read()
        return super().recv(*args, **kwargs)

    def recv_into(self, *args, **kwargs):
        self._refresh_read()
        return super().recv_into(*args, **kwargs)

    def makefile(self, mode="r", buffering=None, **kwargs):
        wrapped = super().makefile(mode, buffering, **kwargs)
        if "r" in mode:
            return _DeadlineReader(wrapped, self, self._comourat_deadline)
        return wrapped


class _PinnedSOCKSConnectionMixin(_PinnedConnectionMixin):
    def __init__(self, *args, target_answers, **kwargs) -> None:
        self._target_answers = target_answers
        super().__init__(*args, **kwargs)

    def _new_conn(self):
        options = self._socks_options
        if options["rdns"] and not self._target_answers:
            targets = [(self.host, self.port)]
        else:
            targets = [
                (answer[4][0], self.port)
                for answer in self._target_answers
                if options["socks_version"] != socks.PROXY_TYPE_SOCKS4
                or answer[0] == socket.AF_INET
            ]
        last_error = None
        for (
            family,
            socktype,
            proto,
            _canonname,
            proxy_sockaddr,
        ) in self._resolved_addresses:
            for target_host, target_port in targets:
                sock = None
                try:
                    self._comourat_deadline.remaining()
                    sock = _PinnedSocksSocket(
                        family,
                        socktype,
                        proto,
                        proxy_sockaddr=proxy_sockaddr,
                        deadline=self._comourat_deadline,
                        connection_timeout=self.timeout,
                    )
                    self._comourat_deadline.register(sock)
                    if self.socket_options:
                        for option in self.socket_options:
                            sock.setsockopt(*option)
                    if self.source_address:
                        sock.bind(self.source_address)
                    sock.set_proxy(
                        options["socks_version"],
                        options["proxy_host"],
                        options["proxy_port"],
                        options["rdns"],
                        options["username"],
                        options["password"],
                    )
                    sock.connect((target_host, target_port))
                    return sock
                except (
                    OSError,
                    TypeError,
                    UnicodeError,
                    socks.ProxyError,
                    _DeadlineExceeded,
                ) as exc:
                    last_error = exc
                    if sock is not None:
                        sock.close()
                    if self._comourat_deadline.expired():
                        break
        if self._comourat_deadline.expired():
            raise ConnectTimeoutError(self, "overall fetch deadline exceeded")
        if _is_connection_timeout(last_error):
            raise ConnectTimeoutError(
                self, "connection timeout exceeded"
            ) from last_error
        raise NewConnectionError(self, f"SOCKS connection failed: {last_error}")


class _PinnedSOCKSConnection(_PinnedSOCKSConnectionMixin, SOCKSConnection):
    pass


class _PinnedSOCKSHTTPSConnection(  # pylint: disable=too-many-ancestors
    _PinnedSOCKSConnectionMixin, SOCKSHTTPSConnection
):
    pass


class _PinnedSOCKSHTTPConnectionPool(SOCKSHTTPConnectionPool):
    ConnectionCls = _PinnedSOCKSConnection


class _PinnedSOCKSHTTPSConnectionPool(SOCKSHTTPSConnectionPool):
    ConnectionCls = _PinnedSOCKSHTTPSConnection


@dataclass(frozen=True)
class _ProxyState:
    target: _Target
    answers: tuple
    url: str


@dataclass(frozen=True)
class _RequestState:
    target: _Target
    target_answers: tuple
    deadline: _Deadline
    proxy: _ProxyState | None = None


class _SafeHTTPAdapter(requests.adapters.HTTPAdapter):
    def __init__(
        self, *, policy: FetchPolicy, resolver: Resolver, clock: Clock, **kwargs
    ):
        self.policy = policy
        self.resolver = resolver
        self.clock = clock
        super().__init__(**kwargs)
        if isinstance(self.max_retries, Retry) and not isinstance(
            self.max_retries, _DeadlineRetry
        ):
            self.max_retries = _DeadlineRetry.from_retry(self.max_retries)

    def send(
        self,
        request,
        stream=False,
        timeout=None,
        verify=True,
        cert=None,
        proxies=None,
    ):
        deadline = _current_deadline.get()
        if deadline is None:
            raise RuntimeError("protected adapter used outside SafeHttpClient")
        try:
            target = self.policy._parse_target(request.url)
            proxy_url = select_proxy(request.url, proxies)
            proxy_state = None
            if proxy_url:
                proxy_target = self.policy._parse_target(
                    proxy_url,
                    allowed_schemes=("http", "https", "socks4", "socks5", "socks5h"),
                    proxy=True,
                )
                proxy_answers = self.policy._resolve(
                    proxy_target, self.resolver, deadline, proxy=True
                )
                if proxy_target.scheme.startswith("socks"):
                    # SOCKS credentials belong in the proxy handshake. Requests
                    # can add a Basic header on plain-HTTP redirects; it must
                    # never be forwarded through the tunnel to the origin.
                    request.headers.pop("Proxy-Authorization", None)
                proxy_state = _ProxyState(
                    target=proxy_target, answers=proxy_answers, url=proxy_url
                )
                if target.literal is not None or proxy_target.scheme in {
                    "socks5",
                    "socks4",
                }:
                    target_answers = self.policy._resolve(
                        target, self.resolver, deadline
                    )
                else:
                    target_answers = ()
            else:
                target_answers = self.policy._resolve(target, self.resolver, deadline)
            if (
                proxy_state
                and proxy_state.target.scheme == "socks4"
                and not any(answer[0] == socket.AF_INET for answer in target_answers)
            ):
                raise FetchPolicyError("SOCKS4 supports only IPv4 targets")
            request._comourat_fetch_state = _RequestState(
                target=target,
                target_answers=target_answers,
                deadline=deadline,
                proxy=proxy_state,
            )
            timeout = _DeadlineTimeout(deadline, self.policy.connection_timeout)
            return super().send(
                request,
                stream=stream,
                timeout=timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
            )
        except FetchPolicyError:
            raise
        except Exception as exc:
            if deadline.expired() or _contains_deadline_error(exc):
                raise FetchDeadlineExceeded("overall fetch deadline exceeded") from exc
            raise

    def get_connection(self, url, proxies=None):
        """Fail closed on Requests versions without the TLS-context hook."""
        raise FetchPolicyError("protected fetching requires Requests 2.32.3 or newer")

    def _get_connection(self, request, verify, proxies=None, cert=None):
        """Fail closed on Requests 2.32.0 and 2.32.1."""
        raise FetchPolicyError("protected fetching requires Requests 2.32.3 or newer")

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        builder = getattr(self, "build_connection_pool_key_attributes", None)
        if builder is None:
            raise FetchPolicyError(
                "protected fetching requires Requests 2.32.3 or newer"
            )
        _host_params, pool_kwargs = builder(request, verify, cert)
        state = request._comourat_fetch_state
        if state.proxy and state.proxy.target.scheme in {"http", "https"}:
            return self._http_proxy_pool(state, pool_kwargs)
        if state.proxy:
            return self._socks_proxy_pool(state, pool_kwargs)
        if state.target.scheme == "http":
            for key in SSL_KEYWORDS:
                pool_kwargs.pop(key, None)
        else:
            pool_kwargs["ssl_context"] = _tls_context(
                pool_kwargs, state.deadline, self.policy.connection_timeout
            )
        pool_class = (
            _PinnedHTTPSConnectionPool
            if state.target.scheme == "https"
            else _PinnedHTTPConnectionPool
        )
        return pool_class(
            host=state.target.host,
            port=state.target.port,
            resolved_addresses=state.target_answers,
            deadline=state.deadline,
            **pool_kwargs,
        )

    def _http_proxy_pool(self, state: _RequestState, pool_kwargs):
        proxy = state.proxy
        assert proxy is not None
        pool_scheme = "https" if state.target.scheme == "https" else proxy.target.scheme
        pool_host = (
            state.target.host if state.target.scheme == "https" else proxy.target.host
        )
        pool_port = (
            state.target.port if state.target.scheme == "https" else proxy.target.port
        )
        proxy_ssl_context = None
        if pool_scheme == "http":
            for key in SSL_KEYWORDS:
                pool_kwargs.pop(key, None)
        else:
            if proxy.target.scheme == "https":
                proxy_ssl_context = _tls_context(
                    pool_kwargs, state.deadline, self.policy.connection_timeout
                )
            pool_kwargs["ssl_context"] = _tls_context(
                pool_kwargs, state.deadline, self.policy.connection_timeout
            )
        pool_class = (
            _PinnedHTTPSConnectionPool
            if pool_scheme == "https"
            else _PinnedHTTPConnectionPool
        )
        parsed_proxy = parse_url(proxy.url)._replace(port=proxy.target.port)
        pool = pool_class(
            host=pool_host,
            port=pool_port,
            resolved_addresses=proxy.answers,
            deadline=state.deadline,
            _proxy=parsed_proxy,
            _proxy_headers=self.proxy_headers(proxy.url),
            _proxy_config=ProxyConfig(proxy_ssl_context, False, None, None),
            **pool_kwargs,
        )
        if state.target.scheme == "http" and proxy.target.scheme == "https":
            # pylint: disable-next=attribute-defined-outside-init
            pool._comourat_cert_url = proxy.url  # type: ignore[union-attr]
        return pool

    def _socks_proxy_pool(self, state: _RequestState, pool_kwargs):
        proxy = state.proxy
        assert proxy is not None
        if state.target.scheme == "http":
            for key in SSL_KEYWORDS:
                pool_kwargs.pop(key, None)
        else:
            pool_kwargs["ssl_context"] = _tls_context(
                pool_kwargs, state.deadline, self.policy.connection_timeout
            )
        username, password = get_auth_from_url(proxy.url)
        socks_options = {
            "socks_version": (
                socks.PROXY_TYPE_SOCKS4
                if proxy.target.scheme == "socks4"
                else socks.PROXY_TYPE_SOCKS5
            ),
            "proxy_host": proxy.target.host,
            "proxy_port": proxy.target.port,
            "username": username,
            "password": password,
            "rdns": proxy.target.scheme == "socks5h",
        }
        pool_class = (
            _PinnedSOCKSHTTPSConnectionPool
            if state.target.scheme == "https"
            else _PinnedSOCKSHTTPConnectionPool
        )
        return pool_class(
            host=state.target.host,
            port=state.target.port,
            resolved_addresses=proxy.answers,
            target_answers=state.target_answers,
            deadline=state.deadline,
            _socks_options=socks_options,
            **pool_kwargs,
        )

    @staticmethod
    def _configure_context_trust(context, conn) -> None:
        if conn.cert_reqs == "CERT_NONE":
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return
        # urllib3 ignores pool CA settings for an explicit proxy context, and
        # Requests can supply no CA attributes for a default target.
        # Initialize both fresh contexts explicitly in every case.
        if getattr(conn, "ca_certs", None):
            context.load_verify_locations(conn.ca_certs)
        elif getattr(conn, "ca_cert_dir", None):
            context.load_verify_locations(capath=conn.ca_cert_dir)
        else:
            context.load_verify_locations(requests.adapters.DEFAULT_CA_BUNDLE_PATH)
        context.verify_mode = ssl.CERT_REQUIRED

    def cert_verify(self, conn, url, verify, cert) -> None:
        super().cert_verify(
            conn, getattr(conn, "_comourat_cert_url", url), verify, cert
        )
        target_context = getattr(conn, "conn_kw", {}).get("ssl_context")
        if target_context is not None:
            self._configure_context_trust(target_context, conn)
        proxy_config = getattr(conn, "proxy_config", None)
        proxy_context = getattr(proxy_config, "ssl_context", None)
        if proxy_context is not None and proxy_context is not target_context:
            self._configure_context_trust(proxy_context, conn)
