"""Page fetching and main-content extraction."""

from __future__ import annotations

import logging

import requests
from trafilatura import extract

from comourat.proxy import ProxyConfigurationError, validate_proxy_url

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


def extract_main_content(html: str) -> str | None:
    """Extract main article content from HTML; None when nothing extractable."""
    return extract(html, include_comments=False, include_tables=True)


class PageFetcher:
    """Fetches a page URL via the configured session and extracts content.

    Returns None when the fetch fails, extraction fails, or nothing is
    extractable; the caller decides how to treat a None result. The
    extractor is injectable (defaults to `extract_main_content`).

    A transport failure is logged at WARNING when it affects every request
    of a batch (a broken proxy, for example) so a silent drop of all items
    cannot pass unnoticed.
    """

    def __init__(
        self,
        http_session=None,
        proxy: str | None = None,
        timeout: int = 30,
        extractor=extract_main_content,
    ) -> None:
        self.session = http_session if http_session is not None else requests.Session()
        if proxy:
            validated = validate_proxy_url(proxy)
            self.session.proxies.update({"http": validated, "https": validated})
        self.proxy = proxy
        self.timeout = timeout
        self.extractor = extractor
        self._transport_failures = 0
        self._requests = 0
        self._all_failed_logged = False

    def _log_transport_failure(self, url: str, exc: Exception) -> None:
        """Log a transport failure; warn loudly once when all requests fail."""
        self._transport_failures += 1
        if (
            self._requests >= 2
            and self._transport_failures == self._requests
            and not self._all_failed_logged
        ):
            self._all_failed_logged = True
            logger.warning(
                "every page request failed (%d/%d); check the proxy "
                "configuration and network access: %s",
                self._transport_failures,
                self._requests,
                exc,
            )
        else:
            logger.debug("fetch failed for %s: %s", url, exc)

    def fetch(self, url: str) -> str | None:
        self._requests += 1
        try:
            resp = self.session.get(
                url,
                timeout=self.timeout,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            self._log_transport_failure(url, exc)
            return None
        try:
            content = self.extractor(resp.text)
        except Exception:
            logger.exception("content extraction failed unexpectedly for %s", url)
            return None
        if not content:
            logger.debug("fetch yielded no content for %s", url)
            return None
        return content
