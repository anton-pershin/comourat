"""Proxy URL validation shared by the HTTP clients and the page fetcher.

A malformed proxy URL makes every request fail immediately. When a pipeline
drops items on fetch failure, that looks like "no content anywhere". The
validation turns the mistake into an error at construction time.
"""

from __future__ import annotations

import importlib.util
from urllib.parse import urlsplit

# Schemes requests can use as an HTTP proxy.
SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h", "socks4"}

# Schemes that need the PySocks package in the environment.
SOCKS_PROXY_SCHEMES = {"socks5", "socks5h", "socks4"}


class ProxyConfigurationError(ValueError):
    """The configured proxy URL cannot be used by requests."""


def validate_proxy_url(proxy: str) -> str:
    """Return the proxy URL when usable, otherwise raise.

    Raises `ProxyConfigurationError` for an empty URL, an unsupported
    scheme, a URL without a host, or a SOCKS proxy in an environment
    without PySocks installed.
    """
    if not proxy or not proxy.strip():
        raise ProxyConfigurationError("proxy URL is empty")
    parts = urlsplit(proxy.strip())
    if parts.scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ProxyConfigurationError(
            f"proxy URL scheme {parts.scheme!r} is not supported; "
            f"expected one of {sorted(SUPPORTED_PROXY_SCHEMES)}"
        )
    if not parts.hostname:
        raise ProxyConfigurationError("proxy URL has no host")
    if parts.scheme in SOCKS_PROXY_SCHEMES and not _socks_support_available():
        raise ProxyConfigurationError(
            f"proxy URL scheme {parts.scheme!r} needs the PySocks package, "
            "which is not installed in this environment "
            "(requests reports 'Missing dependencies for SOCKS support')"
        )
    return proxy.strip()


def _socks_support_available() -> bool:
    """True when the environment can use SOCKS proxies."""
    return importlib.util.find_spec("socks") is not None
