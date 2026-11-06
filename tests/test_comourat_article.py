"""Public article result, failure, extraction, and deadline contract tests."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from comourat.enrichment.page_fetch import (
    ArticleFetchError,
    PageFetcher,
    extract_article_title,
    extract_main_content,
)
from comourat.fetch import FetchDeadlineExceeded, FetchPolicy, FetchResolutionError
from comourat.models import ArticleResult

URL = "https://example.com/research/article"
HTML = "<html><head><title>Ignored</title></head><body>Controlled HTML</body></html>"


def _session(response: MagicMock | None = None) -> MagicMock:
    session = MagicMock()
    session.proxies = {}
    if response is not None:
        session.get.return_value = response
    return session


def _response(
    status: int = 200,
    html: str = HTML,
    final_url: str | None = URL,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.text = html
    response.url = final_url
    return response


def _fetcher(session: MagicMock, **kwargs) -> PageFetcher:
    kwargs.setdefault("extractor", lambda html: "  Extracted article text.  ")
    kwargs.setdefault("title_extractor", lambda html: "  Extracted title  ")
    return PageFetcher(http_session=session, **kwargs)


def _assert_category(error: ArticleFetchError, category: str) -> None:
    assert error.category == category
    assert str(error)
    assert URL not in str(error)


def test_fetch_returns_complete_unsummarized_article_and_closes_response():
    response = _response(final_url="https://example.com/final-article")
    session = _session(response)
    extracted_text = "  First paragraph.\n\nSecond paragraph, without a summary.  "
    fetcher = _fetcher(session, extractor=lambda html: extracted_text)

    result = fetcher.fetch(URL)

    assert isinstance(result, ArticleResult)
    assert result.title == "Extracted title"
    assert result.source_url == "https://example.com/final-article"
    assert result.text == "First paragraph.\n\nSecond paragraph, without a summary."
    session.get.assert_called_once()
    response.close.assert_called_once()


@pytest.mark.parametrize(
    ("html", "metadata_title", "expected"),
    [
        (
            '<html><head><meta property="og:title" content=" Metadata title ">'
            "</head><body><h1>Heading</h1></body></html>",
            " Metadata title ",
            "Metadata title",
        ),
        (
            "<html><head><title> Document title </title></head>"
            "<body><h1>Heading</h1></body></html>",
            None,
            "Document title",
        ),
        (
            "<html><body><h1> First heading </h1><h1>Second</h1></body></html>",
            None,
            "First heading",
        ),
    ],
)
def test_default_title_extractor_uses_metadata_and_document_fallbacks(
    monkeypatch, html, metadata_title, expected
):
    monkeypatch.setattr(
        "comourat.enrichment.page_fetch.extract_metadata",
        lambda content: SimpleNamespace(title=metadata_title),
    )
    assert extract_article_title(html) == expected


def test_default_extractors_return_real_title_and_main_text():
    html = """
    <html><head><title>Research article title</title></head>
    <body><article><h1>Research article title</h1>
    <p>This controlled research article contains enough useful text for the
    default extractor to identify its main content without any network access.
    The paragraph explains the extraction contract in plain language.</p>
    </article></body></html>
    """
    assert extract_article_title(html) == "Research article title"
    text = extract_main_content(html)
    assert text is not None
    assert "controlled research article" in text


def test_source_url_falls_back_to_validated_input():
    session = _session(_response(final_url=""))
    result = _fetcher(session).fetch(f"  {URL}  ")
    assert result.source_url == URL


@pytest.mark.parametrize(
    "url",
    [
        None,
        42,
        "",
        "   ",
        "not a URL",
        "example.com/article",
        "http:///missing",
        "http://example.com:bad/",
        "http://example.com:0/",
        "http://[::1/",
    ],
)
def test_invalid_input_fails_before_a_session_request(url):
    session = _session()
    fetcher = _fetcher(session)
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch(url)
    _assert_category(caught.value, "invalid_input")
    session.get.assert_not_called()


@pytest.mark.parametrize(
    "url",
    ["ftp://example.com/article", "file:///etc/passwd", "mailto:user@example.com"],
)
def test_unsupported_scheme_fails_before_a_session_request(url):
    session = _session()
    fetcher = _fetcher(session)
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch(url)
    _assert_category(caught.value, "unsupported_scheme")
    session.get.assert_not_called()


def test_local_policy_denial_is_invalid_input_without_a_request():
    session = _session()
    fetcher = _fetcher(session, fetch_policy=FetchPolicy(deny_hosts=["example.com"]))
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch(URL)
    _assert_category(caught.value, "invalid_input")
    session.get.assert_not_called()


def test_unsupported_scheme_policy_failure_has_its_own_category():
    session = _session()
    session.get.side_effect = requests.exceptions.InvalidSchema("unsupported redirect")
    fetcher = _fetcher(session)
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch(URL)
    _assert_category(caught.value, "unsupported_scheme")


@pytest.mark.parametrize("status", [404, 410])
def test_missing_source_content_is_unavailable(status):
    response = _response(status=status)
    fetcher = _fetcher(_session(response))
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch(URL)
    _assert_category(caught.value, "unavailable")
    response.close.assert_called_once()


@pytest.mark.parametrize("status", [400, 403, 500, 503, 302, True, None])
def test_other_http_and_invalid_statuses_are_provider_failures(status):
    response = _response(status=status)
    fetcher = _fetcher(_session(response))
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch(URL)
    _assert_category(caught.value, "provider")
    response.close.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        FetchResolutionError("DNS failed"),
        requests.ConnectionError("connection failed"),
        requests.RequestException("request failed"),
        RuntimeError("provider-specific failure"),
    ],
)
def test_source_request_failures_are_provider_failures(error):
    session = _session()
    session.get.side_effect = error
    fetcher = _fetcher(session)
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch(URL)
    _assert_category(caught.value, "provider")


def test_source_side_invalid_url_is_invalid_input():
    session = _session()
    session.get.side_effect = requests.exceptions.InvalidURL("bad redirect")
    fetcher = _fetcher(session)
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch(URL)
    _assert_category(caught.value, "invalid_input")


@pytest.mark.parametrize(
    "error",
    [
        requests.ConnectTimeout("connection timeout"),
        requests.ReadTimeout("read timeout"),
        FetchDeadlineExceeded("overall deadline"),
        TimeoutError("source timeout"),
    ],
)
def test_source_timeouts_do_not_start_extraction(error):
    session = _session()
    session.get.side_effect = error
    extractor = MagicMock()
    title_extractor = MagicMock()
    fetcher = _fetcher(session, extractor=extractor, title_extractor=title_extractor)
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch(URL)
    _assert_category(caught.value, "timeout")
    extractor.assert_not_called()
    title_extractor.assert_not_called()


@pytest.mark.parametrize("failed_extractor", ["title", "text"])
def test_extractor_exceptions_are_extraction_failures(failed_extractor):
    title_extractor = MagicMock(return_value="Title")
    extractor = MagicMock(return_value="Text")
    if failed_extractor == "title":
        title_extractor.side_effect = RuntimeError("secret parser details")
    else:
        extractor.side_effect = RuntimeError("secret parser details")
    fetcher = _fetcher(
        _session(_response()), extractor=extractor, title_extractor=title_extractor
    )

    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch(URL)
    _assert_category(caught.value, "extraction")
    assert "secret parser details" not in str(caught.value)


@pytest.mark.parametrize(
    ("title", "text"),
    [(42, "Text"), ("Title", 42), (["Title"], "Text"), ("Title", ["Text"])],
)
def test_invalid_extractor_return_types_are_extraction_failures(title, text):
    fetcher = _fetcher(
        _session(_response()),
        extractor=lambda html: text,
        title_extractor=lambda html: title,
    )
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch(URL)
    _assert_category(caught.value, "extraction")


@pytest.mark.parametrize(
    ("title", "text"),
    [
        (None, "Text"),
        ("", "Text"),
        ("   ", "Text"),
        ("Title", None),
        ("Title", ""),
        ("Title", "  \n"),
    ],
)
def test_empty_title_or_text_is_empty_extraction(title, text):
    response = _response()
    fetcher = _fetcher(
        _session(response),
        extractor=lambda html: text,
        title_extractor=lambda html: title,
    )
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch(URL)
    _assert_category(caught.value, "empty_extraction")
    response.close.assert_called_once()


def test_timeout_defaults_aliases_and_custom_policy():
    default = _fetcher(_session())
    assert default.connection_timeout == 10.0
    assert default.operation_timeout == 30.0
    assert default.timeout == 30.0

    direct = _fetcher(_session(), connection_timeout=2, operation_timeout=8)
    assert direct.connection_timeout == 2.0
    assert direct.operation_timeout == 8.0

    alias = _fetcher(_session(), timeout=6)
    assert alias.operation_timeout == 6.0
    assert alias.timeout == 6.0

    policy = FetchPolicy(connection_timeout=None, overall_timeout=12)
    custom = _fetcher(_session(), fetch_policy=policy)
    assert custom.connection_timeout is None
    assert custom.operation_timeout == 12.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 1, "operation_timeout": 2},
        {"timeout": 1, "fetch_policy": FetchPolicy()},
        {"connection_timeout": 1, "fetch_policy": FetchPolicy()},
        {"operation_timeout": 1, "fetch_policy": FetchPolicy()},
    ],
)
def test_conflicting_timeout_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        _fetcher(_session(), **kwargs)


@pytest.mark.parametrize("field", ["connection_timeout", "operation_timeout"])
@pytest.mark.parametrize(
    "value", [0, -1, True, False, float("nan"), float("inf"), "10"]
)
def test_invalid_direct_timeouts_are_rejected(field, value):
    with pytest.raises(ValueError):
        _fetcher(_session(), **{field: value})


def test_page_fetcher_passes_connection_timeout_to_protected_policy():
    fetcher = _fetcher(_session(), connection_timeout=1.5, operation_timeout=4)
    assert fetcher.http_client.policy.connection_timeout == 1.5
    assert fetcher.http_client.policy.overall_timeout == 4.0


def test_extraction_wait_uses_remaining_operation_time():
    response = _response()
    session = _session(response)

    def slow_source(*args, **kwargs):
        time.sleep(0.08)
        return response

    session.get.side_effect = slow_source
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_extractor(html):
        started.set()
        release.wait(2)
        finished.set()
        return "Late text"

    fetcher = _fetcher(
        session,
        extractor=blocking_extractor,
        operation_timeout=0.12,
        title_extractor=lambda html: "Title",
    )
    before = time.monotonic()
    try:
        with pytest.raises(ArticleFetchError) as caught:
            fetcher.fetch(URL)
        _assert_category(caught.value, "timeout")
        assert time.monotonic() - before < 0.5
        assert started.is_set()
        session.get.assert_called_once()
    finally:
        release.set()
    assert finished.wait(1)


def test_late_extraction_result_on_controlled_clock_is_timeout():
    class ControlledClock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = ControlledClock()

    def late_extractor(html):
        clock.value = 31.0
        return "Late text"

    fetcher = _fetcher(_session(_response()), extractor=late_extractor, clock=clock)
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch(URL)
    _assert_category(caught.value, "timeout")


def test_public_failure_messages_do_not_copy_sensitive_provider_text():
    secret = "credential-secret-private-path"
    session = _session()
    session.get.side_effect = requests.ConnectionError(secret)
    first = _fetcher(session)
    with pytest.raises(ArticleFetchError) as first_error:
        first.fetch(URL)
    assert secret not in str(first_error.value)

    second_session = _session()
    second_session.get.side_effect = requests.ConnectionError("different details")
    second = _fetcher(second_session)
    with pytest.raises(ArticleFetchError) as second_error:
        second.fetch(URL)
    assert str(second_error.value) == str(first_error.value)
