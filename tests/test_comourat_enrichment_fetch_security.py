"""Entry-point security matrix for article and enrichment fetching."""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from comourat.collectors.paper_discoverer import PaperDiscovererCore
from comourat.enrichment.enricher import EnrichmentAgent, create_enrichment_agent
from comourat.enrichment.page_fetch import ArticleFetchError, PageFetcher
from comourat.enrichment.web_tools import attach_web_tools
from comourat.fetch import FetchPolicy
from comourat.models import ArticleResult

ARTICLE = b"""<html><body><article><h1>Control title</h1>
<p>Allowed control article content provides enough useful text for extraction
and verifies that the protected entry point can return real article content.</p>
</article></body></html>"""
SEARCH = b"""<html><body><a class="result-link" href="https://example.com/article">
Allowed control result</a></body></html>"""


class _LoopbackControlPolicy(FetchPolicy):
    def _has_builtin_denial(self, address):
        if address == ipaddress.ip_address("127.0.0.2"):
            return False
        return super()._has_builtin_denial(address)


class _Handler(BaseHTTPRequestHandler):
    def _handle(self):
        self.server.requests_seen.append(self.path)
        if self.path in {"/redirect-prohibited", "/search-redirect"}:
            self.send_response(302)
            self.send_header("Location", self.server.redirect_target)
            self.send_header("Connection", "close")
            self.end_headers()
            return
        body = SEARCH if self.path == "/search" else ARTICLE
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *args):
        return


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        self.connections = 0
        self.requests_seen = []
        super().__init__(address, _Handler)

    def verify_request(self, request, client_address):
        self.connections += 1
        return True


@pytest.fixture
def servers():
    running = []

    def start(host):
        server = _Server((host, 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running.append((server, thread))
        return server

    yield start
    for server, thread in running:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _Agent:
    def __init__(self):
        self.tools = {}

    def tool(self, function):
        self.tools[function.__name__] = function
        return function


def _context():
    return SimpleNamespace(
        usage=SimpleNamespace(tool_calls=0),
        usage_limits=SimpleNamespace(tool_calls_limit=None),
    )


def _entry_point(kind, resolver, monkeypatch):
    if kind == "page":
        fetcher = PageFetcher(fetch_policy=_LoopbackControlPolicy(), resolver=resolver)

        def fetch_article(url):
            try:
                return fetcher.fetch(url)
            except ArticleFetchError as exc:
                return exc

        return fetch_article
    agent = _Agent()
    attach_web_tools(
        agent,
        fetch_policy=_LoopbackControlPolicy(),
        resolver=resolver,
    )
    if kind == "extract":
        return lambda url: agent.tools["extract_url"](_context(), url)

    def search(url):
        monkeypatch.setattr("comourat.enrichment.web_tools.DEFAULT_SEARCH_URL", url)
        return agent.tools["web_search"](_context(), "control")

    return search


def _path_for(kind, path=""):
    if kind == "search":
        return path or "/search"
    return path or "/article"


def _succeeded(kind, result):
    if kind == "page":
        return (
            isinstance(result, ArticleResult)
            and "Allowed control article content" in result.text
        )
    if kind == "extract":
        return "Allowed control article content" in result
    return "Title: Allowed control result" in result


def _failed(result):
    return (
        isinstance(result, ArticleFetchError)
        or result is None
        or (isinstance(result, str) and result.startswith("Error "))
    )


@pytest.mark.parametrize("kind", ["page", "extract", "search"])
def test_allowed_entry_point_control(kind, servers, monkeypatch):
    allowed = servers("127.0.0.2")
    invoke = _entry_point(kind, lambda host, port: [], monkeypatch)
    result = invoke(f"http://127.0.0.2:{allowed.server_port}{_path_for(kind)}")
    assert _succeeded(kind, result)


@pytest.mark.parametrize("kind", ["page", "extract", "search"])
def test_direct_prohibited_target_at_each_entry_point(kind, servers, monkeypatch):
    prohibited = servers("127.0.0.1")
    invoke = _entry_point(kind, lambda host, port: [], monkeypatch)
    result = invoke(f"http://127.0.0.1:{prohibited.server_port}{_path_for(kind)}")
    assert _failed(result)
    assert prohibited.connections == 0
    assert prohibited.requests_seen == []


@pytest.mark.parametrize("kind", ["page", "extract", "search"])
def test_redirect_to_prohibited_target_at_each_entry_point(kind, servers, monkeypatch):
    allowed = servers("127.0.0.2")
    prohibited = servers("127.0.0.1")
    allowed.redirect_target = (
        f"http://127.0.0.1:{prohibited.server_port}{_path_for(kind)}"
    )
    invoke = _entry_point(kind, lambda host, port: [], monkeypatch)
    redirect_path = "/search-redirect" if kind == "search" else "/redirect-prohibited"
    result = invoke(f"http://127.0.0.2:{allowed.server_port}{redirect_path}")
    assert _failed(result)
    assert prohibited.connections == 0
    assert prohibited.requests_seen == []


@pytest.mark.parametrize("kind", ["page", "extract", "search"])
def test_resolution_change_at_each_entry_point(kind, servers, monkeypatch):
    allowed = servers("127.0.0.2")
    prohibited = servers("127.0.0.1")
    answers = iter(("127.0.0.2", "127.0.0.1"))

    def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))]

    invoke = _entry_point(kind, resolver, monkeypatch)
    path = _path_for(kind)
    first = invoke(f"http://changing.test:{allowed.server_port}{path}")
    assert _succeeded(kind, first)
    second = invoke(f"http://changing.test:{prohibited.server_port}{path}")
    assert _failed(second)
    assert prohibited.connections == 0
    assert prohibited.requests_seen == []


def _fetch_dependencies():
    return {
        "http_session": MagicMock(),
        "proxy": "socks5h://proxy.test:1080",
        "fetch_policy": FetchPolicy(),
        "resolver": lambda host, port: [],
    }


def test_attach_web_tools_accepts_all_fetch_dependencies(monkeypatch):
    dependencies = _fetch_dependencies()
    client = MagicMock()
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("comourat.enrichment.web_tools.SafeHttpClient", factory)
    attach_web_tools(_Agent(), **dependencies)
    factory.assert_called_once_with(
        dependencies["http_session"],
        fetch_policy=dependencies["fetch_policy"],
        proxy=dependencies["proxy"],
        resolver=dependencies["resolver"],
    )


def test_create_enrichment_agent_passes_fetch_dependencies(monkeypatch):
    dependencies = _fetch_dependencies()
    agent = _Agent()
    monkeypatch.setattr(
        "comourat.enrichment.enricher.Agent", MagicMock(return_value=agent)
    )
    attach = MagicMock(return_value=agent)
    monkeypatch.setattr("comourat.enrichment.enricher.attach_web_tools", attach)
    assert create_enrichment_agent(MagicMock(), **dependencies) is agent
    assert attach.call_args.kwargs == {
        "tool_call_rejection_buffer": 4,
        "raise_on_error": True,
        **dependencies,
    }


def test_enrichment_agent_passes_fetch_dependencies(monkeypatch):
    dependencies = _fetch_dependencies()
    agent = _Agent()
    agent._comourat_tool_time_total = 0
    factory = MagicMock(return_value=agent)
    monkeypatch.setattr("comourat.enrichment.enricher.create_enrichment_agent", factory)
    EnrichmentAgent(MagicMock(), **dependencies)
    assert factory.call_args.kwargs == {
        "system_prompt": factory.call_args.kwargs["system_prompt"],
        "model_settings": None,
        "retries": None,
        "tool_call_rejection_buffer": 4,
        **dependencies,
    }


def test_paper_discoverer_passes_fetch_dependencies(monkeypatch):
    dependencies = _fetch_dependencies()
    agent = _Agent()
    monkeypatch.setattr(
        "comourat.collectors.paper_discoverer.Agent", MagicMock(return_value=agent)
    )
    attach = MagicMock()
    monkeypatch.setattr("comourat.collectors.paper_discoverer.attach_web_tools", attach)
    PaperDiscovererCore(MagicMock(), "research description", **dependencies)
    assert attach.call_args.kwargs == {
        "tool_call_rejection_buffer": 4,
        **dependencies,
    }


def test_page_fetch_logs_redact_target_credentials_paths_and_queries(caplog):
    session = MagicMock()
    session.proxies = {}
    session.get.side_effect = requests.ConnectionError(
        "failed http://user:secret@example.com/generate?token=private"
    )
    fetcher = PageFetcher(http_session=session)
    with caplog.at_level(logging.DEBUG):
        for _ in range(2):
            with pytest.raises(ArticleFetchError):
                fetcher.fetch("http://user:secret@example.com/generate?token=private")
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret" not in messages
    assert "private" not in messages
    assert "/generate" not in messages
    assert "http://example.com" in messages
