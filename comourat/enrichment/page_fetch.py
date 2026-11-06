"""Page fetching and research-article title and main-content extraction."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Literal, TypeAlias, cast
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from trafilatura import extract, extract_metadata

from comourat.fetch import (
    Clock,
    FetchPolicy,
    FetchPolicyError,
    Resolver,
    SafeHttpClient,
    _redact_url,
)
from comourat.models import ArticleResult

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
DEFAULT_CONNECTION_TIMEOUT = 10.0
DEFAULT_OPERATION_TIMEOUT = 30.0

ArticleErrorCategory: TypeAlias = Literal[
    "invalid_input",
    "unsupported_scheme",
    "unavailable",
    "empty_extraction",
    "timeout",
    "provider",
    "extraction",
]

_ERROR_MESSAGES: dict[ArticleErrorCategory, str] = {
    "invalid_input": "Article URL or fetch policy input is invalid.",
    "unsupported_scheme": "Article URL scheme is not supported; use HTTP or HTTPS.",
    "unavailable": "Article content is unavailable.",
    "empty_extraction": "Article extraction returned an empty title or text.",
    "timeout": "Article operation exceeded a configured timeout.",
    "provider": "Article source request failed.",
    "extraction": "Article title or text extraction failed.",
}


class ArticleFetchError(RuntimeError):
    """An expected article failure with a stable category and safe message."""

    def __init__(self, category: ArticleErrorCategory) -> None:
        self.category = category
        super().__init__(_ERROR_MESSAGES[category])


def extract_main_content(html: str) -> str | None:
    """Extract main article content from HTML; None when nothing extractable."""
    return extract(html, include_comments=False, include_tables=True)


def extract_article_title(html: str) -> str | None:
    """Extract a title from metadata, the document title, or the first h1."""
    metadata = extract_metadata(html)
    if metadata is not None:
        title = metadata.title
        if isinstance(title, str) and title.strip():
            return title.strip()

    soup = BeautifulSoup(html, "html.parser")
    if soup.title is not None:
        title = soup.title.get_text(" ", strip=True)
        if title:
            return title
    heading = soup.find("h1")
    if heading is not None:
        title = heading.get_text(" ", strip=True)
        if title:
            return title
    return None


class PageFetcher:
    """Fetch a public research article and return its complete extraction result.

    Expected input, source, timeout, and extraction failures raise
    `ArticleFetchError`. The `extractor` and `title_extractor` arguments are
    injectable. Direct timeout arguments construct the default fetch policy.
    A caller-provided `fetch_policy` replaces all direct timeout settings.
    """

    def __init__(
        self,
        http_session=None,
        proxy: str | None = None,
        timeout: float | None = None,
        extractor=extract_main_content,
        fetch_policy: FetchPolicy | None = None,
        resolver: Resolver | None = None,
        connection_timeout: float | None = None,
        operation_timeout: float | None = None,
        title_extractor=extract_article_title,
        clock: Clock | None = None,
    ) -> None:
        direct_timeouts_used = (
            timeout is not None
            or connection_timeout is not None
            or operation_timeout is not None
        )
        if fetch_policy is not None and direct_timeouts_used:
            raise ValueError("pass direct timeout settings or fetch_policy, not both")
        if timeout is not None and operation_timeout is not None:
            raise ValueError("pass either timeout or operation_timeout, not both")

        if fetch_policy is not None:
            policy = fetch_policy
        else:
            overall_timeout = DEFAULT_OPERATION_TIMEOUT
            if operation_timeout is not None:
                overall_timeout = operation_timeout
            elif timeout is not None:
                overall_timeout = timeout
            policy = FetchPolicy(
                overall_timeout=overall_timeout,
                connection_timeout=(
                    DEFAULT_CONNECTION_TIMEOUT
                    if connection_timeout is None
                    else connection_timeout
                ),
            )
        self._clock = clock or time.monotonic
        self.http_client = SafeHttpClient(
            http_session,
            fetch_policy=policy,
            proxy=proxy,
            resolver=resolver,
            clock=self._clock,
        )
        self.session = self.http_client.session
        self.proxy = self.http_client.proxy
        self.timeout = policy.overall_timeout
        self.operation_timeout = policy.overall_timeout
        self.connection_timeout = policy.connection_timeout
        self.extractor = extractor
        self.title_extractor = title_extractor
        self._transport_failures = 0
        self._requests = 0
        self._all_failed_logged = False

    @staticmethod
    def _validate_url(url: str) -> str:
        if not isinstance(url, str):
            raise ArticleFetchError("invalid_input")
        candidate = url.strip()
        if not candidate or any(ord(char) < 32 for char in candidate):
            raise ArticleFetchError("invalid_input")
        try:
            parts = urlsplit(candidate)
            port = parts.port
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ArticleFetchError("invalid_input") from exc
        if not parts.scheme:
            raise ArticleFetchError("invalid_input")
        if parts.scheme.lower() not in {"http", "https"}:
            raise ArticleFetchError("unsupported_scheme")
        if not parts.hostname or port == 0:
            raise ArticleFetchError("invalid_input")
        return candidate

    @staticmethod
    def _policy_category(error: FetchPolicyError) -> ArticleErrorCategory:
        message = str(error)
        if "scheme" in message and "not supported" in message:
            return "unsupported_scheme"
        return "invalid_input"

    def _log_transport_failure(self, url: str, exc: Exception) -> None:
        """Log a transport failure; warn loudly once when all requests fail."""
        self._transport_failures += 1
        if (
            2 <= self._requests == self._transport_failures
            and not self._all_failed_logged
        ):
            self._all_failed_logged = True
            logger.warning(
                "every page request failed (%d/%d); check the proxy "
                "configuration and network access: %s",
                self._transport_failures,
                self._requests,
                type(exc).__name__,
            )
        else:
            logger.debug(
                "fetch failed for %s (%s)", _redact_url(url), type(exc).__name__
            )

    def _transport_error(
        self,
        url: str,
        category: ArticleErrorCategory,
        error: Exception,
    ) -> ArticleFetchError:
        self._log_transport_failure(url, error)
        return ArticleFetchError(category)

    def _get_html(self, url: str) -> tuple[str, str]:
        response = None
        try:
            response = self.http_client.get(
                url,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
            status = response.status_code
            if status in {404, 410}:
                raise ArticleFetchError("unavailable")
            if (
                isinstance(status, bool)
                or not isinstance(status, int)
                or not 200 <= status < 300
            ):
                raise ArticleFetchError("provider")
            html = response.text
            if not isinstance(html, str):
                raise TypeError("article response text is not a string")
            response_url = response.url
            source_url = (
                response_url.strip()
                if isinstance(response_url, str) and response_url.strip()
                else url
            )
            return html, source_url
        except ArticleFetchError as exc:
            self._log_transport_failure(url, exc)
            raise
        except (requests.Timeout, TimeoutError) as exc:
            raise self._transport_error(url, "timeout", exc) from exc
        except requests.exceptions.InvalidSchema as exc:
            raise self._transport_error(url, "unsupported_scheme", exc) from exc
        except FetchPolicyError as exc:
            category = self._policy_category(exc)
            raise self._transport_error(url, category, exc) from exc
        except (requests.exceptions.InvalidURL, requests.exceptions.URLRequired) as exc:
            raise self._transport_error(url, "invalid_input", exc) from exc
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise self._transport_error(url, "provider", exc) from exc
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.debug("failed to close article response")

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise ArticleFetchError("timeout")
        return remaining

    def _extract(self, html: str, deadline: float) -> tuple[str, str]:
        results: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def extract_worker() -> None:
            try:
                value = (self.title_extractor(html), self.extractor(html))
                results.put((True, value))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                results.put((False, exc))

        self._remaining(deadline)
        thread = threading.Thread(target=extract_worker, daemon=True)
        thread.start()
        try:
            succeeded, value = results.get(timeout=self._remaining(deadline))
        except queue.Empty as exc:
            raise ArticleFetchError("timeout") from exc
        self._remaining(deadline)

        if not succeeded:
            error = cast(Exception, value)
            logger.debug("article extractor failed (%s)", type(error).__name__)
            raise ArticleFetchError("extraction") from error
        if not isinstance(value, tuple) or len(value) != 2:
            raise ArticleFetchError("extraction")
        title, text = value
        if title is None or text is None:
            raise ArticleFetchError("empty_extraction")
        if not isinstance(title, str) or not isinstance(text, str):
            raise ArticleFetchError("extraction")
        title = title.strip()
        text = text.strip()
        if not title or not text:
            raise ArticleFetchError("empty_extraction")
        return title, text

    def fetch(self, url: str) -> ArticleResult:
        """Return a complete article or raise a categorized extraction error."""
        deadline = self._clock() + self.operation_timeout
        validated_url = self._validate_url(url)
        self._requests += 1
        html, source_url = self._get_html(validated_url)
        title, text = self._extract(html, deadline)
        self._remaining(deadline)
        return ArticleResult(title=title, source_url=source_url, text=text)
