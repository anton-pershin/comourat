"""Policy-controlled HTTP fetching with explicit DNS and proxy boundaries."""

# Internal policy methods are shared with the package transport module.
# pylint: disable=protected-access,cyclic-import

from __future__ import annotations

import contextvars
import ipaddress
import math
import queue
import re
import socket
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import MethodType
from typing import Any
from urllib.parse import urlsplit

import idna
import requests

from comourat.proxy import ProxyConfigurationError, validate_proxy_url

Resolver = Callable[[str, int], Iterable[tuple]]
Clock = Callable[[], float]

_TRUSTED_PROXY_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


class FetchError(requests.RequestException):
    """Base class for protected-fetch failures."""


class FetchPolicyError(FetchError):
    """A URL or destination violates the configured fetch policy."""


class FetchResolutionError(requests.ConnectionError, FetchError):
    """A hostname cannot be resolved into a usable answer set."""


class FetchDeadlineExceeded(requests.Timeout, FetchError):
    """The overall monotonic deadline for a fetch has expired."""


class _DeadlineExceeded(TimeoutError):
    pass


class _Deadline:
    """Monotonic deadline with a watchdog that aborts registered sockets."""

    def __init__(
        self,
        timeout: float,
        clock: Clock,
        started: float,
        read_timeout: float | None = None,
    ) -> None:
        self.timeout = timeout
        self.clock = clock
        self.started = started
        self.read_timeout = read_timeout
        self._lock = threading.Lock()
        self._sockets: set[Any] = set()
        self._active = True
        self._timed_out = False
        self._timer = threading.Timer(timeout, self._abort)
        self._timer.daemon = True
        self._timer.start()

    @classmethod
    def start(
        cls,
        timeout: float,
        clock: Clock,
        read_timeout: float | None = None,
    ) -> _Deadline:
        return cls(
            timeout=timeout,
            clock=clock,
            started=clock(),
            read_timeout=read_timeout,
        )

    def remaining(self) -> float:
        remaining = self.timeout - (self.clock() - self.started)
        if self._timed_out or remaining <= 0:
            raise _DeadlineExceeded("overall fetch deadline exceeded")
        return remaining

    def expired(self) -> bool:
        return self._timed_out or self.clock() - self.started >= self.timeout

    def register(self, sock) -> None:
        """Register a real or TLS socket for mandatory deadline abortion."""
        with self._lock:
            if not self._active or self.expired():
                close_now = True
            else:
                self._sockets.add(sock)
                close_now = False
        if close_now:
            self._close_socket(sock)
            raise _DeadlineExceeded("overall fetch deadline exceeded")

    @staticmethod
    def _close_socket(sock) -> None:
        try:
            shutdown = getattr(sock, "shutdown", None)
            if shutdown is not None:
                shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError, RuntimeError):
            pass
        try:
            sock.close()
        except (OSError, ValueError, RuntimeError):
            pass

    def _abort(self) -> None:
        with self._lock:
            if not self._active:
                return
            self._timed_out = True
            self._active = False
            sockets = tuple(self._sockets)
            self._sockets.clear()
        for sock in sockets:
            self._close_socket(sock)

    def close(self) -> None:
        self._timer.cancel()
        with self._lock:
            self._active = False
            self._sockets.clear()


_current_deadline: contextvars.ContextVar[_Deadline | None] = contextvars.ContextVar(
    "comourat_fetch_deadline", default=None
)


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None

    @property
    def display_host(self) -> str:
        return f"[{self.host}]" if ":" in self.host else self.host


def _redact_url(url: str) -> str:
    """Return only scheme and host for safe logs and error messages."""
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            return "<redacted URL>"
        host = parts.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port is not None else ""
        return f"{parts.scheme}://{host}{port}"
    except (TypeError, ValueError):
        return "<redacted URL>"


def _redact_urls_in_text(text: str) -> str:
    redacted = re.sub(
        r"https?://[^\s'\"<>]+",
        lambda match: _redact_url(match.group(0)),
        text,
    )
    return re.sub(
        r"(?i)(\burl\s*[:=]\s*['\"]?)/[^\s'\"<>]+",
        r"\1<redacted>",
        redacted,
    )


def _sanitize_exception(error: BaseException) -> None:
    """Remove URL credentials, paths, and queries from exception chains."""
    seen = set()

    def sanitize(current: BaseException | None) -> None:
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        current.args = tuple(
            _redact_urls_in_text(argument) if isinstance(argument, str) else argument
            for argument in current.args
        )
        sanitize(current.__cause__)
        sanitize(current.__context__)

    sanitize(error)


class FetchPolicy:
    """Destination, redirect, retry, and overall-deadline rules for fetching.

    ``deny_hosts`` contains exact hostnames. ``deny_addresses`` accepts IP
    addresses and CIDR networks. The built-in target policy permits global
    unicast addresses only. An explicitly configured proxy may use a loopback
    or private address, but the same caller deny rules apply to it.
    ``connection_timeout`` optionally caps each connection attempt.
    ``read_timeout`` optionally caps each response read. Neither limit exceeds
    the remaining overall time. Their default ``None`` preserves the
    overall-timeout-only behavior.
    """

    def __init__(
        self,
        *,
        deny_hosts: Iterable[str] = (),
        deny_addresses: Iterable[str] = (),
        overall_timeout: float = 30.0,
        connection_timeout: float | None = None,
        read_timeout: float | None = None,
        max_redirects: int = 30,
        max_retries: int = 0,
    ) -> None:
        if isinstance(overall_timeout, bool) or not isinstance(
            overall_timeout, (int, float)
        ):
            raise ValueError("overall timeout must be a positive finite number")
        if not math.isfinite(overall_timeout) or overall_timeout <= 0:
            raise ValueError("overall timeout must be a positive finite number")
        if connection_timeout is not None and (
            isinstance(connection_timeout, bool)
            or not isinstance(connection_timeout, (int, float))
            or not math.isfinite(connection_timeout)
            or connection_timeout <= 0
        ):
            raise ValueError("connection timeout must be a positive finite number")
        if read_timeout is not None and (
            isinstance(read_timeout, bool)
            or not isinstance(read_timeout, (int, float))
            or not math.isfinite(read_timeout)
            or read_timeout <= 0
        ):
            raise ValueError("read timeout must be a positive finite number")
        if (
            isinstance(max_redirects, bool)
            or not isinstance(max_redirects, int)
            or max_redirects < 0
        ):
            raise ValueError("max redirects must be a non-negative integer")
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries < 0
        ):
            raise ValueError("max retries must be a non-negative integer")
        self.deny_hosts = frozenset(self._normalize_host(host) for host in deny_hosts)
        networks = []
        for value in deny_addresses:
            try:
                networks.append(ipaddress.ip_network(value, strict=False))
            except ValueError as exc:
                raise ValueError(
                    f"invalid denied IP address or network: {value!r}"
                ) from exc
        self.deny_networks = tuple(networks)
        self.overall_timeout = float(overall_timeout)
        self.connection_timeout = (
            None if connection_timeout is None else float(connection_timeout)
        )
        self.read_timeout = None if read_timeout is None else float(read_timeout)
        self.max_redirects = max_redirects
        self.max_retries = max_retries

    @staticmethod
    def _normalize_host(host: str) -> str:
        if not isinstance(host, str):
            raise ValueError("hostname must be a string")
        candidate = host.strip().rstrip(".")
        if not candidate:
            raise ValueError("hostname is empty")
        if candidate.startswith("[") and candidate.endswith("]"):
            candidate = candidate[1:-1]
        try:
            ipaddress.ip_address(candidate)
            return candidate.lower()
        except ValueError:
            pass
        try:
            return idna.encode(candidate, uts46=True).decode("ascii").lower()
        except (idna.IDNAError, UnicodeError) as exc:
            raise ValueError(f"invalid hostname: {host!r}") from exc

    def _parse_target(
        self,
        url: str,
        allowed_schemes: tuple[str, ...] = ("http", "https"),
        *,
        proxy: bool = False,
    ) -> _Target:
        if not isinstance(url, str) or any(ord(char) < 32 for char in url):
            raise FetchPolicyError("target URL is not valid")
        try:
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            if scheme not in allowed_schemes:
                raise FetchPolicyError(f"target URL scheme {scheme!r} is not supported")
            hostname = parts.hostname
            if not hostname:
                raise FetchPolicyError("target URL has no host")
            host = self._normalize_host(hostname)
            port = parts.port
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, FetchPolicyError):
                raise
            raise FetchPolicyError("target URL is not valid") from exc
        if port is None:
            ports = {
                "http": 80,
                "https": 443,
                "socks4": 1080,
                "socks5": 1080,
                "socks5h": 1080,
            }
            port = ports[scheme]
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        target = _Target(scheme=scheme, host=host, port=port, literal=literal)
        self._validate_target_rules(target, proxy=proxy)
        return target

    def _validate_target_rules(self, target: _Target, *, proxy: bool = False) -> None:
        if target.host in self.deny_hosts:
            raise FetchPolicyError(f"target host is denied: {target.display_host}")
        if target.literal is not None:
            self._validate_address(target.literal, proxy=proxy)

    def _candidate_addresses(self, address):
        candidates = [address]
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            candidates.append(mapped)
        return candidates

    def _matches_caller_denial(self, address) -> bool:
        return any(
            candidate in network
            for candidate in self._candidate_addresses(address)
            for network in self.deny_networks
        )

    @staticmethod
    def _has_builtin_denial(address) -> bool:
        """Return whether an address is prohibited for an untrusted target."""
        return (
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
            or address.is_reserved
        )

    def _validate_address(self, address, *, proxy: bool) -> None:
        candidates = self._candidate_addresses(address)
        if any(self._matches_caller_denial(candidate) for candidate in candidates):
            raise FetchPolicyError("destination matches a caller deny rule")
        for candidate in candidates:
            trusted_private_proxy = any(
                candidate in network for network in _TRUSTED_PROXY_PRIVATE_NETWORKS
            )
            if proxy and (candidate.is_loopback or trusted_private_proxy):
                continue
            if self._has_builtin_denial(candidate):
                kind = "proxy" if proxy else "target"
                raise FetchPolicyError(
                    f"{kind} destination address is prohibited: {candidate}"
                )

    @staticmethod
    def _answer_for_literal(target: _Target) -> tuple:
        address = target.literal
        if address is None:
            raise ValueError("target is not an IP literal")
        sockaddr: tuple[Any, ...]
        if address.version == 4:
            sockaddr = (str(address), target.port)
            family = socket.AF_INET
        else:
            sockaddr = (str(address), target.port, 0, 0)
            family = socket.AF_INET6
        return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)

    def _validate_answers(self, answers: Iterable[tuple], *, proxy: bool) -> tuple:
        normalized = []
        seen = set()
        for answer in answers:
            try:
                family, socktype, proto, canonname, sockaddr = answer
                if family not in (socket.AF_INET, socket.AF_INET6):
                    raise FetchPolicyError("resolved address family is not supported")
                address = ipaddress.ip_address(sockaddr[0])
                expected_length = 2 if family == socket.AF_INET else 4
                expected_version = 4 if family == socket.AF_INET else 6
                if (
                    len(sockaddr) != expected_length
                    or address.version != expected_version
                    or socktype != socket.SOCK_STREAM
                ):
                    raise FetchResolutionError("resolver returned an invalid answer")
                port = sockaddr[1]
                if (
                    isinstance(port, bool)
                    or not isinstance(port, int)
                    or not 0 <= port <= 65535
                ):
                    raise FetchResolutionError("resolver returned an invalid port")
                self._validate_address(address, proxy=proxy)
            except (ValueError, IndexError, TypeError) as exc:
                if isinstance(exc, FetchPolicyError):
                    raise
                raise FetchResolutionError(
                    "resolver returned an invalid answer"
                ) from exc
            marker = (family, sockaddr)
            if marker not in seen:
                seen.add(marker)
                normalized.append((family, socktype, proto, canonname, sockaddr))
        if not normalized:
            raise FetchResolutionError("resolver returned no addresses")
        return tuple(normalized)

    def _resolve(
        self,
        target: _Target,
        resolver: Resolver,
        deadline: _Deadline,
        *,
        proxy: bool = False,
    ) -> tuple:
        if target.literal is not None:
            answer = self._answer_for_literal(target)
            return self._validate_answers([answer], proxy=proxy)

        # Python's blocking getaddrinfo has no timeout. Run only the resolver in
        # a daemon thread; no connection work occurs there, and a late answer
        # cannot cause a socket to be created after the deadline.
        results: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def resolve_worker() -> None:
            try:
                answers = tuple(resolver(target.host, target.port))
                results.put((True, answers))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # Return resolver failures to the requesting thread.
                results.put((False, exc))

        thread = threading.Thread(target=resolve_worker, daemon=True)
        thread.start()
        try:
            succeeded, value = results.get(timeout=deadline.remaining())
        except queue.Empty as exc:
            raise _DeadlineExceeded(
                "DNS resolution exceeded the fetch deadline"
            ) from exc
        deadline.remaining()
        if not succeeded:
            if isinstance(value, _DeadlineExceeded):
                raise value
            raise FetchResolutionError(
                f"could not resolve {target.display_host}: {value}"
            ) from value
        return self._validate_answers(value, proxy=proxy)


class SafeHttpClient:
    """Requests-compatible GET/POST client with an enforced fetch policy."""

    def __init__(
        self,
        http_session=None,
        *,
        fetch_policy: FetchPolicy | None = None,
        proxy: str | None = None,
        resolver: Resolver | None = None,
        clock: Clock | None = None,
    ) -> None:
        from comourat._fetch_transport import (  # pylint: disable=import-outside-toplevel
            _SafeHTTPAdapter,
        )

        self.policy = fetch_policy or FetchPolicy()
        self.session = http_session if http_session is not None else requests.Session()
        owner_key = "_comourat_fetch_client_owner"
        if self.session.__dict__.get(owner_key) is not None:
            raise ValueError(
                "HTTP session already belongs to a protected client; "
                "use a separate session for each policy"
            )
        self.session.__dict__[owner_key] = id(self)
        self.resolver = resolver or self._system_resolver
        self.clock = clock or time.monotonic
        self.session.trust_env = False
        self.proxy = self._configure_proxies(proxy)
        self.session.max_redirects = self.policy.max_redirects
        adapter = _SafeHTTPAdapter(
            policy=self.policy,
            resolver=self.resolver,
            clock=self.clock,
            max_retries=self.policy.max_retries,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self._adapter = adapter
        self._install_get_adapter()

    def _install_get_adapter(self) -> None:
        """Make policy validation authoritative over Requests mount prefixes."""
        adapter = self._adapter

        def get_adapter(_session, url):
            self.policy._parse_target(url)
            return adapter

        self.session.get_adapter = MethodType(get_adapter, self.session)  # type: ignore[method-assign]

    @staticmethod
    def _system_resolver(host: str, port: int):
        return socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)

    def _configure_proxies(self, proxy: str | None) -> str | None:
        try:
            configured = dict(self.session.proxies or {})
        except (TypeError, ValueError) as exc:
            raise ProxyConfigurationError("session proxies must be a mapping") from exc
        if any(key.lower() in {"no", "no_proxy"} for key in configured):
            raise ProxyConfigurationError("proxy bypass rules are not supported")
        if proxy:
            configured = {"http": proxy, "https": proxy}
        elif "all" in configured:
            configured["http"] = configured["all"]
            configured["https"] = configured["all"]
        validated = {}
        for scheme in ("http", "https"):
            value = configured.get(scheme)
            if value:
                validated[scheme] = validate_proxy_url(value)
        self.session.proxies.clear()
        self.session.proxies.update(validated)
        values = set(validated.values())
        return values.pop() if len(values) == 1 else None

    def _call(
        self,
        method,
        url: str,
        *,
        _deadline: _Deadline | None = None,
        **kwargs,
    ):
        self.policy._parse_target(url)
        self._install_get_adapter()
        if "proxies" in kwargs:
            raise ProxyConfigurationError(
                "per-request proxies are not supported; configure the client"
            )
        owns_deadline = _deadline is None
        deadline = (
            _Deadline.start(
                self.policy.overall_timeout,
                self.clock,
                read_timeout=self.policy.read_timeout,
            )
            if owns_deadline
            else _deadline
        )
        assert deadline is not None
        token = _current_deadline.set(deadline)
        kwargs["timeout"] = self.policy.overall_timeout
        kwargs.setdefault("allow_redirects", True)
        try:
            deadline.remaining()
            response = method(url, **kwargs)
            deadline.remaining()
            return response
        except FetchPolicyError:
            raise
        except Exception as exc:
            _sanitize_exception(exc)
            if deadline.expired():
                raise FetchDeadlineExceeded("overall fetch deadline exceeded") from exc
            raise
        finally:
            _current_deadline.reset(token)
            if owns_deadline:
                deadline.close()

    def get(self, url: str, **kwargs):
        return self._call(self.session.get, url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._call(self.session.post, url, **kwargs)
