"""Tests for proxy validation and loud failure on a broken proxy.

The failing run of 2026-10-30 dropped every HN post in 0.02s because the
proxy was unusable: requests failed instantly and the fetcher dropped each
post silently. These tests pin both fixes.
"""

import logging
from unittest.mock import MagicMock

import pytest
import requests

from comourat.clients.arxiv import ArxivClient
from comourat.clients.semantic_scholar import SemanticScholarClient
from comourat.enrichment.page_fetch import PageFetcher
from comourat.proxy import ProxyConfigurationError, validate_proxy_url


class TestValidateProxyUrl:
    def test_valid_socks_proxy_is_returned(self):
        proxy = "socks5h://user:pass@proxy.example:1080"
        assert validate_proxy_url(proxy) == proxy

    def test_valid_http_proxy_is_returned(self):
        proxy = "http://proxy.example:3128"
        assert validate_proxy_url(proxy) == proxy

    def test_surrounding_whitespace_is_stripped(self):
        assert validate_proxy_url("  http://proxy.example:3128  ") == (
            "http://proxy.example:3128"
        )

    def test_empty_proxy_is_rejected(self):
        with pytest.raises(ProxyConfigurationError):
            validate_proxy_url("")

    def test_unsupported_scheme_is_rejected(self):
        with pytest.raises(ProxyConfigurationError):
            validate_proxy_url("socks5h://:://")

    def test_proxy_without_host_is_rejected(self):
        with pytest.raises(ProxyConfigurationError):
            validate_proxy_url("http://")

    def test_socks_proxy_without_pysocks_is_rejected(self, monkeypatch):
        # The failing run of 2026-10-31: the environment had no PySocks, so
        # requests raised 'Missing dependencies for SOCKS support' for every
        # page and the fetcher dropped every post.
        monkeypatch.setattr("comourat.proxy._socks_support_available", lambda: False)
        with pytest.raises(ProxyConfigurationError, match="PySocks"):
            validate_proxy_url("socks5h://user:pass@proxy.example:1080")

    def test_socks_proxy_with_pysocks_is_accepted(self, monkeypatch):
        monkeypatch.setattr("comourat.proxy._socks_support_available", lambda: True)
        proxy = "socks5h://user:pass@proxy.example:1080"
        assert validate_proxy_url(proxy) == proxy


class TestClientsValidateProxy:
    def test_arxiv_client_rejects_malformed_proxy(self):
        with pytest.raises(ProxyConfigurationError):
            ArxivClient(user_agent="test/0.1", proxy="socks5h://:://")

    def test_semantic_scholar_client_rejects_malformed_proxy(self):
        with pytest.raises(ProxyConfigurationError):
            SemanticScholarClient(proxy="socks5h://:://")

    def test_clients_accept_valid_proxy(self):
        assert ArxivClient(user_agent="test/0.1", proxy="http://p:3128").proxy == (
            "http://p:3128"
        )


class TestPageFetcherFailsLoudly:
    def _session_raising(self, exc):
        session = MagicMock()
        session.get.side_effect = exc
        session.proxies = {}
        return session

    def test_malformed_proxy_rejected_at_construction(self):
        with pytest.raises(ProxyConfigurationError):
            PageFetcher(proxy="socks5h://:://")

    def test_all_transport_failures_log_a_warning(self, caplog):
        session = self._session_raising(requests.exceptions.InvalidURL("bad url"))
        fetcher = PageFetcher(http_session=session)
        with caplog.at_level(logging.WARNING):
            assert fetcher.fetch("https://example.com/") is None
            assert fetcher.fetch("https://example.com/") is None
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "every page request failed" in warnings[0].message

    def test_partial_failures_log_at_debug_only(self, caplog):
        session = MagicMock()
        response = MagicMock()
        response.text = (
            "<html><body><p>Some content here for the reader.</p></body></html>"
        )
        response.raise_for_status.return_value = None
        session.get.side_effect = [requests.exceptions.Timeout("slow"), response]
        session.proxies = {}
        fetcher = PageFetcher(http_session=session)
        with caplog.at_level(logging.DEBUG):
            fetcher.fetch("https://example.com/")
            fetcher.fetch("https://example.com/ok")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []
