"""arXiv client: title lookup by arXiv id and the ranged PDF probe.

Plain (non-`Function`) client called from within pipeline components. arXiv
answers Atom XML (not JSON) and its range-aware PDF endpoint normally answers
`206 Partial Content`, so both are handled here rather than by callers.
"""

import logging
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

from comourat.proxy import validate_proxy_url

logger = logging.getLogger(__name__)

ARXIV_API_QUERY_URL = "https://export.arxiv.org/api/query"
ARXIV_PDF_URL_TEMPLATE = "https://arxiv.org/pdf/{arxiv_id}"

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


class ArxivClient:
    """Client for the unauthenticated arXiv API and PDF endpoint.

    Args mirror the future Hydra group file keys one-to-one. `http_client` is
    injectable for tests; by default a module-level `requests.Session` with the
    configured identifying User-Agent is used.
    """

    def __init__(
        self,
        user_agent: str,
        timeout_seconds: float = 120.0,
        max_retries: int = 8,
        backoff_seconds: float = 2.0,
        regular_delay_seconds: float = 3.0,
        proxy: str | None = None,
        http_client: Any | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.regular_delay_seconds = regular_delay_seconds
        self.proxy = validate_proxy_url(proxy) if proxy else None
        self._session: requests.Session | None = None
        if http_client is None:
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self.user_agent})
            if self.proxy:
                self._session.proxies.update({"http": self.proxy, "https": self.proxy})
        else:
            self._http_client = http_client

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        """GET with bounded retry and backoff on 429/5xx and transport errors.

        A `Timeout` or `ConnectionError` is as transient as a 5xx — arXiv
        under sustained querying regularly hangs a read until the client
        timeout — so both are retried, bounded by `max_retries`.
        """
        attempt = 0
        while True:
            try:
                if self._session is not None:
                    response = self._session.get(
                        url, timeout=self.timeout_seconds, **kwargs
                    )
                else:
                    response = self._http_client.get(
                        url, timeout=self.timeout_seconds, **kwargs
                    )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self.max_retries:
                    raise
                attempt += 1
                delay = self.backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "arXiv request failed (%s); retry %d/%d in %.1fs: %s",
                    type(exc).__name__,
                    attempt,
                    self.max_retries,
                    delay,
                    url,
                )
                time.sleep(delay)
                continue
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt >= self.max_retries:
                time.sleep(self.regular_delay_seconds)  # arXiv politeness rate
                response.raise_for_status()
                return response
            attempt += 1
            delay = self.backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "arXiv request failed with %d; retry %d/%d in %.1fs: %s",
                response.status_code,
                attempt,
                self.max_retries,
                delay,
                url,
            )
            time.sleep(delay)

    def get_title_by_id(self, arxiv_id: str) -> str | None:
        """Return the title of an arXiv id, or None when the feed is empty.

        An unknown arXiv id returns a normal `<feed>` with zero `<entry>`s —
        no 404 — so an empty feed is "not found", not an error.
        """
        response = self._get(ARXIV_API_QUERY_URL, params={"id_list": arxiv_id})
        root = ET.fromstring(response.text)
        title = root.findtext("a:entry/a:title", namespaces=ATOM_NS)
        if title is None:
            return None
        # Atom titles arrive whitespace-mangled (line breaks, runs of spaces).
        return " ".join(title.split())

    def search_by_title(self, title: str) -> tuple[str, str] | None:
        """Find an arXiv preprint by title; returns (arxiv_id, arxiv_title).

        The top Atom entry of a `ti:"<title>"` search, with the id stripped of
        its version suffix and url prefix, and the title whitespace-collapsed.
        An empty feed (no preprint with that title) returns None — not an
        error, same convention as `get_title_by_id`. Callers must cross-check
        the returned title against their own claimed title: arXiv relevance
        matching can return a different paper.
        """
        response = self._get(
            ARXIV_API_QUERY_URL,
            params={"search_query": f'ti:"{title}"', "max_results": 1},
        )
        root = ET.fromstring(response.text)
        entry_id = root.findtext("a:entry/a:id", namespaces=ATOM_NS)
        entry_title = root.findtext("a:entry/a:title", namespaces=ATOM_NS)
        if entry_id is None or entry_title is None:
            return None
        # Entry ids look like `http://arxiv.org/abs/2305.13245v3`.
        bare_id = entry_id.rsplit("/", 1)[-1]
        arxiv_id = bare_id.split("v")[0] if "v" in bare_id[4:] else bare_id
        return arxiv_id, " ".join(entry_title.split())
