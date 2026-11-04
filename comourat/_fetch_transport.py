"""Internal Requests/urllib3 transport for protected fetching."""

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


class _DeadlineTimeout(Timeout):
    """urllib3 timeout view that always returns the remaining overall time."""

    def __init__(self, deadline: _Deadline) -> None:
        self.deadline = deadline
        super().__init__(total=None, connect=None, read=None)

    def clone(self):
        return _DeadlineTimeout(self.deadline)

    @property
    def connect_timeout(self):
        try:
            return self.deadline.remaining()
        except _DeadlineExceeded:
            return 0

    @property
    def read_timeout(self):
        try:
            return self.deadline.remaining()
        except _DeadlineExceeded:
            return 0


class _DeadlineSSLContext:
    """Delegate SSLContext behavior and register sockets before handshakes."""

    def __init__(self, context, deadline: _Deadline) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_deadline", deadline)

    def __getattr__(self, name):
        return getattr(self._context, name)

    def __setattr__(self, name, value) -> None:
        if name in {"_context", "_deadline"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._context, name, value)

    def wrap_socket(self, sock, *args, **kwargs):
        do_handshake = kwargs.get("do_handshake_on_connect", True)
        kwargs["do_handshake_on_connect"] = False
        tls_socket = self._context.wrap_socket(sock, *args, **kwargs)
        self._deadline.register(tls_socket)
        if do_handshake:
            tls_socket.do_handshake()
        return tls_socket


def _tls_context(pool_kwargs, deadline: _Deadline):
    context = pool_kwargs.get("ssl_context")
    if context is None:
        context = create_urllib3_context(
            ssl_version=resolve_ssl_version(pool_kwargs.get("ssl_version")),
            ssl_minimum_version=pool_kwargs.get("ssl_minimum_version"),
            ssl_maximum_version=pool_kwargs.get("ssl_maximum_version"),
            cert_reqs=resolve_cert_reqs(pool_kwargs.get("cert_reqs")),
        )
    return _DeadlineSSLContext(context, deadline)


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
        self._socket.settimeout(self._deadline.remaining())

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


class _DeadlineSocket(socket.socket):
    """Socket that refreshes its timeout around blocking operations."""

    def __init__(self, *args, deadline: _Deadline, **kwargs) -> None:
        self._comourat_deadline = deadline
        super().__init__(*args, **kwargs)

    def _refresh(self) -> None:
        super().settimeout(self._comourat_deadline.remaining())

    def connect(self, address):
        self._refresh()
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
        self._refresh()
        return super().recv(*args, **kwargs)

    def recv_into(self, *args, **kwargs):
        self._refresh()
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
    def __init__(self, *args, proxy_sockaddr, deadline: _Deadline, **kwargs) -> None:
        self._comourat_deadline = deadline
        self._proxy_sockaddr = proxy_sockaddr
        super().__init__(*args, **kwargs)

    def _refresh(self) -> None:
        super().settimeout(self._comourat_deadline.remaining())

    def _proxy_addr(self):
        return self._proxy_sockaddr

    def connect(self, address):
        self._refresh()
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
        self._refresh()
        return super().recv(*args, **kwargs)

    def recv_into(self, *args, **kwargs):
        self._refresh()
        return super().recv_into(*args, **kwargs)

    def makefile(self, mode="r", buffering=None, *args, **kwargs):
        wrapped = super().makefile(mode, buffering, *args, **kwargs)
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
        raise NewConnectionError(self, f"SOCKS connection failed: {last_error}")


class _PinnedSOCKSConnection(_PinnedSOCKSConnectionMixin, SOCKSConnection):
    pass


class _PinnedSOCKSHTTPSConnection(_PinnedSOCKSConnectionMixin, SOCKSHTTPSConnection):
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

    def send(self, request, **kwargs):
        deadline = _current_deadline.get()
        if deadline is None:
            raise RuntimeError("protected adapter used outside SafeHttpClient")
        try:
            target = self.policy._parse_target(request.url)
            proxy_url = select_proxy(request.url, kwargs.get("proxies"))
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
            kwargs["timeout"] = _DeadlineTimeout(deadline)
            return super().send(request, **kwargs)
        except FetchPolicyError:
            raise
        except Exception as exc:
            if deadline.expired() or _contains_deadline_error(exc):
                raise FetchDeadlineExceeded("overall fetch deadline exceeded") from exc
            raise

    def get_connection(self, request, proxies=None):
        """Fail closed on Requests versions without the TLS-context hook."""
        raise FetchPolicyError("protected fetching requires Requests 2.32.0 or newer")

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        _host_params, pool_kwargs = self.build_connection_pool_key_attributes(
            request, verify, cert
        )
        state = request._comourat_fetch_state
        if state.proxy and state.proxy.target.scheme in {"http", "https"}:
            return self._http_proxy_pool(state, pool_kwargs)
        if state.proxy:
            return self._socks_proxy_pool(state, pool_kwargs)
        if state.target.scheme == "http":
            for key in SSL_KEYWORDS:
                pool_kwargs.pop(key, None)
        else:
            pool_kwargs["ssl_context"] = _tls_context(pool_kwargs, state.deadline)
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
        if pool_scheme == "http":
            for key in SSL_KEYWORDS:
                pool_kwargs.pop(key, None)
        else:
            pool_kwargs["ssl_context"] = _tls_context(pool_kwargs, state.deadline)
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
            _proxy_config=ProxyConfig(None, False, None, None),
            **pool_kwargs,
        )
        if state.target.scheme == "http" and proxy.target.scheme == "https":
            pool._comourat_cert_url = proxy.url  # type: ignore[union-attr]
        return pool

    def _socks_proxy_pool(self, state: _RequestState, pool_kwargs):
        proxy = state.proxy
        assert proxy is not None
        if state.target.scheme == "http":
            for key in SSL_KEYWORDS:
                pool_kwargs.pop(key, None)
        else:
            pool_kwargs["ssl_context"] = _tls_context(pool_kwargs, state.deadline)
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

    def cert_verify(self, conn, url, verify, cert) -> None:
        super().cert_verify(
            conn, getattr(conn, "_comourat_cert_url", url), verify, cert
        )
